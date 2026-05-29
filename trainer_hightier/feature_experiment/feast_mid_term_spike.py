"""Feast feasibility spike: canonical mid-term compute, materialize, online lookup.

Experimental probe only — not wired into production scorer. Measures compute /
materialize / lookup latency and missing-rate for a minimal mid-term feature slice.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    HK_TZ,
    MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS,
    MID_TERM_SNAPSHOT_SCOPE_PRODUCTION,
    configs_from_run_profile,
    default_hightier_serving_config,
    get_run_profile,
)
from trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot import (
    MID_TERM_SNAPSHOT_OUTPUT_COLUMNS,
    materialize_mid_term_daily_snapshot,
)
from trainer_hightier.serving.adt_allowlist import load_adt_allowlist_ids, resolve_adt_allowlist_path
from trainer_hightier.serving.ch_adapter import (
    CH_TBET_PAYOUT_ODDS_SELECT,
    CH_TBET_WAGER_POSITIVE_PRED,
    CH_TBET_WAGER_SELECT,
    get_clickhouse_client,
)
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path

logger = logging.getLogger(__name__)

SPIKE_MID_TERM_FEATURE_COLUMNS: Final[tuple[str, ...]] = tuple(
    c
    for c in MID_TERM_SNAPSHOT_OUTPUT_COLUMNS
    if c not in ("canonical_id", "anchor_gaming_day_event")
)
SPIKE_FEATURE_VIEW_NAME: Final[str] = "mid_term_daily_spike_features"
SPIKE_FEATURE_SERVICE_NAME: Final[str] = "walkaway_canonical_mid_term_spike_v1"
SPIKE_ONLINE_FEATURE_REFS: Final[tuple[str, ...]] = tuple(
    f"{SPIKE_FEATURE_VIEW_NAME}:{c}" for c in SPIKE_MID_TERM_FEATURE_COLUMNS
)


def _split_player_id_chunks(ids: frozenset[int], chunk_size: int) -> list[list[int]]:
    """Return stable sorted ``player_id`` chunks for bounded ClickHouse ``IN`` lists."""
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size!r}")
    if not ids:
        return []
    sorted_ids = sorted(int(x) for x in ids)
    return [sorted_ids[i : i + chunk_size] for i in range(0, len(sorted_ids), chunk_size)]


@dataclass(frozen=True)
class FeastMidTermSpikeConfig:
    """Spike runner settings (edit here; no environment-variable SSOT)."""

    repo_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])
    feast_repo: Path | None = None
    spike_parquet: Path | None = None
    staging_dir: Path | None = None
    report_path: Path | None = None
    canonical_mapping_parquet: Path | None = None
    adt_allowlist_parquet: Path | None = None
    duckdb_runtime: DuckDbRuntimeConfig = field(
        default_factory=lambda: configs_from_run_profile(get_run_profile("laptop_8g"))[0],
    )
    anchor_days: int = 3
    lookup_batch_size: int = 20
    #: ``clickhouse`` | ``local_cleaned``
    bet_source: str = "clickhouse"
    local_cleaned_bet: Path | None = None
    #: ``small_sample`` limits to ADT allowlist universe; ``wider_sample`` does not.
    sample_mode: str = "small_sample"


def default_spike_config() -> FeastMidTermSpikeConfig:
    """Resolve default artifact paths under ``trainer_hightier/artifacts/feast``."""
    pkg = Path(__file__).resolve().parents[1]
    feast_art = pkg / "artifacts" / "feast"
    return FeastMidTermSpikeConfig(
        feast_repo=pkg / "feast_repo",
        spike_parquet=feast_art / "mid_term_spike_canonical.parquet",
        staging_dir=feast_art / "spike_staging",
        report_path=feast_art / "mid_term_spike_report.json",
        local_cleaned_bet=pkg / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet",
    )


def _anchor_bounds(*, anchor_days: int) -> tuple[date, date, date, date]:
    """Return anchor_start, anchor_end, bets_gday_start, bets_gday_end (HK calendar)."""
    if anchor_days < 1:
        raise ValueError(f"anchor_days must be >= 1, got {anchor_days!r}")
    hk = ZoneInfo(HK_TZ)
    today = datetime.now(hk).date()
    anchor_end = today - timedelta(days=1)
    anchor_start = anchor_end - timedelta(days=int(anchor_days) - 1)
    lb = int(MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS)
    bets_gday_end = anchor_end
    bets_gday_start = anchor_start - timedelta(days=lb - 1)
    return anchor_start, anchor_end, bets_gday_start, bets_gday_end


def export_clickhouse_bets_to_parquet(
    out_parquet: Path,
    *,
    bets_gaming_day_start: date,
    bets_gaming_day_end: date,
    player_ids: frozenset[int] | None,
) -> dict[str, Any]:
    """Export minimal cleaned-bet columns from ClickHouse to a single Parquet file."""
    cfg = default_hightier_serving_config()
    client = get_clickhouse_client()
    placeholder = int(cfg.placeholder_player_id)
    out_parquet = Path(out_parquet).resolve()
    out_parquet.parent.mkdir(parents=True, exist_ok=True)

    def _query(player_filter: str) -> pd.DataFrame:
        q = f"""
        SELECT
            CAST(player_id AS Int64) AS player_id,
            CAST(gaming_day_event AS Date) AS gaming_day_event,
            CAST(payout_complete_dtm AS DateTime64(3, 'UTC')) AS payout_complete_dtm,
            {CH_TBET_WAGER_SELECT},
            {CH_TBET_PAYOUT_ODDS_SELECT}
        FROM {cfg.source_db}.{cfg.tbet} FINAL
        WHERE gaming_day_event >= %(g_start)s
          AND gaming_day_event <= %(g_end)s
          AND payout_complete_dtm IS NOT NULL
          AND gaming_day_event IS NOT NULL
          AND {CH_TBET_WAGER_POSITIVE_PRED}
          AND player_id IS NOT NULL
          AND player_id != {placeholder}
          {player_filter}
    """
        return client.query_df(
            q,
            parameters={
                "g_start": bets_gaming_day_start,
                "g_end": bets_gaming_day_end,
            },
        )

    t0 = time.perf_counter()
    chunk_count = 0
    chunk_size = 0
    query_count = 1
    if player_ids is None:
        df = _query("")
    else:
        if not player_ids:
            raise ValueError("player_ids is empty; cannot export ClickHouse bets")
        chunk_size = int(cfg.hightier_scorer_player_id_chunk_size)
        chunks = _split_player_id_chunks(player_ids, chunk_size)
        chunk_count = len(chunks)
        query_count = chunk_count
        frames: list[pd.DataFrame] = []
        row_cap = int(cfg.hightier_scorer_chunk_merge_row_cap)
        for i, chunk in enumerate(chunks):
            in_list = ",".join(str(int(x)) for x in chunk)
            frames.append(_query(f"AND player_id IN ({in_list})"))
            if row_cap > 0:
                rows_so_far = sum(len(frame) for frame in frames)
                if rows_so_far > row_cap:
                    raise RuntimeError(
                        "feast mid-term spike ClickHouse export exceeds "
                        f"hightier_scorer_chunk_merge_row_cap={row_cap} "
                        f"({rows_so_far} rows after chunk {i + 1}/{chunk_count})"
                    )
        nonempty = [frame for frame in frames if frame is not None and not frame.empty]
        df = pd.concat(nonempty, ignore_index=True) if nonempty else pd.DataFrame()
    elapsed = round(time.perf_counter() - t0, 3)
    if df.empty:
        raise ValueError(
            f"ClickHouse export returned 0 rows for gaming_day in "
            f"[{bets_gaming_day_start}, {bets_gaming_day_end}]"
        )
    df.to_parquet(out_parquet, index=False)
    return {
        "source": "clickhouse",
        "rows_exported": int(len(df)),
        "export_seconds": elapsed,
        "query_count": int(query_count),
        "player_id_chunk_count": int(chunk_count),
        "player_id_chunk_size": int(chunk_size),
        "path": str(out_parquet),
        "gaming_day_start": bets_gaming_day_start.isoformat(),
        "gaming_day_end": bets_gaming_day_end.isoformat(),
    }


def build_canonical_universe_from_allowlist(
    allowlist_parquet: Path,
    mapping_parquet: Path,
    out_parquet: Path,
) -> dict[str, Any]:
    """Map ADT allowlist ``player_id`` values to distinct ``canonical_id`` for semi-join."""
    allow = Path(allowlist_parquet).resolve()
    cmap = Path(mapping_parquet).resolve()
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not allow.is_file():
        raise FileNotFoundError(f"adt allowlist missing: {allow}")
    if not cmap.is_file():
        raise FileNotFoundError(f"canonical mapping missing: {cmap}")
    allow_esc = str(allow).replace("\\", "/").replace("'", "''")
    cmap_esc = str(cmap).replace("\\", "/").replace("'", "''")
    dst_esc = str(dst).replace("\\", "/").replace("'", "''")
    sql = f"""
COPY (
  SELECT DISTINCT TRIM(CAST(m.canonical_id AS VARCHAR)) AS canonical_id
  FROM read_parquet('{allow_esc}') AS a
  INNER JOIN (
    SELECT DISTINCT
      TRY_CAST(player_id AS BIGINT) AS player_id,
      TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
    FROM read_parquet('{cmap_esc}')
    WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
      AND TRIM(CAST(canonical_id AS VARCHAR)) <> ''
  ) AS m ON TRY_CAST(a.player_id AS BIGINT) = m.player_id
  WHERE TRIM(CAST(m.canonical_id AS VARCHAR)) <> ''
  ORDER BY canonical_id
) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(sql)
    finally:
        con.close()
    nrows = int(pq.ParquetFile(dst).metadata.num_rows)
    return {"canonical_ids": nrows, "path": str(dst)}


def _add_event_timestamp_column(full_snap: Path, feast_out: Path) -> int:
    """Collapse to latest anchor per canonical_id and write Feast-compatible Parquet."""
    src = str(Path(full_snap).resolve()).replace("\\", "/").replace("'", "''")
    dst = Path(feast_out).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst_esc = str(dst).replace("\\", "/").replace("'", "''")
    feat_cols = ", ".join(f'"{c}"' for c in SPIKE_MID_TERM_FEATURE_COLUMNS)
    sql = f"""
COPY (
  SELECT
    canonical_id,
    {feat_cols},
    CAST(
      (CAST(anchor_gaming_day_event AS TIMESTAMP) + INTERVAL '1' DAY - INTERVAL '1' SECOND)
      AS TIMESTAMPTZ
    ) AS event_timestamp
  FROM (
    SELECT
      TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id,
      CAST(anchor_gaming_day_event AS DATE) AS anchor_gaming_day_event,
      {feat_cols},
      ROW_NUMBER() OVER (
        PARTITION BY TRIM(CAST(canonical_id AS VARCHAR))
        ORDER BY CAST(anchor_gaming_day_event AS DATE) DESC
      ) AS rn
    FROM read_parquet('{src}')
  ) AS ranked
  WHERE rn = 1
) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(sql)
        nrows = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{dst_esc}')").fetchone()[0])
    finally:
        con.close()
    return nrows


def compute_mid_term_spike_snapshot(
    *,
    cleaned_bet_path: Path,
    cfg: FeastMidTermSpikeConfig,
    canonical_universe_parquet: Path | None,
    anchor_start: date,
    anchor_end: date,
    bets_gday_start: date,
    bets_gday_end: date,
) -> tuple[Path, dict[str, Any]]:
    """Run mid-term materializer and write Feast spike Parquet."""
    staging = Path(cfg.staging_dir or (default_spike_config().staging_dir)).resolve()
    staging.mkdir(parents=True, exist_ok=True)
    full_snap = staging / "mid_term_full.parquet"
    cmap = (
        Path(cfg.canonical_mapping_parquet).resolve()
        if cfg.canonical_mapping_parquet is not None
        else default_canonical_mapping_parquet_path().resolve()
    )
    t0 = time.perf_counter()
    _, meta = materialize_mid_term_daily_snapshot(
        cleaned_bet_parquet=cleaned_bet_path,
        out_parquet=full_snap,
        duckdb_runtime=cfg.duckdb_runtime,
        canonical_mapping_parquet=cmap,
        canonical_universe_parquet=canonical_universe_parquet,
        anchor_gaming_day_event_start=anchor_start,
        anchor_gaming_day_event_end=anchor_end,
        bets_gaming_day_start=bets_gday_start,
        bets_gaming_day_end=bets_gday_end,
        snapshot_scope=MID_TERM_SNAPSHOT_SCOPE_PRODUCTION,
    )
    compute_sec = round(time.perf_counter() - t0, 3)
    feast_out = Path(cfg.spike_parquet or default_spike_config().spike_parquet).resolve()
    feast_rows = _add_event_timestamp_column(full_snap, feast_out)
    return feast_out, {
        **meta,
        "compute_seconds_total": compute_sec,
        "feast_spike_rows": feast_rows,
        "feast_spike_parquet": str(feast_out),
        "full_snapshot_parquet": str(full_snap),
    }


def run_feast_apply(feast_repo: Path) -> float:
    """Run ``feast apply`` in the feature repo; return wall-clock seconds."""
    repo = Path(feast_repo).resolve()
    feast_bin = shutil.which("feast")
    if feast_bin is None:
        raise RuntimeError("feast CLI not found on PATH; install feast==0.63.x")
    t0 = time.perf_counter()
    proc = subprocess.run(
        [feast_bin, "apply"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = round(time.perf_counter() - t0, 3)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        raise RuntimeError(f"feast apply failed (exit {proc.returncode}): {tail}")
    return elapsed


def run_feast_materialize(
    feast_repo: Path,
    *,
    start: datetime,
    end: datetime,
) -> float:
    """Materialize spike FeatureView into the local online store."""
    from feast import FeatureStore

    repo = Path(feast_repo).resolve()
    t0 = time.perf_counter()
    store = FeatureStore(repo_path=str(repo))
    store.materialize(
        feature_views=[SPIKE_FEATURE_VIEW_NAME],
        start_date=start,
        end_date=end,
    )
    return round(time.perf_counter() - t0, 3)


def _pick_probe_bet(
    cleaned_bet_path: Path,
    feast_snap: Path,
    mapping_parquet: Path,
) -> dict[str, Any]:
    """Pick one bet row whose canonical_id exists in the spike snapshot."""
    bet_esc = str(Path(cleaned_bet_path).resolve()).replace("\\", "/").replace("'", "''")
    snap_esc = str(Path(feast_snap).resolve()).replace("\\", "/").replace("'", "''")
    cmap_esc = str(Path(mapping_parquet).resolve()).replace("\\", "/").replace("'", "''")
    row = duckdb.sql(
        f"""
        WITH snap AS (
          SELECT DISTINCT TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
          FROM read_parquet('{snap_esc}')
        ),
        bets AS (
          SELECT
            TRY_CAST(b.player_id AS BIGINT) AS player_id,
            CAST(b.gaming_day_event AS DATE) AS gaming_day_event,
            COALESCE(
              TRIM(CAST(c.canonical_id AS VARCHAR)),
              CAST(TRY_CAST(b.player_id AS BIGINT) AS VARCHAR)
            ) AS canonical_id
          FROM read_parquet('{bet_esc}') AS b
          LEFT JOIN read_parquet('{cmap_esc}') AS c
            ON TRY_CAST(b.player_id AS BIGINT) = TRY_CAST(c.player_id AS BIGINT)
          WHERE b.gaming_day_event IS NOT NULL
        )
        SELECT b.player_id, b.gaming_day_event, b.canonical_id
        FROM bets AS b
        INNER JOIN snap AS s ON b.canonical_id = s.canonical_id
        ORDER BY b.gaming_day_event DESC
        LIMIT 1
        """,
    ).fetchone()
    if row is None:
        raise ValueError("no probe bet found with canonical_id present in spike snapshot")
    return {
        "player_id": int(row[0]),
        "gaming_day_event": row[1].isoformat() if hasattr(row[1], "isoformat") else str(row[1]),
        "canonical_id": str(row[2]),
    }


def _feast_entity_rows(canonical_ids: list[str]) -> dict[str, list[str]]:
    """Build ``entity_rows`` for Feast 0.63 (dict-of-lists, not a pandas DataFrame)."""
    if not canonical_ids:
        raise ValueError("canonical_ids must be non-empty")
    return {"canonical_id": [str(x) for x in canonical_ids]}


def run_online_lookup_smoke(
    feast_repo: Path,
    *,
    canonical_ids: list[str],
    batch_size: int,
) -> dict[str, Any]:
    """Measure batched ``get_online_features`` latency and missing rate."""
    from feast import FeatureStore

    if not canonical_ids:
        raise ValueError("canonical_ids must be non-empty")
    store = FeatureStore(repo_path=str(Path(feast_repo).resolve()))
    missing_counts: dict[str, int] = {c: 0 for c in SPIKE_MID_TERM_FEATURE_COLUMNS}
    batch_ids = [str(x) for x in canonical_ids[: max(1, batch_size)]]
    t0 = time.perf_counter()
    out = store.get_online_features(
        features=list(SPIKE_ONLINE_FEATURE_REFS),
        entity_rows=_feast_entity_rows(batch_ids),
    ).to_df()
    batch_latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    n_ok = 0
    if out.empty:
        for col in SPIKE_MID_TERM_FEATURE_COLUMNS:
            missing_counts[col] += len(batch_ids)
    else:
        for _, row in out.iterrows():
            ok_row = True
            for col in SPIKE_MID_TERM_FEATURE_COLUMNS:
                if col not in row.index or pd.isna(row[col]):
                    missing_counts[col] += 1
                    ok_row = False
            if ok_row:
                n_ok += 1
    per_entity_ms = round(batch_latency_ms / max(1, len(batch_ids)), 3)
    return {
        "lookup_batch_size": len(batch_ids),
        "lookup_ok_rows": n_ok,
        "lookup_missing_by_feature": missing_counts,
        "lookup_latency_ms_batch": batch_latency_ms,
        "lookup_latency_ms_per_entity": per_entity_ms,
        "lookup_latency_ms_p50": batch_latency_ms,
        "lookup_latency_ms_p95": batch_latency_ms,
    }


def _verdict_from_metrics(metrics: dict[str, Any]) -> str:
    """Heuristic pass / marginal / fail label for the spike report."""
    compute_sec = float(metrics.get("compute_seconds_total") or 0.0)
    p95 = metrics.get("lookup_latency_ms_p95")
    missing = metrics.get("lookup_missing_by_feature") or {}
    total_missing = sum(int(v) for v in missing.values())
    if compute_sec > 3600:
        return "fail"
    if total_missing > 0:
        return "marginal"
    if p95 is not None and float(p95) > 500.0:
        return "marginal"
    if compute_sec > 600:
        return "marginal"
    return "pass"


def run_spike(cfg: FeastMidTermSpikeConfig | None = None) -> dict[str, Any]:
    """Execute the full spike pipeline and return the report dict."""
    spike_cfg = cfg or default_spike_config()
    anchor_start, anchor_end, bets_start, bets_end = _anchor_bounds(
        anchor_days=spike_cfg.anchor_days,
    )
    staging = Path(spike_cfg.staging_dir).resolve()
    staging.mkdir(parents=True, exist_ok=True)
    cmap = (
        Path(spike_cfg.canonical_mapping_parquet).resolve()
        if spike_cfg.canonical_mapping_parquet is not None
        else default_canonical_mapping_parquet_path().resolve()
    )
    universe_path: Path | None = None
    player_ids: frozenset[int] | None = None
    if spike_cfg.sample_mode == "small_sample":
        allow_path = (
            Path(spike_cfg.adt_allowlist_parquet).resolve()
            if spike_cfg.adt_allowlist_parquet is not None
            else resolve_adt_allowlist_path(default_hightier_serving_config(), manifest=None)
        )
        player_ids = load_adt_allowlist_ids(allow_path)
        universe_path = staging / "canonical_universe.parquet"
        build_canonical_universe_from_allowlist(allow_path, cmap, universe_path)

    report: dict[str, Any] = {
        "spike": "feast_mid_term",
        "sample_mode": spike_cfg.sample_mode,
        "bet_source": spike_cfg.bet_source,
        "anchor_gaming_day_event_start": anchor_start.isoformat(),
        "anchor_gaming_day_event_end": anchor_end.isoformat(),
        "feature_columns": list(SPIKE_MID_TERM_FEATURE_COLUMNS),
    }

    if spike_cfg.bet_source == "clickhouse":
        staged_bet = staging / "ch_bets_export.parquet"
        report["clickhouse_export"] = export_clickhouse_bets_to_parquet(
            staged_bet,
            bets_gaming_day_start=bets_start,
            bets_gaming_day_end=bets_end,
            player_ids=player_ids,
        )
        cleaned_path = staged_bet
    elif spike_cfg.bet_source == "local_cleaned":
        cleaned_path = Path(
            spike_cfg.local_cleaned_bet or default_spike_config().local_cleaned_bet,
        ).resolve()
        if not cleaned_path.exists():
            raise FileNotFoundError(f"local cleaned bet path missing: {cleaned_path}")
        report["clickhouse_export"] = {"source": "local_cleaned", "path": str(cleaned_path)}
    else:
        raise ValueError(f"unsupported bet_source={spike_cfg.bet_source!r}")

    feast_path, compute_meta = compute_mid_term_spike_snapshot(
        cleaned_bet_path=cleaned_path,
        cfg=spike_cfg,
        canonical_universe_parquet=universe_path,
        anchor_start=anchor_start,
        anchor_end=anchor_end,
        bets_gday_start=bets_start,
        bets_gday_end=bets_end,
    )
    report.update(compute_meta)

    feast_repo = Path(spike_cfg.feast_repo or default_spike_config().feast_repo).resolve()
    report["feast_apply_seconds"] = run_feast_apply(feast_repo)

    # Materialize window spans all event_timestamp values in the spike parquet.
    ts_row = duckdb.sql(
        f"""
        SELECT MIN(event_timestamp), MAX(event_timestamp)
        FROM read_parquet('{str(feast_path).replace(chr(92), "/")}')
        """,
    ).fetchone()
    if ts_row is None or ts_row[0] is None:
        raise ValueError("spike parquet has no event_timestamp rows")
    start_dt = ts_row[0].to_pydatetime() if hasattr(ts_row[0], "to_pydatetime") else ts_row[0]
    end_dt = ts_row[1].to_pydatetime() if hasattr(ts_row[1], "to_pydatetime") else ts_row[1]
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    report["feast_materialize_seconds"] = run_feast_materialize(
        feast_repo,
        start=start_dt - timedelta(hours=1),
        end=end_dt + timedelta(hours=1),
    )

    probe = _pick_probe_bet(cleaned_path, feast_path, cmap)
    report["probe_bet"] = probe
    cids = duckdb.sql(
        f"SELECT DISTINCT canonical_id FROM read_parquet('{str(feast_path).replace(chr(92), '/')}')",
    ).fetchdf()["canonical_id"].astype(str).tolist()
    report.update(
        run_online_lookup_smoke(
            feast_repo,
            canonical_ids=cids,
            batch_size=spike_cfg.lookup_batch_size,
        ),
    )
    report["verdict"] = _verdict_from_metrics(report)

    out_report = Path(spike_cfg.report_path or default_spike_config().report_path).resolve()
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    report["report_path"] = str(out_report)
    from trainer_hightier.serving.feast_readiness import update_readiness_layer_from_spike_report

    update_readiness_layer_from_spike_report(report, layer="mid_term", feast_repo=feast_repo)
    logger.info("[feast_mid_term_spike] verdict=%s report=%s", report["verdict"], out_report)
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entry for the Feast mid-term feasibility spike."""
    parser = argparse.ArgumentParser(description="Feast mid-term feasibility spike")
    parser.add_argument(
        "--sample-mode",
        choices=("small_sample", "wider_sample"),
        default="small_sample",
    )
    parser.add_argument(
        "--bet-source",
        choices=("clickhouse", "local_cleaned"),
        default="clickhouse",
    )
    parser.add_argument("--anchor-days", type=int, default=3)
    parser.add_argument("--allowlist-parquet", type=Path, default=None)
    parser.add_argument("--canonical-mapping", type=Path, default=None)
    parser.add_argument("--local-cleaned-bet", type=Path, default=None)
    parser.add_argument("--report-path", type=Path, default=None)
    parser.add_argument(
        "--lookup-batch-size",
        type=int,
        default=20,
        help="Number of canonical_id values in one get_online_features batch (default 20)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    base = default_spike_config()
    cfg = FeastMidTermSpikeConfig(
        feast_repo=base.feast_repo,
        spike_parquet=base.spike_parquet,
        staging_dir=base.staging_dir,
        report_path=args.report_path or base.report_path,
        canonical_mapping_parquet=args.canonical_mapping,
        adt_allowlist_parquet=args.allowlist_parquet,
        anchor_days=int(args.anchor_days),
        bet_source=str(args.bet_source),
        local_cleaned_bet=args.local_cleaned_bet or base.local_cleaned_bet,
        sample_mode=str(args.sample_mode),
        lookup_batch_size=max(1, int(args.lookup_batch_size)),
    )
    run_spike(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
