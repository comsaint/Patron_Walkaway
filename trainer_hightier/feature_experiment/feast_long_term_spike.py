"""Feast feasibility spike: canonical long-term (180d monthly) slow patron features.

Experimental probe only. Uses ADT allowlist scope, exports session history from
ClickHouse (or local cleaned session parquet), materializes canonical monthly
snapshots, and measures Feast online lookup latency.
"""

from __future__ import annotations

import argparse
import json
import logging
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
    configs_from_run_profile,
    default_hightier_serving_config,
    get_run_profile,
)
from trainer_hightier.feature_experiment.feast_mid_term_spike import (
    _feast_entity_rows,
    _split_player_id_chunks,
    run_feast_apply,
)
from trainer_hightier.serving.adt_allowlist import load_adt_allowlist_ids, resolve_adt_allowlist_path
from trainer_hightier.serving.ch_adapter import get_clickhouse_client
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path
from trainer_hightier.utils.slow_patron_180d_monthly import (
    default_cleaned_session_parquet_path,
    materialize_slow_patron_180d_canonical_asof,
)

logger = logging.getLogger(__name__)

SPIKE_LONG_TERM_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "patron__theo_win_sum__w180d_m1snap",
    "patron__gaming_days_cnt__w180d_m1snap",
    "patron__adt__w180d_m1snap",
)
SPIKE_FEATURE_VIEW_NAME: Final[str] = "long_term_slow_spike_features"
SPIKE_ONLINE_FEATURE_REFS: Final[tuple[str, ...]] = tuple(
    f"{SPIKE_FEATURE_VIEW_NAME}:{c}" for c in SPIKE_LONG_TERM_FEATURE_COLUMNS
)


@dataclass(frozen=True)
class FeastLongTermSpikeConfig:
    """Long-term spike runner settings (edit here; no environment-variable SSOT)."""

    feast_repo: Path | None = None
    spike_parquet: Path | None = None
    staging_dir: Path | None = None
    report_path: Path | None = None
    canonical_mapping_parquet: Path | None = None
    adt_allowlist_parquet: Path | None = None
    duckdb_runtime: DuckDbRuntimeConfig = field(
        default_factory=lambda: configs_from_run_profile(get_run_profile("laptop_8g"))[0],
    )
    lookback_days: int = 180
    lookup_batch_size: int = 1000
    session_source: str = "clickhouse"
    local_cleaned_session: Path | None = None


def default_long_term_spike_config() -> FeastLongTermSpikeConfig:
    """Default artifact paths under ``trainer_hightier/artifacts/feast``."""
    pkg = Path(__file__).resolve().parents[1]
    feast_art = pkg / "artifacts" / "feast"
    cfg = default_hightier_serving_config()
    return FeastLongTermSpikeConfig(
        feast_repo=pkg / "feast_repo",
        spike_parquet=feast_art / "slow_patron_180d_monthly.parquet",
        staging_dir=feast_art / "long_term_spike_staging",
        report_path=feast_art / "long_term_spike_report.json",
        local_cleaned_session=default_cleaned_session_parquet_path(),
        lookback_days=int(cfg.production_slow_lookback_days),
    )


def _session_gaming_day_bounds(*, lookback_days: int) -> tuple[date, date]:
    """Return ``gaming_day_start``, ``gaming_day_end`` (HK calendar, end = yesterday)."""
    if lookback_days < 1:
        raise ValueError(f"lookback_days must be >= 1, got {lookback_days!r}")
    hk = ZoneInfo(HK_TZ)
    gaming_day_end = datetime.now(hk).date() - timedelta(days=1)
    gaming_day_start = gaming_day_end - timedelta(days=int(lookback_days) - 1)
    return gaming_day_start, gaming_day_end


def _sanitize_ch_session_export_df(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce raw ClickHouse session rows for ``materialize_slow_patron_180d_canonical_asof``."""
    required = ("player_id", "gaming_day", "theo_win")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"session export missing columns {missing}; got {list(df.columns)[:20]}")
    if df.empty:
        return df
    out = df.loc[:, list(required)].copy()
    out["player_id"] = pd.to_numeric(out["player_id"], errors="coerce")
    out["theo_win"] = pd.to_numeric(out["theo_win"], errors="coerce")
    out["gaming_day"] = pd.to_datetime(out["gaming_day"], errors="coerce").dt.normalize()
    out = out.loc[out["player_id"].notna() & out["gaming_day"].notna()].copy()
    out["player_id"] = out["player_id"].astype("int64", copy=False)
    return out


def export_clickhouse_sessions_to_parquet(
    out_parquet: Path,
    *,
    gaming_day_start: date,
    gaming_day_end: date,
    player_ids: frozenset[int],
) -> dict[str, Any]:
    """Export minimal session columns for allowed players from ClickHouse."""
    if not player_ids:
        raise ValueError("player_ids is empty; long-term spike requires ADT allowlist scope")
    cfg = default_hightier_serving_config()
    client = get_clickhouse_client()
    placeholder = int(cfg.placeholder_player_id)
    out_parquet = Path(out_parquet).resolve()
    out_parquet.parent.mkdir(parents=True, exist_ok=True)
    chunk_size = int(cfg.hightier_scorer_player_id_chunk_size)
    chunks = _split_player_id_chunks(player_ids, chunk_size)
    frames: list[pd.DataFrame] = []
    row_cap = int(cfg.hightier_scorer_chunk_merge_row_cap)
    t0 = time.perf_counter()
    for i, chunk in enumerate(chunks):
        in_list = ",".join(str(int(x)) for x in chunk)
        q = f"""
            SELECT
                player_id,
                gaming_day,
                theo_win
            FROM {cfg.source_db}.{cfg.tsession} FINAL
            WHERE gaming_day >= %(g_start)s
              AND gaming_day <= %(g_end)s
              AND gaming_day IS NOT NULL
              AND player_id IS NOT NULL
              AND player_id != {placeholder}
              AND COALESCE(is_deleted, 0) = 0
              AND COALESCE(is_canceled, 0) = 0
              AND player_id IN ({in_list})
        """
        frames.append(
            client.query_df(
                q,
                parameters={"g_start": gaming_day_start, "g_end": gaming_day_end},
            ),
        )
        if row_cap > 0:
            rows_so_far = sum(len(frame) for frame in frames)
            if rows_so_far > row_cap:
                raise RuntimeError(
                    "feast long-term spike ClickHouse export exceeds "
                    f"hightier_scorer_chunk_merge_row_cap={row_cap} "
                    f"({rows_so_far} rows after chunk {i + 1}/{len(chunks)})"
                )
    nonempty = [frame for frame in frames if frame is not None and not frame.empty]
    raw = pd.concat(nonempty, ignore_index=True) if nonempty else pd.DataFrame()
    elapsed = round(time.perf_counter() - t0, 3)
    raw_rows = int(len(raw))
    df = _sanitize_ch_session_export_df(raw)
    if df.empty:
        raise ValueError(
            f"ClickHouse session export returned 0 rows for gaming_day in "
            f"[{gaming_day_start}, {gaming_day_end}]"
        )
    df.to_parquet(out_parquet, index=False)
    return {
        "source": "clickhouse",
        "rows_exported": int(len(df)),
        "rows_raw_from_clickhouse": raw_rows,
        "rows_dropped_on_sanitize": max(0, raw_rows - len(df)),
        "export_seconds": elapsed,
        "query_count": len(chunks),
        "player_id_chunk_count": len(chunks),
        "player_id_chunk_size": chunk_size,
        "path": str(out_parquet),
        "gaming_day_start": gaming_day_start.isoformat(),
        "gaming_day_end": gaming_day_end.isoformat(),
    }


def _write_feast_spike_parquet(full_snap: Path, feast_out: Path) -> int:
    """Collapse to latest monthly anchor per canonical_id for Feast online store."""
    src = str(Path(full_snap).resolve()).replace("\\", "/").replace("'", "''")
    dst = Path(feast_out).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst_esc = str(dst).replace("\\", "/").replace("'", "''")
    feat_cols = ", ".join(f'"{c}"' for c in SPIKE_LONG_TERM_FEATURE_COLUMNS)
    sql = f"""
COPY (
  SELECT
    canonical_id,
    {feat_cols},
    CAST(
      (CAST(anchor_gaming_day AS TIMESTAMP) + INTERVAL '1' DAY - INTERVAL '1' SECOND)
      AS TIMESTAMPTZ
    ) AS event_timestamp
  FROM (
    SELECT
      TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id,
      CAST(anchor_gaming_day AS DATE) AS anchor_gaming_day,
      {feat_cols},
      ROW_NUMBER() OVER (
        PARTITION BY TRIM(CAST(canonical_id AS VARCHAR))
        ORDER BY CAST(anchor_gaming_day AS DATE) DESC
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


def compute_long_term_spike_snapshot(
    *,
    session_path: Path,
    cfg: FeastLongTermSpikeConfig,
    lookback_days: int,
) -> tuple[Path, dict[str, Any]]:
    """Materialize slow patron canonical ASOF snapshot and Feast spike parquet."""
    staging = Path(cfg.staging_dir or default_long_term_spike_config().staging_dir).resolve()
    staging.mkdir(parents=True, exist_ok=True)
    full_snap = staging / "slow_patron_full.parquet"
    cmap = (
        Path(cfg.canonical_mapping_parquet).resolve()
        if cfg.canonical_mapping_parquet is not None
        else default_canonical_mapping_parquet_path().resolve()
    )
    t0 = time.perf_counter()
    materialize_slow_patron_180d_canonical_asof(
        cleaned_session_parquet=session_path,
        canonical_mapping_parquet=cmap,
        out_parquet=full_snap,
        lookback_days=int(lookback_days),
        duckdb_runtime=cfg.duckdb_runtime,
    )
    compute_sec = round(time.perf_counter() - t0, 3)
    feast_out = Path(cfg.spike_parquet or default_long_term_spike_config().spike_parquet).resolve()
    feast_rows = _write_feast_spike_parquet(full_snap, feast_out)
    pf = pq.ParquetFile(full_snap)
    nrows = int(pf.metadata.num_rows) if pf.metadata is not None else 0
    anchor_max = None
    if nrows > 0:
        row = duckdb.sql(
            f"SELECT MAX(CAST(anchor_gaming_day AS DATE)) FROM read_parquet('{str(full_snap).replace(chr(92), '/')}')",
        ).fetchone()
        if row and row[0] is not None:
            anchor_max = row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0])[:10]
    return feast_out, {
        "compute_seconds_total": compute_sec,
        "slow_snapshot_rows": nrows,
        "feast_spike_rows": feast_rows,
        "feast_spike_parquet": str(feast_out),
        "full_snapshot_parquet": str(full_snap),
        "slow_anchor_gaming_day_max": anchor_max,
        "lookback_days": int(lookback_days),
    }


def run_feast_materialize(
    feast_repo: Path,
    *,
    start: datetime,
    end: datetime,
) -> float:
    """Materialize long-term spike FeatureView into the local online store."""
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
    missing_counts: dict[str, int] = {c: 0 for c in SPIKE_LONG_TERM_FEATURE_COLUMNS}
    batch_ids = [str(x) for x in canonical_ids[: max(1, batch_size)]]
    t0 = time.perf_counter()
    out = store.get_online_features(
        features=list(SPIKE_ONLINE_FEATURE_REFS),
        entity_rows=_feast_entity_rows(batch_ids),
    ).to_df()
    batch_latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    n_ok = 0
    if out.empty:
        for col in SPIKE_LONG_TERM_FEATURE_COLUMNS:
            missing_counts[col] += len(batch_ids)
    else:
        for _, row in out.iterrows():
            ok_row = True
            for col in SPIKE_LONG_TERM_FEATURE_COLUMNS:
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
    """Heuristic pass / marginal / fail for long-term feasibility."""
    compute_sec = float(metrics.get("compute_seconds_total") or 0.0)
    export_sec = float((metrics.get("clickhouse_export") or {}).get("export_seconds") or 0.0)
    total_sec = compute_sec + export_sec
    missing = metrics.get("lookup_missing_by_feature") or {}
    total_missing = sum(int(v) for v in missing.values())
    if total_sec > 3600:
        return "fail"
    if total_sec > 1800:
        return "marginal"
    if total_missing > 0:
        return "marginal"
    return "pass"


def run_spike(cfg: FeastLongTermSpikeConfig | None = None) -> dict[str, Any]:
    """Execute the long-term Feast feasibility spike and return the report dict."""
    spike_cfg = cfg or default_long_term_spike_config()
    g_start, g_end = _session_gaming_day_bounds(lookback_days=spike_cfg.lookback_days)
    staging = Path(spike_cfg.staging_dir).resolve()
    staging.mkdir(parents=True, exist_ok=True)
    cmap = (
        Path(spike_cfg.canonical_mapping_parquet).resolve()
        if spike_cfg.canonical_mapping_parquet is not None
        else default_canonical_mapping_parquet_path().resolve()
    )
    allow_path = (
        Path(spike_cfg.adt_allowlist_parquet).resolve()
        if spike_cfg.adt_allowlist_parquet is not None
        else resolve_adt_allowlist_path(default_hightier_serving_config(), manifest=None)
    )
    player_ids = load_adt_allowlist_ids(allow_path)

    report: dict[str, Any] = {
        "spike": "feast_long_term",
        "scope": "adt_allowlist",
        "session_source": spike_cfg.session_source,
        "lookback_days": int(spike_cfg.lookback_days),
        "gaming_day_start": g_start.isoformat(),
        "gaming_day_end": g_end.isoformat(),
        "feature_columns": list(SPIKE_LONG_TERM_FEATURE_COLUMNS),
    }

    if spike_cfg.session_source == "clickhouse":
        staged_sess = staging / "ch_session_export.parquet"
        report["clickhouse_export"] = export_clickhouse_sessions_to_parquet(
            staged_sess,
            gaming_day_start=g_start,
            gaming_day_end=g_end,
            player_ids=player_ids,
        )
        session_path = staged_sess
    elif spike_cfg.session_source == "local_cleaned":
        session_path = Path(
            spike_cfg.local_cleaned_session or default_long_term_spike_config().local_cleaned_session,
        ).resolve()
        if not session_path.is_file():
            raise FileNotFoundError(f"local cleaned session parquet missing: {session_path}")
        report["clickhouse_export"] = {"source": "local_cleaned", "path": str(session_path)}
    else:
        raise ValueError(f"unsupported session_source={spike_cfg.session_source!r}")

    feast_path, compute_meta = compute_long_term_spike_snapshot(
        session_path=session_path,
        cfg=spike_cfg,
        lookback_days=spike_cfg.lookback_days,
    )
    report.update(compute_meta)

    feast_repo = Path(spike_cfg.feast_repo or default_long_term_spike_config().feast_repo).resolve()
    report["feast_apply_seconds"] = run_feast_apply(feast_repo)

    ts_row = duckdb.sql(
        f"""
        SELECT MIN(event_timestamp), MAX(event_timestamp)
        FROM read_parquet('{str(feast_path).replace(chr(92), "/")}')
        """,
    ).fetchone()
    if ts_row is None or ts_row[0] is None:
        raise ValueError("long-term spike parquet has no event_timestamp rows")
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

    out_report = Path(spike_cfg.report_path or default_long_term_spike_config().report_path).resolve()
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    report["report_path"] = str(out_report)
    from trainer_hightier.serving.feast_readiness import update_readiness_layer_from_spike_report

    update_readiness_layer_from_spike_report(report, layer="slow_patron", feast_repo=feast_repo)
    logger.info("[feast_long_term_spike] verdict=%s report=%s", report["verdict"], out_report)
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entry for the Feast long-term (180d) feasibility spike."""
    parser = argparse.ArgumentParser(description="Feast long-term slow patron feasibility spike")
    parser.add_argument(
        "--session-source",
        choices=("clickhouse", "local_cleaned"),
        default="clickhouse",
    )
    parser.add_argument("--lookback-days", type=int, default=None)
    parser.add_argument("--allowlist-parquet", type=Path, default=None)
    parser.add_argument("--canonical-mapping", type=Path, default=None)
    parser.add_argument("--local-cleaned-session", type=Path, default=None)
    parser.add_argument("--report-path", type=Path, default=None)
    parser.add_argument(
        "--lookup-batch-size",
        type=int,
        default=1000,
        help="canonical_id count in one get_online_features batch",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    base = default_long_term_spike_config()
    cfg = FeastLongTermSpikeConfig(
        feast_repo=base.feast_repo,
        spike_parquet=base.spike_parquet,
        staging_dir=base.staging_dir,
        report_path=args.report_path or base.report_path,
        canonical_mapping_parquet=args.canonical_mapping,
        adt_allowlist_parquet=args.allowlist_parquet,
        lookback_days=int(args.lookback_days if args.lookback_days is not None else base.lookback_days),
        session_source=str(args.session_source),
        local_cleaned_session=args.local_cleaned_session or base.local_cleaned_session,
        lookup_batch_size=max(1, int(args.lookup_batch_size)),
    )
    run_spike(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
