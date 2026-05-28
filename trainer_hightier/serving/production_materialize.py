"""Production feature materialization for Route B serving (ClickHouse/cleaned-bet path)."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    FE_DERIVED_SOURCE_KIND_PRODUCTION,
    PRODUCTION_BET_MIRROR_DIRNAME,
    PRODUCTION_SESSION_MIRROR_FILENAME,
    SLOW_PATRON_GRAIN_CANONICAL_ASOF,
    default_hightier_serving_config,
)
from trainer_hightier.feature_experiment.materialize_fe_derived import materialize_fe_derived_parquet
from trainer_hightier.utils.bet_l0_preprocess import (
    cleaned_bet_dataset_has_any_parquet,
    resolved_cleaned_bet_read_parquet_sql,
)
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas
from trainer_hightier.utils.slow_month_turn import resolve_slow_month_turn_context
from trainer_hightier.utils.slow_patron_180d_monthly import materialize_slow_patron_180d_canonical_asof

logger = logging.getLogger(__name__)

# Baseline fe__* after mid-term/composite removal (retrain; no Feast mid in model).
DEFAULT_MODEL_FE_DERIVED_COLUMNS: tuple[str, ...] = (
    "fe__wager_sum__w15m",
    "fe__bets_cnt__w15m",
    "fe__canonical__bets_cnt__today",
    "fe__canonical__wager_sum__today",
    "fe__canonical__avg_wager__today",
    "fe__canonical__elapsed_sec_since_first_bet__today",
    "fe__interarrival__lag2_sec",
    "fe__interarrival__last_gap_to_recent_mean_ratio__w1h",
    "fe__interarrival__cv__w1h",
    "fe__odds__payout_odds_z__w1h",
    "fe__odds__payout_odds_to_recent_max_ratio__w1h",
    "fe__odds__payout_odds_step_ratio",
)

DEFAULT_MODEL_SLOW_PATRON_COLUMNS: tuple[str, ...] = (
    "patron__theo_win_sum__w180d_m1snap",
    "patron__gaming_days_cnt__w180d_m1snap",
    "patron__adt__w180d_m1snap",
)

_TRAINING_FE_ARTIFACT_MARKERS: tuple[str, ...] = (
    "_main_trainer_fe_derived",
    "_main_trainer_fe_short_term",
    "/training_data/",
    "\\training_data\\",
)


def _path_esc(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "''")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parquet_row_stats(path: Path, *, key_col: str = "bet_id") -> dict[str, Any]:
    """Lightweight Parquet coverage stats for manifest metadata."""

    pf = pq.ParquetFile(path)
    nrows = int(pf.metadata.num_rows) if pf.metadata is not None else 0
    schema = {c.lower(): c for c in pf.schema_arrow.names}
    stats: dict[str, Any] = {"row_count": nrows, "path": str(path.resolve())}
    if key_col.lower() in schema:
        col = schema[key_col.lower()]
        con = duckdb.connect(database=":memory:")
        try:
            esc = _path_esc(path)
            stats["distinct_bet_count"] = int(
                con.execute(
                    f"SELECT COUNT(DISTINCT TRY_CAST(\"{col}\" AS DOUBLE)) FROM read_parquet('{esc}')"
                ).fetchone()[0]
            )
        finally:
            con.close()
    return stats


def write_production_artifact_sidecar(parquet_path: Path, metadata: dict[str, Any]) -> Path:
    """Write ``<stem>.production_meta.json`` next to a production Parquet artifact."""

    sidecar = Path(parquet_path).resolve().parent / f"{Path(parquet_path).stem}.production_meta.json"
    sidecar.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    return sidecar


def _write_production_bet_id_universe(
    *,
    cleaned_bet_parquet: Path,
    adt_allowlist_parquet: Path,
    out_bet_ids_parquet: Path,
    coverage_start: datetime,
    coverage_end_exclusive: datetime,
    duckdb_runtime: DuckDbRuntimeConfig | None,
) -> None:
    """Derive production ``bet_id`` universe for fe_derived (allowlist × coverage window)."""

    if coverage_start.tzinfo is None:
        coverage_start = coverage_start.replace(tzinfo=timezone.utc)
    if coverage_end_exclusive.tzinfo is None:
        coverage_end_exclusive = coverage_end_exclusive.replace(tzinfo=timezone.utc)
    bet_from = resolved_cleaned_bet_read_parquet_sql(cleaned_bet_parquet)
    allow_esc = _path_esc(adt_allowlist_parquet)
    dst_esc = _path_esc(out_bet_ids_parquet)
    start_s = coverage_start.isoformat().replace("'", "''")
    end_s = coverage_end_exclusive.isoformat().replace("'", "''")
    sql = f"""
COPY (
  SELECT DISTINCT TRY_CAST(b."bet_id" AS DOUBLE) AS bet_id
  FROM {bet_from} AS b
  INNER JOIN (
    SELECT DISTINCT TRY_CAST(player_id AS BIGINT) AS player_id
    FROM read_parquet('{allow_esc}')
    WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
  ) AS al ON TRY_CAST(b."player_id" AS BIGINT) = al.player_id
  WHERE TRY_CAST(b."bet_id" AS DOUBLE) IS NOT NULL
    AND b."payout_complete_dtm" IS NOT NULL
    AND CAST(b."payout_complete_dtm" AS TIMESTAMPTZ) >= TIMESTAMPTZ '{start_s}'
    AND CAST(b."payout_complete_dtm" AS TIMESTAMPTZ) < TIMESTAMPTZ '{end_s}'
) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    out_bet_ids_parquet.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    try:
        if duckdb_runtime is not None:
            apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(sql)
    finally:
        con.close()
    if not out_bet_ids_parquet.is_file():
        raise RuntimeError(f"production bet_id universe not written: {out_bet_ids_parquet}")


def materialize_production_fe_derived(
    *,
    cleaned_bet_parquet: Path,
    adt_allowlist_parquet: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    canonical_mapping_parquet: Path | None = None,
    coverage_hours: int | None = None,
    coverage_end_exclusive: datetime | None = None,
    required_columns: tuple[str, ...] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Materialize production ``fe__*`` keyed by live/recent ``bet_id`` (Route B)."""

    cfg = default_hightier_serving_config()
    hours = int(coverage_hours if coverage_hours is not None else cfg.production_fe_coverage_hours)
    if hours < 1:
        raise ValueError(f"coverage_hours must be >= 1, got {hours!r}")
    cov_end = coverage_end_exclusive or datetime.now(timezone.utc)
    if cov_end.tzinfo is None:
        cov_end = cov_end.replace(tzinfo=timezone.utc)
    cov_start = cov_end - timedelta(hours=hours)
    src_root = Path(cleaned_bet_parquet).resolve()
    allow = Path(adt_allowlist_parquet).resolve()
    dst = Path(out_parquet).resolve()
    if not allow.is_file():
        raise FileNotFoundError(f"adt allowlist parquet missing: {allow}")
    if not cleaned_bet_dataset_has_any_parquet(src_root):
        raise FileNotFoundError(f"No cleaned bet parquet under {src_root}")

    tmp_ids = dst.parent / f"{dst.stem}_bet_ids.parquet"
    _write_production_bet_id_universe(
        cleaned_bet_parquet=src_root,
        adt_allowlist_parquet=allow,
        out_bet_ids_parquet=tmp_ids,
        coverage_start=cov_start,
        coverage_end_exclusive=cov_end,
        duckdb_runtime=duckdb_runtime,
    )
    materialize_fe_derived_parquet(
        cleaned_bet_parquet=src_root,
        training_parquet_for_bet_ids=tmp_ids,
        out_parquet=dst,
        duckdb_runtime=duckdb_runtime or DuckDbRuntimeConfig(),
        canonical_mapping_parquet=canonical_mapping_parquet,
    )
    cols = required_columns or DEFAULT_MODEL_FE_DERIVED_COLUMNS
    schema_names = set(pq.read_schema(dst).names)
    miss = [c for c in cols if c not in schema_names]
    if miss:
        raise ValueError(f"production fe_derived missing model columns {miss}; got {sorted(schema_names)[:40]}")

    meta = {
        "artifact_kind": "fe_derived_production",
        "fe_derived_source_kind": FE_DERIVED_SOURCE_KIND_PRODUCTION,
        "coverage_start": cov_start.isoformat(),
        "coverage_end_exclusive": cov_end.isoformat(),
        "coverage_hours": hours,
        "sha256": _sha256_file(dst),
        **parquet_row_stats(dst),
    }
    write_production_artifact_sidecar(dst, meta)
    try:
        tmp_ids.unlink(missing_ok=True)
    except OSError:
        pass
    logger.info(
        "[production_materialize] fe_derived rows=%s distinct_bets=%s coverage=[%s, %s)",
        meta.get("row_count"),
        meta.get("distinct_bet_count"),
        meta.get("coverage_start"),
        meta.get("coverage_end_exclusive"),
    )
    return dst, meta


def materialize_production_slow_canonical_asof(
    *,
    cleaned_session_parquet: Path,
    canonical_mapping_parquet: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    lookback_days: int | None = None,
    publish_readiness: bool = True,
    context_day: date | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Materialize canonical/player ASOF slow 180d snapshot for production serving."""

    cfg = default_hightier_serving_config()
    lb = int(lookback_days if lookback_days is not None else cfg.production_slow_lookback_days)
    ctx_day = context_day if context_day is not None else date.today()
    out = materialize_slow_patron_180d_canonical_asof(
        cleaned_session_parquet=cleaned_session_parquet,
        canonical_mapping_parquet=canonical_mapping_parquet,
        out_parquet=out_parquet,
        lookback_days=lb,
        duckdb_runtime=duckdb_runtime,
        context_day=ctx_day,
    )
    ctx = resolve_slow_month_turn_context(ctx_day)
    anchor_max = None
    pf = pq.ParquetFile(out)
    if pf.metadata and pf.metadata.num_rows > 0 and "anchor_gaming_day" in pf.schema_arrow.names:
        con = duckdb.connect(database=":memory:")
        try:
            esc = _path_esc(out)
            raw = con.execute(
                f"SELECT MAX(CAST(anchor_gaming_day AS DATE)) FROM read_parquet('{esc}')"
            ).fetchone()[0]
            if raw is not None:
                anchor_max = pd.Timestamp(raw).date()
        finally:
            con.close()
    meta = {
        "artifact_kind": "slow_patron_canonical_asof",
        "slow_patron_grain": SLOW_PATRON_GRAIN_CANONICAL_ASOF,
        "snapshot_scope": "production",
        "lookback_days": lb,
        "slow_anchor_gaming_day_max": anchor_max.isoformat() if anchor_max else None,
        **ctx.to_manifest_dict(),
        "sha256": _sha256_file(out),
        **parquet_row_stats(out, key_col="canonical_id"),
    }
    write_production_artifact_sidecar(out, meta)
    if publish_readiness:
        try:
            from trainer_hightier.serving.feast_readiness import (
                layer_readiness_from_production_slow_meta,
                publish_feast_layer_readiness,
            )

            publish_feast_layer_readiness(layer_readiness_from_production_slow_meta(meta))
        except Exception as exc:
            logger.warning("[production_materialize] feast readiness publish skipped: %s", exc)
    return out, meta


def _filter_parquet_to_allowlist_canonical_ids(
    *,
    src_parquet: Path,
    dst_parquet: Path,
    adt_allowlist_parquet: Path,
    canonical_mapping_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> None:
    """Keep rows whose ``canonical_id`` maps from high-ADT allowlist ``player_id`` values."""

    cfg = default_hightier_serving_config()
    rt = duckdb_runtime or DuckDbRuntimeConfig()
    dst = Path(dst_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_esc = _path_esc(Path(src_parquet))
    allow_esc = _path_esc(Path(adt_allowlist_parquet))
    cmap_esc = _path_esc(Path(canonical_mapping_parquet))
    dst_esc = _path_esc(dst)
    sql = f"""
COPY (
  WITH allow_players AS (
    SELECT DISTINCT TRY_CAST(player_id AS BIGINT) AS player_id
    FROM read_parquet('{allow_esc}')
    WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
  ),
  allow_canonical AS (
    SELECT DISTINCT TRIM(CAST(c.canonical_id AS VARCHAR)) AS canonical_id
    FROM read_parquet('{cmap_esc}') AS c
    INNER JOIN allow_players AS a ON TRY_CAST(c.player_id AS BIGINT) = a.player_id
    WHERE TRIM(CAST(c.canonical_id AS VARCHAR)) <> ''
  )
  SELECT s.*
  FROM read_parquet('{src_esc}') AS s
  INNER JOIN allow_canonical AS ac
    ON TRIM(CAST(s.canonical_id AS VARCHAR)) = ac.canonical_id
) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, rt)
        con.execute(sql)
    finally:
        con.close()


def materialize_production_mid_term_daily_snapshot(
    *,
    cleaned_bet_parquet: Path,
    canonical_mapping_parquet: Path,
    adt_allowlist_parquet: Path,
    out_parquet: Path,
    anchor_gaming_day_start: date | None = None,
    anchor_gaming_day_end: date | None = None,
    lookback_days: int | None = None,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    publish_readiness: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """Materialize canonical mid-term daily snapshot for high-ADT production universe."""

    from trainer_hightier.config import (
        MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
        MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS,
        MID_TERM_SNAPSHOT_SCOPE_PRODUCTION,
    )
    from trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot import (
        materialize_mid_term_daily_snapshot,
    )

    cfg = default_hightier_serving_config()
    rt = duckdb_runtime or DuckDbRuntimeConfig()
    tmp = Path(out_parquet).resolve().parent / f"{Path(out_parquet).stem}__full_tmp.parquet"
    lb = int(lookback_days if lookback_days is not None else MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS)

    _, _ = materialize_mid_term_daily_snapshot(
        cleaned_bet_parquet=cleaned_bet_parquet,
        out_parquet=tmp,
        duckdb_runtime=rt,
        canonical_mapping_parquet=canonical_mapping_parquet,
        lookback_days=lb,
        anchor_gaming_day_start=anchor_gaming_day_start,
        anchor_gaming_day_end=anchor_gaming_day_end,
        snapshot_scope=MID_TERM_SNAPSHOT_SCOPE_PRODUCTION,
    )
    _filter_parquet_to_allowlist_canonical_ids(
        src_parquet=tmp,
        dst_parquet=out_parquet,
        adt_allowlist_parquet=adt_allowlist_parquet,
        canonical_mapping_parquet=canonical_mapping_parquet,
        duckdb_runtime=rt,
    )
    tmp.unlink(missing_ok=True)
    dst = Path(out_parquet).resolve()
    anchor_max = None
    pf = pq.ParquetFile(dst)
    if pf.metadata and pf.metadata.num_rows > 0 and "anchor_gaming_day" in pf.schema_arrow.names:
        con = duckdb.connect(database=":memory:")
        try:
            esc = _path_esc(dst)
            raw = con.execute(
                f"SELECT MAX(CAST(anchor_gaming_day AS DATE)) FROM read_parquet('{esc}')"
            ).fetchone()[0]
            if raw is not None:
                anchor_max = pd.Timestamp(raw).date()
        finally:
            con.close()
    meta: dict[str, Any] = {
        "artifact_kind": "mid_term_daily_gaming_day_snapshot",
        "mid_term_grain": MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
        "snapshot_scope": MID_TERM_SNAPSHOT_SCOPE_PRODUCTION,
        "lookback_days": lb,
        "anchor_gaming_day_start": (
            anchor_gaming_day_start.isoformat() if anchor_gaming_day_start is not None else None
        ),
        "anchor_gaming_day_end": (
            anchor_gaming_day_end.isoformat() if anchor_gaming_day_end is not None else None
        ),
        "mid_term_anchor_gaming_day_max": anchor_max.isoformat() if anchor_max is not None else None,
        "sha256": _sha256_file(dst),
        **parquet_row_stats(dst, key_col="canonical_id"),
    }
    write_production_artifact_sidecar(dst, meta)
    if publish_readiness:
        try:
            from trainer_hightier.serving.feast_readiness import (
                layer_readiness_from_production_mid_meta,
                publish_feast_layer_readiness,
            )

            publish_feast_layer_readiness(layer_readiness_from_production_mid_meta(meta))
        except Exception as exc:
            logger.warning("[production_materialize] feast readiness publish skipped: %s", exc)
    return dst, meta


def materialize_production_fe_short_term(
    *,
    cleaned_bet_parquet: Path,
    adt_allowlist_parquet: Path,
    out_parquet: Path,
    canonical_mapping_parquet: Path,
    short_term_columns: tuple[str, ...],
    coverage_hours: int | None = None,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Materialize production **short-term PIT cache** for mirror bet_ids (Route B; not training artifact)."""

    cfg = default_hightier_serving_config()
    hours = int(coverage_hours if coverage_hours is not None else cfg.production_fe_coverage_hours)
    tmp_full = Path(out_parquet).resolve().parent / f"{Path(out_parquet).stem}__full_tmp.parquet"
    full_path, full_meta = materialize_production_fe_derived(
        cleaned_bet_parquet=cleaned_bet_parquet,
        adt_allowlist_parquet=adt_allowlist_parquet,
        out_parquet=tmp_full,
        canonical_mapping_parquet=canonical_mapping_parquet,
        coverage_hours=hours,
        duckdb_runtime=duckdb_runtime,
    )
    cols = tuple(dict.fromkeys(("bet_id", *short_term_columns)))
    dst = Path(out_parquet).resolve()
    dst_esc = _path_esc(dst)
    src_esc = _path_esc(full_path)
    col_sql = ", ".join(f'"{c}"' for c in cols)
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, DuckDbRuntimeConfig())
        con.execute(
            f"COPY (SELECT {col_sql} FROM read_parquet('{src_esc}')) "
            f"TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
        )
    finally:
        con.close()
    tmp_full.unlink(missing_ok=True)
    meta = {
        "artifact_kind": "fe_short_term_production",
        "fe_derived_source_kind": FE_DERIVED_SOURCE_KIND_PRODUCTION,
        "coverage_start": full_meta.get("coverage_start"),
        "coverage_end_exclusive": full_meta.get("coverage_end_exclusive"),
        "coverage_hours": hours,
        "sha256": _sha256_file(dst),
        **parquet_row_stats(dst),
    }
    write_production_artifact_sidecar(dst, meta)
    return dst, meta


def is_training_fe_derived_artifact(path: Path | str | None) -> bool:
    """Heuristic: training-bundle fe parquet paths must not serve production."""

    if path is None:
        return False
    s = str(path).replace("\\", "/").lower()
    return any(m.lower() in s for m in _TRAINING_FE_ARTIFACT_MARKERS)


def default_production_cleaned_bet_path() -> Path:
    """Default cleaned bet mirror root for production refresh."""

    cfg = default_hightier_serving_config()
    if cfg.production_cleaned_bet_mirror_dir is not None:
        return Path(cfg.production_cleaned_bet_mirror_dir).resolve()
    return (
        Path(cfg.snapshot_manifest_dir).resolve().parent
        / "source_mirror"
        / PRODUCTION_BET_MIRROR_DIRNAME
    )


def default_production_cleaned_session_path() -> Path:
    """Default cleaned session mirror parquet for production refresh."""

    cfg = default_hightier_serving_config()
    if cfg.production_cleaned_session_mirror_parquet is not None:
        return Path(cfg.production_cleaned_session_mirror_parquet).resolve()
    return (
        Path(cfg.snapshot_manifest_dir).resolve().parent
        / "source_mirror"
        / PRODUCTION_SESSION_MIRROR_FILENAME
    )


def resolve_production_canonical_mapping(path: Path | None = None) -> Path:
    return Path(path or default_canonical_mapping_parquet_path()).resolve()


def shadow_validate_route_b_features(
    staged: pd.DataFrame,
    *,
    feature_columns: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Shadow-scoring sanity check: non-null fe__* and slow patron columns on staged bets."""

    cols = feature_columns or (
        *DEFAULT_MODEL_FE_DERIVED_COLUMNS,
        *DEFAULT_MODEL_SLOW_PATRON_COLUMNS,
    )
    fe_cols = [c for c in cols if c.startswith("fe__")]
    slow_cols = [c for c in cols if c in DEFAULT_MODEL_SLOW_PATRON_COLUMNS]
    if staged.empty:
        raise ValueError("shadow_validate_route_b_features: staged frame is empty")
    miss = [c for c in cols if c not in staged.columns]
    if miss:
        raise ValueError(f"shadow_validate_route_b_features: missing columns {miss}")

    max_fe_miss = (
        int(staged[fe_cols].isna().sum(axis=1).max()) if fe_cols else 0
    )
    slow_null_fracs = {
        c: float(staged[c].isna().mean()) for c in slow_cols
    }
    slow_max = max(slow_null_fracs.values()) if slow_null_fracs else 0.0
    if max_fe_miss >= len(staged) and fe_cols:
        raise ValueError(
            "shadow_validate_route_b_features: all fe__* null for all rows "
            f"(fe_cols={len(fe_cols)}, rows={len(staged)})"
        )
    if slow_max >= 1.0 and slow_cols:
        raise ValueError(
            f"shadow_validate_route_b_features: slow patron all-null (fracs={slow_null_fracs})"
        )
    return {
        "rows": int(len(staged)),
        "fe_features_missing_max": int(max_fe_miss),
        "slow_null_fraction_max": float(slow_max),
        "slow_null_fractions": slow_null_fracs,
    }
