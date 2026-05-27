"""Production Feast online refresh orchestration (ClickHouse -> materialize -> Feast online)."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

from trainer_hightier.config import (
    HK_TZ,
    MID_TERM_BOOTSTRAP_SEED_PARQUET_BASENAME,
    MID_TERM_SNAPSHOT_DEPLOY_PARQUET_BASENAME,
    MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS,
    default_hightier_serving_config,
)
from trainer_hightier.serving.feast_production_constants import (
    LONG_SPIKE_FEATURE_VIEW_NAME,
    MID_SPIKE_FEATURE_VIEW_NAME,
    PRODUCTION_LONG_TERM_FEATURE_COLUMNS,
    PRODUCTION_MID_TERM_FEATURE_COLUMNS,
)
from trainer_hightier.serving.adt_allowlist import load_adt_allowlist_ids, resolve_adt_allowlist_path
from trainer_hightier.serving.ch_adapter import (
    CH_TBET_PAYOUT_ODDS_SELECT,
    CH_TBET_WAGER_POSITIVE_PRED,
    CH_TBET_WAGER_SELECT,
    get_clickhouse_client,
)
from trainer_hightier.serving.feast_online_adapter import (
    default_feast_repo_path,
    ensure_feast_schema_ready,
    feast_registry_missing,
    feast_schema_drift_issues,
    read_feast_parquet_max_event_timestamp,
    reset_feast_repo_runtime_state,
    resolve_feast_artifacts_dir,
)
from trainer_hightier.serving.feast_readiness import (
    FEAST_READINESS_SCOPE_PRODUCTION,
    FeastLayerReadiness,
    FeastOnlineReadiness,
    layer_readiness_from_production_mid_meta,
    layer_readiness_from_production_slow_meta,
    load_feast_online_readiness,
    merge_layer_readiness,
    mid_feast_coverage_telemetry,
    resolve_feast_readiness_path,
    run_allowlist_feast_lookup_smoke,
    evaluate_feast_lookup_smoke_gate,
    write_feast_online_readiness,
)
from trainer_hightier.serving.feature_state_store import (
    feast_refresh_run_finish,
    feast_refresh_run_start,
    init_feature_state_db,
    persist_feast_online_readiness_latest,
    upsert_feast_refresh_layer,
)
from trainer_hightier.serving.production_materialize import (
    materialize_production_mid_term_daily_snapshot,
    materialize_production_slow_canonical_asof,
    resolve_production_canonical_mapping,
)
from trainer_hightier.serving.snapshot_freshness import (
    expected_mid_term_anchor,
    mid_feast_event_timestamp_for_anchor,
    serving_gaming_day,
)
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path

logger = logging.getLogger(__name__)

_SUPPORTED_LAYERS: Final[frozenset[str]] = frozenset({"mid", "slow"})


@dataclass(frozen=True)
class RefreshOptions:
    """Resolved CLI options for one Feast online refresh run."""

    layers: frozenset[str]
    source: str
    skip_apply: bool
    skip_materialize: bool
    smoke_only: bool
    dry_run: bool
    feast_repo: Path
    readiness_path: Path
    canonical_mapping: Path
    adt_allowlist: Path
    local_cleaned_bet: Path | None
    local_cleaned_session: Path | None
    max_smoke_entities: int
    summary_path: Path
    bootstrap_mid: bool
    apply_schema: bool
    training_mid_snapshot_parquet: Path | None = None
    use_training_mid_seed: bool = True


@dataclass
class LayerRefreshOutcome:
    """One layer refresh result for orchestration and audit."""

    layer: str
    status: str
    meta: dict[str, Any]
    export_meta: dict[str, Any]
    artifact_path: Path
    feast_parquet_path: Path
    compute_seconds: float
    detail: dict[str, Any]


def _utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def parse_refresh_layers(raw: str) -> frozenset[str]:
    """Parse comma-separated layer names; fail on unsupported values."""
    parts = {p.strip().lower() for p in str(raw).split(",") if p.strip()}
    if not parts:
        raise ValueError("layers must be non-empty, e.g. mid,slow")
    unknown = sorted(parts - _SUPPORTED_LAYERS)
    if unknown:
        raise ValueError(f"unsupported layers {unknown}; supported={sorted(_SUPPORTED_LAYERS)}")
    return frozenset(parts)


def _split_player_id_chunks(ids: frozenset[int], chunk_size: int) -> list[list[int]]:
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size!r}")
    if not ids:
        return []
    sorted_ids = sorted(int(x) for x in ids)
    return [sorted_ids[i : i + chunk_size] for i in range(0, len(sorted_ids), chunk_size)]


def _path_esc(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/").replace("'", "''")


def count_allowlist_canonical_ids(
    allowlist_parquet: Path,
    canonical_mapping_parquet: Path,
) -> int:
    """Count distinct canonical ids reachable from the ADT allowlist."""
    allow_esc = _path_esc(allowlist_parquet)
    map_esc = _path_esc(canonical_mapping_parquet)
    row = duckdb.sql(
        f"""
        SELECT COUNT(DISTINCT TRIM(CAST(m.canonical_id AS VARCHAR)))
        FROM read_parquet('{allow_esc}') AS a
        INNER JOIN read_parquet('{map_esc}') AS m
          ON CAST(a.player_id AS BIGINT) = CAST(m.player_id AS BIGINT)
        WHERE m.canonical_id IS NOT NULL
          AND TRIM(CAST(m.canonical_id AS VARCHAR)) != ''
        """,
    ).fetchone()
    return int(row[0] if row and row[0] is not None else 0)


def _mid_export_bounds(
    *,
    close_hour: int,
    bootstrap_mid: bool = False,
    bootstrap_anchor_days: int | None = None,
) -> tuple[date, date, date, date]:
    """Return anchor_start, anchor_end, bets_gday_start, bets_gday_end."""
    cfg = default_hightier_serving_config()
    serving_day = serving_gaming_day(close_hour=close_hour)
    anchor_end = expected_mid_term_anchor(serving_day)
    if bootstrap_mid:
        days = int(
            bootstrap_anchor_days
            if bootstrap_anchor_days is not None
            else cfg.production_mid_feast_bootstrap_anchor_days
        )
        if days < 1:
            raise ValueError(f"bootstrap_anchor_days must be >= 1, got {days!r}")
        anchor_start = anchor_end - timedelta(days=days - 1)
    else:
        anchor_start = anchor_end
    lb = int(MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS)
    bets_gday_end = anchor_end
    bets_gday_start = anchor_start - timedelta(days=lb - 1)
    return anchor_start, anchor_end, bets_gday_start, bets_gday_end


def _slow_export_bounds(*, close_hour: int, lookback_days: int) -> tuple[date, date]:
    serving_day = serving_gaming_day(close_hour=close_hour)
    gaming_day_end = serving_day - timedelta(days=1)
    gaming_day_start = gaming_day_end - timedelta(days=int(lookback_days) - 1)
    return gaming_day_start, gaming_day_end


def export_clickhouse_bets_to_parquet(
    out_parquet: Path,
    *,
    bets_gaming_day_start: date,
    bets_gaming_day_end: date,
    player_ids: frozenset[int],
) -> dict[str, Any]:
    """Export minimal bet columns from ClickHouse for mid-term materialization."""
    cfg = default_hightier_serving_config()
    client = get_clickhouse_client()
    placeholder = int(cfg.placeholder_player_id)
    out_parquet = Path(out_parquet).resolve()
    out_parquet.parent.mkdir(parents=True, exist_ok=True)

    def _query(player_filter: str) -> pd.DataFrame:
        q = f"""
        SELECT
            CAST(player_id AS Int64) AS player_id,
            CAST(gaming_day AS Date) AS gaming_day,
            CAST(payout_complete_dtm AS DateTime64(3, 'UTC')) AS payout_complete_dtm,
            {CH_TBET_WAGER_SELECT},
            {CH_TBET_PAYOUT_ODDS_SELECT}
        FROM {cfg.source_db}.{cfg.tbet} FINAL
        WHERE gaming_day >= %(g_start)s
          AND gaming_day <= %(g_end)s
          AND payout_complete_dtm IS NOT NULL
          AND gaming_day IS NOT NULL
          AND {CH_TBET_WAGER_POSITIVE_PRED}
          AND player_id IS NOT NULL
          AND player_id != {placeholder}
          {player_filter}
        """
        return client.query_df(
            q,
            parameters={"g_start": bets_gaming_day_start, "g_end": bets_gaming_day_end},
        )

    if not player_ids:
        raise ValueError("player_ids is empty; Feast refresh requires ADT allowlist scope")
    chunk_size = int(cfg.hightier_scorer_player_id_chunk_size)
    chunks = _split_player_id_chunks(player_ids, chunk_size)
    frames: list[pd.DataFrame] = []
    row_cap = int(cfg.hightier_scorer_chunk_merge_row_cap)
    t0 = time.perf_counter()
    for i, chunk in enumerate(chunks):
        in_list = ",".join(str(int(x)) for x in chunk)
        frames.append(_query(f"AND player_id IN ({in_list})"))
        if row_cap > 0 and sum(len(f) for f in frames) > row_cap:
            raise RuntimeError(
                f"ClickHouse bet export exceeds hightier_scorer_chunk_merge_row_cap={row_cap} "
                f"after chunk {i + 1}/{len(chunks)}"
            )
    df = pd.concat([f for f in frames if not f.empty], ignore_index=True) if frames else pd.DataFrame()
    elapsed = round(time.perf_counter() - t0, 3)
    if df.empty:
        raise ValueError(
            f"ClickHouse bet export returned 0 rows for gaming_day in "
            f"[{bets_gaming_day_start}, {bets_gaming_day_end}]"
        )
    df.to_parquet(out_parquet, index=False)
    return {
        "source": "clickhouse",
        "rows_exported": int(len(df)),
        "export_seconds": elapsed,
        "query_count": len(chunks),
        "path": str(out_parquet),
    }


def _sanitize_ch_session_export_df(df: pd.DataFrame) -> pd.DataFrame:
    required = ("player_id", "gaming_day", "theo_win")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"session export missing columns {missing}")
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
    """Export minimal session columns from ClickHouse for slow patron materialization."""
    if not player_ids:
        raise ValueError("player_ids is empty; Feast refresh requires ADT allowlist scope")
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
            SELECT player_id, gaming_day, theo_win
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
        frames.append(client.query_df(q, parameters={"g_start": gaming_day_start, "g_end": gaming_day_end}))
        if row_cap > 0 and sum(len(f) for f in frames) > row_cap:
            raise RuntimeError(
                f"ClickHouse session export exceeds hightier_scorer_chunk_merge_row_cap={row_cap} "
                f"after chunk {i + 1}/{len(chunks)}"
            )
    raw = pd.concat([f for f in frames if not f.empty], ignore_index=True) if frames else pd.DataFrame()
    elapsed = round(time.perf_counter() - t0, 3)
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
        "export_seconds": elapsed,
        "query_count": len(chunks),
        "path": str(out_parquet),
    }


def default_training_mid_snapshot_parquet_path() -> Path:
    """Return repo-local default training mid snapshot path."""
    return Path(__file__).resolve().parents[1] / "artifacts" / "training_data" / (
        "_main_trainer_mid_term_daily_snapshot.parquet"
    )


def resolve_bootstrap_mid_seed_parquet(
    bundle_root: Path,
    *,
    metrics: dict[str, Any] | None = None,
) -> Path | None:
    """Return bundled training mid snapshot for Feast bootstrap seed, if present."""
    root = Path(bundle_root).resolve()
    candidates: list[Path] = [
        root / "artifacts" / "feast" / MID_TERM_BOOTSTRAP_SEED_PARQUET_BASENAME,
        root / "models" / "deploy_inputs" / MID_TERM_SNAPSHOT_DEPLOY_PARQUET_BASENAME,
    ]
    if metrics is not None:
        metric_path = metrics.get("main_trainer_mid_term_snapshot_parquet")
        if isinstance(metric_path, str) and metric_path.strip():
            candidates.append(Path(metric_path.strip()).expanduser())
    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None


def resolve_training_mid_snapshot_parquet(opts: RefreshOptions) -> Path | None:
    """Resolve training mid snapshot for bootstrap seed, or ``None`` when unavailable."""
    cfg = default_hightier_serving_config()
    candidates: list[Path] = []
    if opts.training_mid_snapshot_parquet is not None:
        candidates.append(Path(opts.training_mid_snapshot_parquet).resolve())
    if cfg.training_mid_snapshot_parquet is not None:
        candidates.append(Path(cfg.training_mid_snapshot_parquet).resolve())
    feast_art = resolve_feast_artifacts_dir(opts.feast_repo)
    candidates.append((feast_art / MID_TERM_BOOTSTRAP_SEED_PARQUET_BASENAME).resolve())
    candidates.append(default_training_mid_snapshot_parquet_path().resolve())
    for path in candidates:
        if path.is_file():
            return path
    return None


def materialize_training_mid_feast_seed(
    *,
    training_mid_snapshot: Path,
    allowlist_parquet: Path,
    canonical_mapping_parquet: Path,
    anchor_end: date,
    anchor_start: date | None = None,
    out_parquet: Path,
) -> dict[str, Any]:
    """Filter training mid snapshot to allowlist canonicals within the bootstrap anchor window."""
    import pyarrow.parquet as pq

    src = Path(training_mid_snapshot).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"training mid snapshot missing: {src}")
    schema_cols = set(pq.read_schema(src).names)
    feat_exprs = []
    for col in PRODUCTION_MID_TERM_FEATURE_COLUMNS:
        if col in schema_cols:
            feat_exprs.append(f'"{col}"')
        else:
            feat_exprs.append(f'CAST(NULL AS DOUBLE) AS "{col}"')
    feat_sql = ", ".join(feat_exprs)
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_esc = _path_esc(src)
    allow_esc = _path_esc(allowlist_parquet)
    map_esc = _path_esc(canonical_mapping_parquet)
    dst_esc = _path_esc(dst)
    sql = f"""
COPY (
  SELECT TRIM(CAST(s.canonical_id AS VARCHAR)) AS canonical_id,
         CAST(s.anchor_gaming_day AS DATE) AS anchor_gaming_day,
         {feat_sql}
  FROM read_parquet('{src_esc}') AS s
  INNER JOIN (
    SELECT DISTINCT TRIM(CAST(m.canonical_id AS VARCHAR)) AS canonical_id
    FROM read_parquet('{allow_esc}') AS a
    INNER JOIN read_parquet('{map_esc}') AS m
      ON CAST(a.player_id AS BIGINT) = CAST(m.player_id AS BIGINT)
    WHERE TRIM(CAST(m.canonical_id AS VARCHAR)) <> ''
  ) AS allow
    ON TRIM(CAST(s.canonical_id AS VARCHAR)) = allow.canonical_id
  WHERE CAST(s.anchor_gaming_day AS DATE) <= CAST('{anchor_end.isoformat()}' AS DATE)
    {
        f"AND CAST(s.anchor_gaming_day AS DATE) >= CAST('{anchor_start.isoformat()}' AS DATE)"
        if anchor_start is not None
        else ""
    }
) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(sql)
        stats = con.execute(
            f"""
            SELECT COUNT(*) AS rows,
                   COUNT(DISTINCT canonical_id) AS distinct_canonical
            FROM read_parquet('{dst_esc}')
            """,
        ).fetchone()
    finally:
        con.close()
    rows = int(stats[0] if stats and stats[0] is not None else 0)
    distinct = int(stats[1] if stats and stats[1] is not None else 0)
    if rows <= 0 or distinct <= 0:
        raise ValueError(
            f"training mid seed produced no rows (rows={rows}, distinct_canonical={distinct}) "
            f"from {src}"
        )
    return {
        "source": str(src),
        "rows": rows,
        "distinct_canonical": distinct,
        "anchor_end": anchor_end.isoformat(),
        "path": str(dst),
    }


def merge_mid_feast_carry_forward(
    *,
    previous_feast_parquet: Path | None,
    daily_snapshot_parquet: Path,
    feast_out: Path,
) -> int:
    """Merge daily production snapshot into carry-forward Feast mid parquet."""
    dst = Path(feast_out).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst_esc = _path_esc(dst)
    new_esc = _path_esc(daily_snapshot_parquet)
    feat_cols = ", ".join(f'"{c}"' for c in PRODUCTION_MID_TERM_FEATURE_COLUMNS)
    prev_path = Path(previous_feast_parquet).resolve() if previous_feast_parquet else None
    if prev_path is not None and prev_path.is_file():
        prev_esc = _path_esc(prev_path)
        combined_sql = f"""
        SELECT TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id,
               CAST(event_timestamp AS DATE) AS anchor_gaming_day,
               {feat_cols}
        FROM read_parquet('{prev_esc}')
        UNION ALL
        SELECT TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id,
               CAST(anchor_gaming_day AS DATE) AS anchor_gaming_day,
               {feat_cols}
        FROM read_parquet('{new_esc}')
        """
    else:
        combined_sql = f"""
        SELECT TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id,
               CAST(anchor_gaming_day AS DATE) AS anchor_gaming_day,
               {feat_cols}
        FROM read_parquet('{new_esc}')
        """
    sql = f"""
COPY (
  SELECT canonical_id,
    CAST(anchor_gaming_day AS VARCHAR) AS anchor_gaming_day,
    {feat_cols},
    CAST(
      timezone('Asia/Hong_Kong', CAST(anchor_gaming_day AS TIMESTAMP) + INTERVAL '1' DAY - INTERVAL '1' SECOND)
      AS TIMESTAMPTZ
    ) AS event_timestamp
  FROM (
    SELECT canonical_id, anchor_gaming_day, {feat_cols},
      ROW_NUMBER() OVER (
        PARTITION BY canonical_id
        ORDER BY anchor_gaming_day DESC
      ) AS rn
    FROM ({combined_sql}) AS combined
  ) AS ranked
  WHERE rn = 1
) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(sql)
        return int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{dst_esc}')").fetchone()[0])
    finally:
        con.close()


def read_feast_mid_spike_stats(feast_parquet: Path) -> dict[str, Any]:
    """Return row count, distinct canonical, and max anchor from Feast mid spike parquet."""
    esc = _path_esc(feast_parquet)
    row = duckdb.sql(
        f"""
        SELECT COUNT(*) AS rows,
               COUNT(DISTINCT canonical_id) AS distinct_canonical,
               MAX(CAST(event_timestamp AS DATE)) AS anchor_max
        FROM read_parquet('{esc}')
        """,
    ).fetchone()
    if row is None:
        return {"rows": 0, "distinct_canonical": 0, "anchor_max": None}
    anchor_max = row[2]
    if anchor_max is not None and hasattr(anchor_max, "date"):
        anchor_max = anchor_max.date()
    elif anchor_max is not None:
        anchor_max = pd.Timestamp(anchor_max).date()
    return {
        "rows": int(row[0] or 0),
        "distinct_canonical": int(row[1] or 0),
        "anchor_max": anchor_max,
    }


def enrich_mid_refresh_meta_from_feast(
    meta: dict[str, Any],
    *,
    feast_path: Path,
    training_seed_meta: dict[str, Any] | None,
    data_bounded_expected_anchor: bool = False,
) -> dict[str, Any]:
    """Attach final Feast spike stats and optional data-bounded expected anchor to refresh meta.

    When ``data_bounded_expected_anchor`` is true (e.g. ``local_cleaned`` deploy E2E),
    freshness compares against the materialized max anchor instead of calendar ``D-1``.
    """
    stats = read_feast_mid_spike_stats(feast_path)
    out = {
        **meta,
        "feast_spike_rows": int(stats["rows"]),
        "distinct_canonical_count": int(stats["distinct_canonical"]),
        "mid_term_anchor_gaming_day_max": (
            stats["anchor_max"].isoformat() if stats["anchor_max"] is not None else None
        ),
        "materialize_source": "feast_online_refresh",
    }
    use_bounded = data_bounded_expected_anchor or training_seed_meta is not None
    if use_bounded and stats["anchor_max"] is not None:
        out["mid_term_expected_anchor_gaming_day"] = stats["anchor_max"].isoformat()
    return out


def write_mid_feast_parquet(full_snap: Path, feast_out: Path) -> int:
    """Collapse to latest anchor per canonical_id and add ``event_timestamp``."""
    src = str(Path(full_snap).resolve()).replace("\\", "/").replace("'", "''")
    dst = Path(feast_out).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst_esc = str(dst).replace("\\", "/").replace("'", "''")
    feat_cols = ", ".join(f'"{c}"' for c in PRODUCTION_MID_TERM_FEATURE_COLUMNS)
    sql = f"""
COPY (
  SELECT canonical_id,
    CAST(anchor_gaming_day AS VARCHAR) AS anchor_gaming_day,
    {feat_cols},
    CAST(
      timezone('Asia/Hong_Kong', CAST(anchor_gaming_day AS TIMESTAMP) + INTERVAL '1' DAY - INTERVAL '1' SECOND)
      AS TIMESTAMPTZ
    ) AS event_timestamp
  FROM (
    SELECT TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id,
      CAST(anchor_gaming_day AS DATE) AS anchor_gaming_day, {feat_cols},
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
        return int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{dst_esc}')").fetchone()[0])
    finally:
        con.close()


def write_slow_feast_parquet(full_snap: Path, feast_out: Path) -> int:
    """Collapse slow snapshot to latest monthly anchor per canonical_id."""
    src = str(Path(full_snap).resolve()).replace("\\", "/").replace("'", "''")
    dst = Path(feast_out).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst_esc = str(dst).replace("\\", "/").replace("'", "''")
    feat_cols = ", ".join(f'"{c}"' for c in PRODUCTION_LONG_TERM_FEATURE_COLUMNS)
    import pyarrow.parquet as pq

    schema_names = set(pq.read_schema(str(Path(full_snap).resolve())).names)
    if "anchor_gaming_day" in schema_names:
        ts_expr = (
            "CAST((CAST(anchor_gaming_day AS TIMESTAMP) + INTERVAL '1' DAY "
            "- INTERVAL '1' SECOND) AS TIMESTAMPTZ)"
        )
    elif "event_timestamp" in schema_names:
        ts_expr = "CAST(event_timestamp AS TIMESTAMPTZ)"
    else:
        raise ValueError(
            f"slow snapshot must include anchor_gaming_day or event_timestamp: {full_snap}",
        )
    sql = f"""
COPY (
  SELECT canonical_id, {feat_cols},
    {ts_expr} AS event_timestamp
  FROM (
    SELECT TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id,
      {feat_cols},
      {"CAST(anchor_gaming_day AS DATE) AS anchor_gaming_day," if "anchor_gaming_day" in schema_names else ""}
      {"CAST(event_timestamp AS TIMESTAMPTZ) AS event_timestamp," if "event_timestamp" in schema_names else ""}
      ROW_NUMBER() OVER (
        PARTITION BY TRIM(CAST(canonical_id AS VARCHAR))
        ORDER BY {"CAST(anchor_gaming_day AS DATE) DESC" if "anchor_gaming_day" in schema_names else "event_timestamp DESC"}
      ) AS rn
    FROM read_parquet('{src}')
  ) AS ranked
  WHERE rn = 1
) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(sql)
        return int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{dst_esc}')").fetchone()[0])
    finally:
        con.close()


def run_feast_apply(feast_repo: Path, *, reset_runtime: bool = False) -> float:
    """Run ``feast apply``; optionally reset repo runtime state first."""
    repo = Path(feast_repo).resolve()
    if reset_runtime:
        reset_feast_repo_runtime_state(repo)
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


def _materialize_window_from_feast_parquet(feast_parquet: Path) -> tuple[datetime, datetime]:
    """Derive Feast materialize window from ``event_timestamp`` bounds."""
    esc = str(Path(feast_parquet).resolve()).replace("\\", "/").replace("'", "''")
    row = duckdb.sql(
        f"SELECT MIN(event_timestamp), MAX(event_timestamp) FROM read_parquet('{esc}')",
    ).fetchone()
    if row is None or row[0] is None or row[1] is None:
        raise ValueError(f"feast parquet has no event_timestamp rows: {feast_parquet}")
    start_dt = row[0].to_pydatetime() if hasattr(row[0], "to_pydatetime") else row[0]
    end_dt = row[1].to_pydatetime() if hasattr(row[1], "to_pydatetime") else row[1]
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    return start_dt - timedelta(hours=1), end_dt + timedelta(hours=1)


def ensure_feast_mid_anchor_column_ready(feast_repo: Path) -> None:
    """Ensure production Feast schema is ready (includes ``anchor_gaming_day`` on mid FV)."""
    from trainer_hightier.serving.feast_online_adapter import ensure_feast_schema_ready

    ensure_feast_schema_ready(Path(feast_repo).resolve(), auto_apply=True)


def sync_training_mid_snapshot_to_feast_online(
    feast_repo: Path,
    *,
    mid_snapshot: Path,
) -> dict[str, Any]:
    """Publish training mid-term daily snapshot into Feast online (Step 6 / parity replay).

    Collapses to latest anchor per ``canonical_id`` and materializes ``mid_term_daily_spike_features``
    including ``anchor_gaming_day`` for Option B bounded ASOF.
    """
    src = Path(mid_snapshot).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"training mid snapshot not found: {src}")
    repo = Path(feast_repo).resolve()
    ensure_feast_mid_anchor_column_ready(repo)
    feast_art = resolve_feast_artifacts_dir(repo)
    feast_art.mkdir(parents=True, exist_ok=True)
    feast_path = feast_art / "mid_term_spike_canonical.parquet"
    t0 = time.perf_counter()
    feast_rows = write_mid_feast_parquet(src, feast_path)
    materialize_seconds = run_feast_materialize_views(
        repo,
        feature_views=(MID_SPIKE_FEATURE_VIEW_NAME,),
        feast_parquets=(feast_path,),
    )
    elapsed = round(time.perf_counter() - t0, 3)
    logger.info(
        "[feast_online_refresh] synced training mid snapshot rows=%d feast_path=%s materialize_s=%.3f",
        feast_rows,
        feast_path,
        materialize_seconds,
    )
    return {
        "mid_source_parquet": str(src),
        "feast_parquet": str(feast_path),
        "feast_rows": int(feast_rows),
        "materialize_seconds": float(materialize_seconds),
        "elapsed_seconds": elapsed,
    }


def sync_training_slow_parquet_to_feast_online(
    feast_repo: Path,
    *,
    slow_parquet: Path,
) -> dict[str, Any]:
    """Publish training ``slow_patron_180d_monthly.parquet`` into Feast online (scorer v2 path).

    Copies the canonical active-month artifact into bundle-local Feast artifacts, then
    materializes ``long_term_slow_spike_features`` so Step 6 / production replay match training.
    """
    src = Path(slow_parquet).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"training slow parquet not found: {src}")
    repo = Path(feast_repo).resolve()
    feast_art = resolve_feast_artifacts_dir(repo)
    feast_art.mkdir(parents=True, exist_ok=True)
    feast_path = feast_art / "slow_patron_180d_monthly.parquet"
    t0 = time.perf_counter()
    feast_rows = write_slow_feast_parquet(src, feast_path)
    materialize_seconds = run_feast_materialize_views(
        repo,
        feature_views=(LONG_SPIKE_FEATURE_VIEW_NAME,),
        feast_parquets=(feast_path,),
    )
    elapsed = round(time.perf_counter() - t0, 3)
    logger.info(
        "[feast_online_refresh] synced training slow parquet rows=%d feast_path=%s materialize_s=%.3f",
        feast_rows,
        feast_path,
        materialize_seconds,
    )
    return {
        "slow_source_parquet": str(src),
        "feast_parquet": str(feast_path),
        "feast_rows": int(feast_rows),
        "materialize_seconds": float(materialize_seconds),
        "elapsed_seconds": elapsed,
    }


def run_feast_materialize_views(
    feast_repo: Path,
    *,
    feature_views: tuple[str, ...],
    feast_parquets: tuple[Path, ...],
) -> float:
    """Materialize selected feature views into the online store."""
    from feast import FeatureStore

    if not feature_views:
        return 0.0
    t0 = time.perf_counter()
    store = FeatureStore(repo_path=str(Path(feast_repo).resolve()))
    for view, parquet in zip(feature_views, feast_parquets, strict=True):
        start_dt, end_dt = _materialize_window_from_feast_parquet(parquet)
        store.materialize(feature_views=[view], start_date=start_dt, end_date=end_dt)
    return round(time.perf_counter() - t0, 3)


def _resolve_refresh_options(
    *,
    layers: str,
    source: str,
    skip_apply: bool,
    skip_materialize: bool,
    smoke_only: bool,
    dry_run: bool,
    feast_repo: Path | None,
    readiness_path: Path | None,
    canonical_mapping: Path | None,
    adt_allowlist: Path | None,
    local_cleaned_bet: Path | None,
    local_cleaned_session: Path | None,
    max_smoke_entities: int,
    summary_path: Path | None,
    bootstrap_mid: bool = False,
    apply_schema: bool = False,
    training_mid_snapshot_parquet: Path | None = None,
    use_training_mid_seed: bool = True,
) -> RefreshOptions:
    cfg = default_hightier_serving_config()
    parsed_layers = parse_refresh_layers(layers)
    src = str(source).strip().lower()
    if src not in ("clickhouse", "local_cleaned"):
        raise ValueError(f"unsupported source={source!r}; use clickhouse or local_cleaned")
    if src == "local_cleaned" and "mid" in parsed_layers and local_cleaned_bet is None:
        raise ValueError("local_cleaned source requires --local-cleaned-bet for mid layer")
    if src == "local_cleaned" and "slow" in parsed_layers and local_cleaned_session is None:
        raise ValueError("local_cleaned source requires --local-cleaned-session for slow layer")
    if src == "clickhouse" and (local_cleaned_bet or local_cleaned_session):
        raise ValueError("local cleaned overrides are only valid with --source local_cleaned")
    allow = (
        Path(adt_allowlist).resolve()
        if adt_allowlist is not None
        else resolve_adt_allowlist_path(cfg, manifest=None).resolve()
    )
    if not allow.is_file():
        raise FileNotFoundError(f"adt allowlist missing: {allow}")
    cmap = resolve_production_canonical_mapping(canonical_mapping)
    repo = Path(feast_repo or default_feast_repo_path()).resolve()
    feast_art = resolve_feast_artifacts_dir(repo)
    feast_art.mkdir(parents=True, exist_ok=True)
    readiness = Path(readiness_path or resolve_feast_readiness_path(cfg)).resolve()
    return RefreshOptions(
        layers=parsed_layers,
        source=src,
        skip_apply=skip_apply,
        skip_materialize=skip_materialize,
        smoke_only=smoke_only,
        dry_run=dry_run,
        feast_repo=repo,
        readiness_path=readiness,
        canonical_mapping=cmap,
        adt_allowlist=allow,
        local_cleaned_bet=Path(local_cleaned_bet).resolve() if local_cleaned_bet else None,
        local_cleaned_session=Path(local_cleaned_session).resolve() if local_cleaned_session else None,
        max_smoke_entities=int(max_smoke_entities),
        summary_path=Path(summary_path or feast_art / "feast_online_refresh_report.json").resolve(),
        bootstrap_mid=bool(bootstrap_mid),
        apply_schema=bool(apply_schema),
        training_mid_snapshot_parquet=(
            Path(training_mid_snapshot_parquet).resolve()
            if training_mid_snapshot_parquet is not None
            else None
        ),
        use_training_mid_seed=bool(use_training_mid_seed),
    )


def _refresh_mid_layer(
    *,
    opts: RefreshOptions,
    staging_dir: Path,
    player_ids: frozenset[int],
) -> LayerRefreshOutcome:
    cfg = default_hightier_serving_config()
    anchor_start, anchor_end, bets_start, bets_end = _mid_export_bounds(
        close_hour=int(cfg.gaming_day_close_hour),
        bootstrap_mid=opts.bootstrap_mid,
        bootstrap_anchor_days=int(cfg.production_mid_feast_bootstrap_anchor_days),
    )
    if opts.source == "clickhouse":
        bet_path = staging_dir / "ch_bets_export.parquet"
        export_meta = export_clickhouse_bets_to_parquet(
            bet_path,
            bets_gaming_day_start=bets_start,
            bets_gaming_day_end=bets_end,
            player_ids=player_ids,
        )
    else:
        bet_path = Path(opts.local_cleaned_bet).resolve()
        if not bet_path.exists():
            raise FileNotFoundError(f"local cleaned bet missing: {bet_path}")
        export_meta = {"source": "local_cleaned", "path": str(bet_path), "rows_exported": None}
    artifact_path = staging_dir / "mid_term_production.parquet"
    t0 = time.perf_counter()
    artifact_path, meta = materialize_production_mid_term_daily_snapshot(
        cleaned_bet_parquet=bet_path,
        canonical_mapping_parquet=opts.canonical_mapping,
        adt_allowlist_parquet=opts.adt_allowlist,
        out_parquet=artifact_path,
        anchor_gaming_day_start=anchor_start,
        anchor_gaming_day_end=anchor_end,
        publish_readiness=False,
    )
    compute_seconds = round(time.perf_counter() - t0, 3)
    feast_art = resolve_feast_artifacts_dir(opts.feast_repo)
    feast_art.mkdir(parents=True, exist_ok=True)
    feast_path = feast_art / "mid_term_spike_canonical.parquet"
    training_seed_meta: dict[str, Any] | None = None
    if opts.bootstrap_mid and opts.use_training_mid_seed:
        seed_src = resolve_training_mid_snapshot_parquet(opts)
        if seed_src is not None:
            seed_staging = staging_dir / "mid_training_seed.parquet"
            training_seed_meta = materialize_training_mid_feast_seed(
                training_mid_snapshot=seed_src,
                allowlist_parquet=opts.adt_allowlist,
                canonical_mapping_parquet=opts.canonical_mapping,
                anchor_end=anchor_end,
                anchor_start=anchor_start,
                out_parquet=seed_staging,
            )
            seed_feast = staging_dir / "mid_training_seed_feast.parquet"
            write_mid_feast_parquet(seed_staging, seed_feast)
            feast_rows = merge_mid_feast_carry_forward(
                previous_feast_parquet=seed_feast,
                daily_snapshot_parquet=artifact_path,
                feast_out=feast_path,
            )
            bootstrap_completed = True
        elif opts.bootstrap_mid or not feast_path.is_file():
            feast_rows = write_mid_feast_parquet(artifact_path, feast_path)
            bootstrap_completed = True
        else:
            feast_rows = merge_mid_feast_carry_forward(
                previous_feast_parquet=feast_path,
                daily_snapshot_parquet=artifact_path,
                feast_out=feast_path,
            )
            bootstrap_completed = False
    elif opts.bootstrap_mid or not feast_path.is_file():
        feast_rows = write_mid_feast_parquet(artifact_path, feast_path)
        bootstrap_completed = True
    else:
        feast_rows = merge_mid_feast_carry_forward(
            previous_feast_parquet=feast_path,
            daily_snapshot_parquet=artifact_path,
            feast_out=feast_path,
        )
        bootstrap_completed = False
    meta = {
        **meta,
        "feast_spike_rows": feast_rows,
        "feast_spike_parquet": str(feast_path),
        "mid_term_bootstrap_completed": bootstrap_completed,
        "anchor_start": anchor_start.isoformat(),
        "anchor_end": anchor_end.isoformat(),
    }
    if training_seed_meta is not None:
        meta["training_mid_seed"] = training_seed_meta
    meta = enrich_mid_refresh_meta_from_feast(
        meta,
        feast_path=feast_path,
        training_seed_meta=training_seed_meta,
        data_bounded_expected_anchor=export_meta.get("source") == "local_cleaned",
    )
    return LayerRefreshOutcome(
        layer="mid",
        status="ok",
        meta=meta,
        export_meta=export_meta,
        artifact_path=artifact_path,
        feast_parquet_path=feast_path,
        compute_seconds=compute_seconds,
        detail={
            "anchor_start": anchor_start.isoformat(),
            "anchor_end": anchor_end.isoformat(),
            "bootstrap_mid": opts.bootstrap_mid,
        },
    )


def _refresh_slow_layer(
    *,
    opts: RefreshOptions,
    staging_dir: Path,
    player_ids: frozenset[int],
) -> LayerRefreshOutcome:
    cfg = default_hightier_serving_config()
    lb = int(cfg.production_slow_lookback_days)
    g_start, g_end = _slow_export_bounds(close_hour=int(cfg.gaming_day_close_hour), lookback_days=lb)
    if opts.source == "clickhouse":
        sess_path = staging_dir / "ch_sessions_export.parquet"
        export_meta = export_clickhouse_sessions_to_parquet(
            sess_path,
            gaming_day_start=g_start,
            gaming_day_end=g_end,
            player_ids=player_ids,
        )
    else:
        sess_path = Path(opts.local_cleaned_session).resolve()
        if not sess_path.is_file():
            raise FileNotFoundError(f"local cleaned session missing: {sess_path}")
        export_meta = {"source": "local_cleaned", "path": str(sess_path), "rows_exported": None}
    artifact_path = staging_dir / "slow_patron_production.parquet"
    t0 = time.perf_counter()
    artifact_path, meta = materialize_production_slow_canonical_asof(
        cleaned_session_parquet=sess_path,
        canonical_mapping_parquet=opts.canonical_mapping,
        out_parquet=artifact_path,
        lookback_days=lb,
        publish_readiness=False,
    )
    compute_seconds = round(time.perf_counter() - t0, 3)
    feast_art = resolve_feast_artifacts_dir(opts.feast_repo)
    feast_art.mkdir(parents=True, exist_ok=True)
    feast_path = feast_art / "slow_patron_180d_monthly.parquet"
    feast_rows = write_slow_feast_parquet(artifact_path, feast_path)
    meta = {**meta, "feast_spike_rows": feast_rows, "feast_spike_parquet": str(feast_path)}
    return LayerRefreshOutcome(
        layer="slow",
        status="ok",
        meta=meta,
        export_meta=export_meta,
        artifact_path=artifact_path,
        feast_parquet_path=feast_path,
        compute_seconds=compute_seconds,
        detail={"gaming_day_start": g_start.isoformat(), "gaming_day_end": g_end.isoformat()},
    )


def _layer_readiness_from_outcome(
    outcome: LayerRefreshOutcome,
    *,
    smoke: dict[str, Any] | None,
) -> FeastLayerReadiness:
    if outcome.layer == "mid":
        base = layer_readiness_from_production_mid_meta(outcome.meta)
    else:
        base = layer_readiness_from_production_slow_meta(outcome.meta)
    if smoke is None:
        return replace(base, materialize_source="feast_online_refresh")
    present_rate = 1.0 - float(smoke.get("entity_missing_rate") or 0.0)
    return replace(
        base,
        lookup_sample_size=int(smoke.get("sample_size") or 0),
        lookup_entity_present_rate=round(present_rate, 4),
        cell_null_counts={str(k): int(v) for k, v in (smoke.get("cell_null_counts") or {}).items()},
        materialize_source="feast_online_refresh",
    )


def _write_summary_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def run_feast_online_refresh(opts: RefreshOptions) -> dict[str, Any]:
    """Execute one Feast online refresh run."""
    cfg = default_hightier_serving_config()
    init_feature_state_db()
    run_id = _utc_run_id()
    feast_art = resolve_feast_artifacts_dir(opts.feast_repo)
    feast_art.mkdir(parents=True, exist_ok=True)
    staging_dir = feast_art / "refresh_staging" / run_id
    staging_dir.mkdir(parents=True, exist_ok=True)
    feast_refresh_run_start(
        run_id,
        source=opts.source,
        layers=",".join(sorted(opts.layers)),
        feast_repo=str(opts.feast_repo),
        readiness_path=str(opts.readiness_path),
    )
    summary: dict[str, Any] = {
        "run_id": run_id,
        "layers": sorted(opts.layers),
        "source": opts.source,
        "dry_run": opts.dry_run,
        "smoke_only": opts.smoke_only,
        "bootstrap_mid": opts.bootstrap_mid,
        "apply_schema": opts.apply_schema,
    }
    apply_seconds: float | None = None
    materialize_seconds: float | None = None
    layer_outcomes: list[LayerRefreshOutcome] = []
    try:
        player_ids = load_adt_allowlist_ids(opts.adt_allowlist)
        if opts.dry_run:
            summary["verdict"] = "dry_run"
            summary["staging_dir"] = str(staging_dir)
            _write_summary_report(opts.summary_path, summary)
            feast_refresh_run_finish(run_id, status="ok", summary_json=json.dumps(summary))
            return summary
        if not opts.smoke_only:
            if "mid" in opts.layers:
                layer_outcomes.append(_refresh_mid_layer(opts=opts, staging_dir=staging_dir, player_ids=player_ids))
            if "slow" in opts.layers:
                layer_outcomes.append(_refresh_slow_layer(opts=opts, staging_dir=staging_dir, player_ids=player_ids))
            registry_missing = feast_registry_missing(opts.feast_repo)
            drift_issues = feast_schema_drift_issues(opts.feast_repo)
            want_bootstrap_apply = opts.bootstrap_mid or opts.apply_schema
            if not opts.skip_apply and (drift_issues or want_bootstrap_apply):
                apply_res = ensure_feast_schema_ready(
                    opts.feast_repo,
                    auto_apply=True,
                    force_apply=want_bootstrap_apply,
                    reset_runtime=want_bootstrap_apply and not registry_missing,
                )
                apply_seconds = apply_res.feast_apply_wall_sec
            if not opts.skip_materialize:
                views: list[str] = []
                parquets: list[Path] = []
                for outcome in layer_outcomes:
                    if outcome.layer == "mid":
                        views.append(MID_SPIKE_FEATURE_VIEW_NAME)
                    else:
                        views.append(LONG_SPIKE_FEATURE_VIEW_NAME)
                    parquets.append(outcome.feast_parquet_path)
                materialize_seconds = run_feast_materialize_views(
                    opts.feast_repo,
                    feature_views=tuple(views),
                    feast_parquets=tuple(parquets),
                )
        mid_cols = PRODUCTION_MID_TERM_FEATURE_COLUMNS if "mid" in opts.layers else ()
        slow_cols = PRODUCTION_LONG_TERM_FEATURE_COLUMNS if "slow" in opts.layers else ()
        smoke_event_ts = None
        mid_feast_for_smoke: Path | None = None
        if mid_cols:
            for outcome in layer_outcomes:
                if outcome.layer == "mid" and outcome.feast_parquet_path.is_file():
                    mid_feast_for_smoke = outcome.feast_parquet_path
                    smoke_event_ts = read_feast_parquet_max_event_timestamp(outcome.feast_parquet_path)
                    break
            if smoke_event_ts is None:
                anchor_end = expected_mid_term_anchor(
                    serving_gaming_day(close_hour=int(cfg.gaming_day_close_hour))
                )
                smoke_event_ts = mid_feast_event_timestamp_for_anchor(anchor_end)
        smoke = run_allowlist_feast_lookup_smoke(
            feast_repo=opts.feast_repo,
            allowlist_parquet=opts.adt_allowlist,
            canonical_mapping_parquet=opts.canonical_mapping,
            mid_columns=mid_cols,
            slow_columns=slow_cols,
            sample_size=opts.max_smoke_entities,
            entity_missing_fail_fraction=float(cfg.scorer_feast_entity_missing_fail_fraction),
            mid_cell_null_fail_fraction=float(cfg.scorer_feast_mid_cell_null_fail_fraction),
            mid_smoke_columns=cfg.scorer_feast_mid_smoke_columns,
            smoke_event_timestamp=smoke_event_ts,
            mid_feast_parquet=mid_feast_for_smoke,
        )
        feast_spike_rows: int | None = None
        allowlist_canonical_count: int | None = None
        if "mid" in opts.layers:
            for outcome in layer_outcomes:
                if outcome.layer == "mid":
                    feast_spike_rows = int(outcome.meta.get("feast_spike_rows") or 0)
                    break
            if not opts.smoke_only:
                allowlist_canonical_count = count_allowlist_canonical_ids(
                    opts.adt_allowlist,
                    opts.canonical_mapping,
                )
        smoke = {
            **smoke,
            **mid_feast_coverage_telemetry(
                feast_spike_rows=feast_spike_rows,
                allowlist_canonical_count=allowlist_canonical_count,
            ),
        }
        smoke_ok, smoke_reason = evaluate_feast_lookup_smoke_gate(
            smoke,
            mid_columns=mid_cols,
            entity_missing_fail_fraction=float(cfg.scorer_feast_entity_missing_fail_fraction),
            mid_cell_null_fail_fraction=float(cfg.scorer_feast_mid_cell_null_fail_fraction),
            feast_spike_rows=feast_spike_rows,
            allowlist_canonical_count=allowlist_canonical_count,
            mid_smoke_columns=cfg.scorer_feast_mid_smoke_columns,
        )
        if smoke_ok and smoke.get("mid_canonical_coverage_fraction") is not None:
            logger.info(
                "[feast_online_refresh] mid Feast allowlist coverage telemetry: "
                "coverage=%.4f rows=%s allowlist=%s (informational only)",
                float(smoke["mid_canonical_coverage_fraction"]),
                smoke.get("feast_spike_rows"),
                smoke.get("allowlist_canonical_count"),
            )
        if not smoke_ok:
            raise RuntimeError(smoke_reason or "Feast online smoke failed")
        readiness_doc: FeastOnlineReadiness | None = load_feast_online_readiness(opts.readiness_path)
        for outcome in layer_outcomes:
            layer_doc = _layer_readiness_from_outcome(outcome, smoke=smoke)
            readiness_doc = merge_layer_readiness(readiness_doc, layer_doc, feast_repo=opts.feast_repo)
            upsert_feast_refresh_layer(
                run_id,
                layer=outcome.layer,
                status="ok",
                artifact_path=str(outcome.artifact_path),
                row_count=int(outcome.meta.get("row_count") or 0),
                anchor_gaming_day_max=str(
                    outcome.meta.get("mid_term_anchor_gaming_day_max")
                    or outcome.meta.get("slow_anchor_gaming_day_max")
                    or ""
                )
                or None,
                source_scope=FEAST_READINESS_SCOPE_PRODUCTION,
                feature_view=(
                    MID_SPIKE_FEATURE_VIEW_NAME if outcome.layer == "mid" else LONG_SPIKE_FEATURE_VIEW_NAME
                ),
                export_rows=int(outcome.export_meta.get("rows_exported") or 0) or None,
                export_seconds=float(outcome.export_meta.get("export_seconds") or 0) or None,
                compute_seconds=outcome.compute_seconds,
                smoke_sample_size=int(smoke.get("sample_size") or 0),
                smoke_entity_present_rate=1.0 - float(smoke.get("entity_missing_rate") or 0.0),
                detail_json=json.dumps(outcome.detail),
            )
        if readiness_doc is not None:
            persist_feast_online_readiness_latest(
                run_id,
                readiness_doc.to_dict(),
                path=Path(cfg.feature_state_db_path).resolve(),
            )
            write_feast_online_readiness(readiness_doc, opts.readiness_path)
        summary.update(
            {
                "verdict": "ok",
                "apply_seconds": apply_seconds,
                "materialize_seconds": materialize_seconds,
                "smoke": smoke,
                "readiness_path": str(opts.readiness_path),
            }
        )
        _write_summary_report(opts.summary_path, summary)
        feast_refresh_run_finish(
            run_id,
            status="ok",
            apply_seconds=apply_seconds,
            materialize_seconds=materialize_seconds,
            summary_json=json.dumps(summary, default=str),
        )
        return summary
    except Exception as exc:
        logger.exception("[feast_online_refresh] run failed: %s", exc)
        summary["verdict"] = "error"
        summary["error"] = str(exc)[:2000]
        _write_summary_report(opts.summary_path, summary)
        feast_refresh_run_finish(
            run_id,
            status="error",
            apply_seconds=apply_seconds,
            materialize_seconds=materialize_seconds,
            summary_json=json.dumps(summary, default=str),
        )
        raise


def main(argv: list[str] | None = None) -> int:
    """CLI entry for production Feast online refresh."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pr = argparse.ArgumentParser(description="Feast online refresh (scorer v2 mid/long)")
    pr.add_argument("--layers", default="mid,slow", help="comma-separated: mid,slow")
    pr.add_argument("--source", default="clickhouse", choices=("clickhouse", "local_cleaned"))
    pr.add_argument("--skip-apply", action="store_true")
    pr.add_argument("--apply-schema", action="store_true", help="run feast apply + reset runtime state")
    pr.add_argument("--bootstrap-mid", action="store_true", help="multi-anchor mid bootstrap + carry-forward base")
    pr.add_argument(
        "--no-training-mid-seed",
        action="store_true",
        help="skip training historical mid snapshot seed during bootstrap",
    )
    pr.add_argument("--training-mid-snapshot", type=Path, default=None, help="training mid snapshot parquet seed")
    pr.add_argument("--skip-materialize", action="store_true")
    pr.add_argument("--smoke-only", action="store_true")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--feast-repo", type=Path, default=None)
    pr.add_argument("--readiness-path", type=Path, default=None)
    pr.add_argument("--canonical-mapping", type=Path, default=None)
    pr.add_argument("--adt-allowlist", type=Path, default=None)
    pr.add_argument("--local-cleaned-bet", type=Path, default=None)
    pr.add_argument("--local-cleaned-session", type=Path, default=None)
    pr.add_argument("--max-smoke-entities", type=int, default=100)
    pr.add_argument("--summary-path", type=Path, default=None)
    args = pr.parse_args(argv)
    opts = _resolve_refresh_options(
        layers=args.layers,
        source=args.source,
        skip_apply=bool(args.skip_apply),
        skip_materialize=bool(args.skip_materialize),
        smoke_only=bool(args.smoke_only),
        dry_run=bool(args.dry_run),
        feast_repo=args.feast_repo,
        readiness_path=args.readiness_path,
        canonical_mapping=args.canonical_mapping or default_canonical_mapping_parquet_path(),
        adt_allowlist=args.adt_allowlist,
        local_cleaned_bet=args.local_cleaned_bet,
        local_cleaned_session=args.local_cleaned_session,
        max_smoke_entities=args.max_smoke_entities,
        summary_path=args.summary_path,
        bootstrap_mid=bool(args.bootstrap_mid),
        apply_schema=bool(args.apply_schema),
        training_mid_snapshot_parquet=args.training_mid_snapshot,
        use_training_mid_seed=not bool(args.no_training_mid_seed),
    )
    report = run_feast_online_refresh(opts)
    logger.info("[feast_online_refresh] verdict=%s", report.get("verdict"))
    return 0 if report.get("verdict") in ("ok", "dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
