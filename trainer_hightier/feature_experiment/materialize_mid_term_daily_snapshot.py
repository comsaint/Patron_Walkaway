"""Materialize mid-term ``fe__*`` as canonical daily ``gaming_day`` snapshots."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS,
    MID_TERM_SNAPSHOT_SCOPE_PRODUCTION,
    MID_TERM_SNAPSHOT_SCOPE_TRAINING,
)
from trainer_hightier.utils.bet_l0_preprocess import (
    cleaned_bet_artifact_fingerprint_block,
    cleaned_bet_dataset_has_any_parquet,
    resolved_cleaned_bet_read_parquet_sql,
)
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas, execute_sql_with_progress

logger = logging.getLogger(__name__)

MID_TERM_CACHE_SCHEMA_VERSION = 1

MID_TERM_SNAPSHOT_OUTPUT_COLUMNS: tuple[str, ...] = (
    "canonical_id",
    "anchor_gaming_day",
    "fe__bets_cnt__w1d",
    "fe__wager_sum__w1d",
    "fe__bets_cnt__w7d",
    "fe__wager_sum__w7d",
    "fe__bets_cnt__w30d",
    "fe__wager_sum__w30d",
    "fe__prior_wager_mean_w30d",
    "fe__prior_wager_std_w30d",
    "fe__prior_odds_mean_w30d",
    "fe__prior_odds_std_w30d",
    "fe__std_wager_w7d",
    "fe__avg_abs_wager_w7d",
    "fe__interarrival_avg_w7d",
    "fe__interarrival_std_w7d",
    "fe__max_pcd_w7d",
    "fe__min_pcd_w7d",
    "fe__payout_odds_avg_w7d",
    "fe__payout_odds_std_w7d",
)


def _path_esc(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "''")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _module_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _parquet_quick_stat(path: Path) -> dict[str, Any]:
    p = Path(path).resolve()
    st = p.stat()
    meta = pq.ParquetFile(p).metadata
    nrows = int(meta.num_rows) if meta is not None else -1
    return {
        "path": str(p),
        "mtime_ns": int(st.st_mtime_ns),
        "size_bytes": int(st.st_size),
        "num_rows": int(nrows),
    }


def _cache_manifest_path(out_parquet: Path) -> Path:
    return Path(out_parquet).resolve().parent / f"{Path(out_parquet).stem}.cache.manifest.json"


def _meta_sidecar_path(out_parquet: Path) -> Path:
    return Path(out_parquet).resolve().parent / f"{Path(out_parquet).stem}.meta.json"


def _daily_snapshot_sql(
    *,
    bet_from: str,
    cmap_esc: str,
    lookback_days: int,
    anchor_start: date | None,
    anchor_end: date | None,
    canonical_universe_esc: str | None,
    bets_gday_start: date | None,
    bets_gday_end: date | None,
) -> str:
    """Build window-driven SQL for canonical daily mid-term snapshots."""

    _ = int(max(1, lookback_days))
    span6 = 6
    span29 = 29
    anchor_pred = "WHERE 1=1"
    if anchor_start is not None:
        anchor_pred += f" AND gday >= DATE '{anchor_start.isoformat()}'"
    if anchor_end is not None:
        anchor_pred += f" AND gday <= DATE '{anchor_end.isoformat()}'"

    bets_pred = "WHERE TRY_CAST(b.\"player_id\" AS BIGINT) IS NOT NULL AND b.\"gaming_day\" IS NOT NULL AND b.\"payout_complete_dtm\" IS NOT NULL"
    if bets_gday_start is not None:
        bets_pred += f" AND CAST(b.\"gaming_day\" AS DATE) >= DATE '{bets_gday_start.isoformat()}'"
    if bets_gday_end is not None:
        bets_pred += f" AND CAST(b.\"gaming_day\" AS DATE) <= DATE '{bets_gday_end.isoformat()}'"

    universe_cte = ""
    universe_join = ""
    if canonical_universe_esc:
        universe_cte = f"""
canon_universe AS (
  SELECT DISTINCT TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
  FROM read_parquet('{canonical_universe_esc}')
  WHERE TRIM(CAST(canonical_id AS VARCHAR)) <> ''
),
""".strip()
        universe_join = "INNER JOIN canon_universe AS u ON COALESCE(c.canonical_id, CAST(b.\"player_id\" AS VARCHAR)) = u.canonical_id"

    return f"""
WITH {universe_cte}cmap AS (
  SELECT DISTINCT
    TRY_CAST(player_id AS BIGINT) AS player_id,
    TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
  FROM read_parquet('{cmap_esc}')
  WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
    AND TRIM(CAST(canonical_id AS VARCHAR)) <> ''
),
bets AS (
  SELECT
    COALESCE(c.canonical_id, CAST(b."player_id" AS VARCHAR)) AS canonical_id,
    CAST(b."gaming_day" AS DATE) AS gday,
    CAST(b."payout_complete_dtm" AS TIMESTAMPTZ) AS pcd,
    TRY_CAST(b."wager" AS DOUBLE) AS wager,
    TRY_CAST(b."payout_odds" AS DOUBLE) AS payout_odds
  FROM {bet_from} AS b
  LEFT JOIN cmap AS c ON TRY_CAST(b."player_id" AS BIGINT) = c.player_id
  {universe_join}
  {bets_pred}
),
ordered AS (
  SELECT
    *,
    LAG(pcd) OVER (PARTITION BY canonical_id ORDER BY pcd) AS lag_pcd
  FROM bets
),
with_iv AS (
  SELECT
    *,
    EXTRACT(epoch FROM (pcd - lag_pcd)) AS interarrival_sec
  FROM ordered
),
rolling AS (
  SELECT
    canonical_id,
    gday,
    pcd,
    interarrival_sec,
    wager,
    payout_odds,
    ROW_NUMBER() OVER (
      PARTITION BY canonical_id, gday ORDER BY pcd DESC
    ) AS rn_day_desc,
    COUNT(*) OVER (PARTITION BY canonical_id, gday) AS fe__bets_cnt__w1d,
    COALESCE(SUM(wager) OVER (PARTITION BY canonical_id, gday), 0.0) AS fe__wager_sum__w1d,
    COUNT(*) OVER w7d AS fe__bets_cnt__w7d,
    COALESCE(SUM(wager) OVER w7d, 0.0) AS fe__wager_sum__w7d,
    COUNT(*) OVER w30d AS fe__bets_cnt__w30d,
    COALESCE(SUM(wager) OVER w30d, 0.0) AS fe__wager_sum__w30d,
    AVG(wager) OVER w30_prior AS fe__prior_wager_mean_w30d,
    STDDEV_POP(wager) OVER w30_prior AS fe__prior_wager_std_w30d,
    AVG(payout_odds) OVER w30_prior AS fe__prior_odds_mean_w30d,
    STDDEV_POP(payout_odds) OVER w30_prior AS fe__prior_odds_std_w30d,
    STDDEV_POP(wager) OVER w7d AS fe__std_wager_w7d,
    AVG(ABS(wager)) OVER w7d AS fe__avg_abs_wager_w7d,
    AVG(interarrival_sec) OVER w7d AS fe__interarrival_avg_w7d,
    STDDEV_POP(interarrival_sec) OVER w7d AS fe__interarrival_std_w7d,
    MAX(pcd) OVER w7d AS fe__max_pcd_w7d,
    MIN(pcd) OVER w7d AS fe__min_pcd_w7d,
    AVG(payout_odds) OVER w7d AS fe__payout_odds_avg_w7d,
    STDDEV_POP(payout_odds) OVER w7d AS fe__payout_odds_std_w7d
  FROM with_iv
  WINDOW
    w7d AS (
      PARTITION BY canonical_id ORDER BY gday
      RANGE BETWEEN INTERVAL '{span6}' DAY PRECEDING AND CURRENT ROW
    ),
    w30d AS (
      PARTITION BY canonical_id ORDER BY gday
      RANGE BETWEEN INTERVAL '{span29}' DAY PRECEDING AND CURRENT ROW
    ),
    w30_prior AS (
      PARTITION BY canonical_id ORDER BY gday
      RANGE BETWEEN INTERVAL '{span29}' DAY PRECEDING AND INTERVAL '1' DAY PRECEDING
    )
)
SELECT
  canonical_id,
  gday AS anchor_gaming_day,
  fe__bets_cnt__w1d,
  fe__wager_sum__w1d,
  fe__bets_cnt__w7d,
  fe__wager_sum__w7d,
  fe__bets_cnt__w30d,
  fe__wager_sum__w30d,
  fe__prior_wager_mean_w30d,
  fe__prior_wager_std_w30d,
  fe__prior_odds_mean_w30d,
  fe__prior_odds_std_w30d,
  fe__std_wager_w7d,
  fe__avg_abs_wager_w7d,
  fe__interarrival_avg_w7d,
  fe__interarrival_std_w7d,
  fe__max_pcd_w7d,
  fe__min_pcd_w7d,
  fe__payout_odds_avg_w7d,
  fe__payout_odds_std_w7d
FROM rolling
{anchor_pred}
  AND rn_day_desc = 1
""".strip()


def compute_training_mid_term_bounds(
    training_parquet: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
    lookback_days: int | None = None,
) -> tuple[date | None, date | None, date | None, date | None]:
    """Return anchor/bets gaming-day bounds for training-scoped mid-term materialization."""

    lb = int(lookback_days if lookback_days is not None else MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS)
    tp = Path(training_parquet).resolve()
    if not tp.is_file():
        raise FileNotFoundError(f"training parquet missing: {tp}")
    esc = _path_esc(tp)
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        row = con.execute(
            f"""
            SELECT
              MIN(CAST(gaming_day AS DATE)) AS mn,
              MAX(CAST(gaming_day AS DATE)) AS mx
            FROM read_parquet('{esc}')
            WHERE gaming_day IS NOT NULL
            """,
        ).fetchone()
    finally:
        con.close()
    if row is None or row[0] is None or row[1] is None:
        return None, None, None, None
    min_gday = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0])[:10])
    max_gday = row[1] if isinstance(row[1], date) else date.fromisoformat(str(row[1])[:10])
    anchor_end = max_gday - timedelta(days=1)
    anchor_start: date | None = None
    bets_gday_end = anchor_end
    bets_gday_start = min_gday - timedelta(days=lb - 1)
    return anchor_start, anchor_end, bets_gday_start, bets_gday_end


def write_training_canonical_universe_parquet(
    training_parquet: Path,
    out_parquet: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> int:
    """Write distinct training ``canonical_id`` values for mid-term universe semi-join."""

    tp = Path(training_parquet).resolve()
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not tp.is_file():
        raise FileNotFoundError(f"training parquet missing: {tp}")
    src_esc = _path_esc(tp)
    dst_esc = _path_esc(dst)
    sql = f"""
COPY (
  SELECT DISTINCT TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
  FROM read_parquet('{src_esc}')
  WHERE canonical_id IS NOT NULL
    AND TRIM(CAST(canonical_id AS VARCHAR)) <> ''
  ORDER BY canonical_id
) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(sql)
    finally:
        con.close()
    pf = pq.ParquetFile(dst)
    return int(pf.metadata.num_rows) if pf.metadata is not None else 0


def mid_term_snapshot_production_safe(meta: dict[str, Any] | None) -> bool:
    """Return True when sidecar metadata marks a production-compatible mid-term snapshot."""

    if not isinstance(meta, dict):
        return False
    return str(meta.get("snapshot_scope", "")).strip() == MID_TERM_SNAPSHOT_SCOPE_PRODUCTION


def _cache_manifest_compatible(
    manifest: dict[str, Any],
    *,
    snapshot_scope: str,
    cleaned_fp: dict[str, Any],
    mapping_fp_sha256: str,
    universe_fp_sha256: str | None,
    lookback_days: int,
    anchor_start: date | None,
    anchor_end: date | None,
    code_fp: str,
    out_stat: dict[str, Any],
) -> bool:
    if int(manifest.get("schema_version", -1)) != MID_TERM_CACHE_SCHEMA_VERSION:
        return False
    if str(manifest.get("snapshot_scope", "")).strip() != snapshot_scope:
        return False
    if str(manifest.get("code_fingerprint", "")).strip() != code_fp:
        return False
    if int(manifest.get("lookback_days", -1)) != int(lookback_days):
        return False
    have_cleaned = manifest.get("cleaned_bet_fingerprint_block")
    if not isinstance(have_cleaned, dict) or have_cleaned != cleaned_fp:
        return False
    if str(manifest.get("canonical_mapping_sha256", "")).strip() != mapping_fp_sha256:
        return False
    have_univ = manifest.get("canonical_universe_sha256")
    if universe_fp_sha256 is None:
        if have_univ is not None:
            return False
    elif str(have_univ or "").strip() != universe_fp_sha256:
        return False
    if str(manifest.get("anchor_gaming_day_start") or "") != (
        anchor_start.isoformat() if anchor_start is not None else ""
    ):
        return False
    if str(manifest.get("anchor_gaming_day_end") or "") != (
        anchor_end.isoformat() if anchor_end is not None else ""
    ):
        return False
    have_out = manifest.get("output_parquet_stat")
    if not isinstance(have_out, dict):
        return False
    return (
        have_out.get("num_rows") == out_stat.get("num_rows")
        and have_out.get("size_bytes") == out_stat.get("size_bytes")
    )


def _write_cache_manifest(
    manifest_path: Path,
    *,
    snapshot_scope: str,
    cleaned_fp: dict[str, Any],
    mapping_fp_sha256: str,
    universe_fp_sha256: str | None,
    lookback_days: int,
    anchor_start: date | None,
    anchor_end: date | None,
    code_fp: str,
    out_stat: dict[str, Any],
) -> None:
    blob = {
        "schema_version": MID_TERM_CACHE_SCHEMA_VERSION,
        "snapshot_scope": snapshot_scope,
        "code_fingerprint": code_fp,
        "lookback_days": int(lookback_days),
        "cleaned_bet_fingerprint_block": cleaned_fp,
        "canonical_mapping_sha256": mapping_fp_sha256,
        "canonical_universe_sha256": universe_fp_sha256,
        "anchor_gaming_day_start": anchor_start.isoformat() if anchor_start is not None else None,
        "anchor_gaming_day_end": anchor_end.isoformat() if anchor_end is not None else None,
        "output_parquet_stat": out_stat,
    }
    manifest_path.write_text(json.dumps(blob, indent=2, sort_keys=True), encoding="utf-8")


def try_reuse_mid_term_snapshot_cache(
    out_parquet: Path,
    *,
    snapshot_scope: str,
    cleaned_bet_parquet: Path,
    canonical_mapping_parquet: Path,
    canonical_universe_parquet: Path | None,
    lookback_days: int,
    anchor_gaming_day_start: date | None,
    anchor_gaming_day_end: date | None,
) -> tuple[Path, dict[str, Any]] | None:
    """Return cached snapshot metadata when manifest matches; otherwise ``None``."""

    dst = Path(out_parquet).resolve()
    mpath = _cache_manifest_path(dst)
    if not dst.is_file() or not mpath.is_file():
        return None
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    cmap_path = Path(canonical_mapping_parquet).resolve()
    cleaned_fp = cleaned_bet_artifact_fingerprint_block(Path(cleaned_bet_parquet).resolve())
    universe_fp = _sha256_file(canonical_universe_parquet) if canonical_universe_parquet is not None else None
    out_stat = _parquet_quick_stat(dst)
    if not _cache_manifest_compatible(
        manifest,
        snapshot_scope=snapshot_scope,
        cleaned_fp=cleaned_fp,
        mapping_fp_sha256=_sha256_file(cmap_path),
        universe_fp_sha256=universe_fp,
        lookback_days=int(lookback_days),
        anchor_start=anchor_gaming_day_start,
        anchor_end=anchor_gaming_day_end,
        code_fp=_module_sha256(),
        out_stat=out_stat,
    ):
        return None
    meta_path = _meta_sidecar_path(dst)
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                meta = dict(meta)
                meta["cache_hit"] = True
                logger.info(
                    "[mid_term_daily_snapshot] cache hit scope=%s rows=%s -> %s",
                    snapshot_scope,
                    out_stat.get("num_rows"),
                    dst,
                )
                return dst, meta
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    meta = {
        "artifact_kind": "mid_term_daily_gaming_day_snapshot",
        "snapshot_scope": snapshot_scope,
        "row_count": out_stat.get("num_rows"),
        "path": str(dst),
        "cache_hit": True,
    }
    logger.info(
        "[mid_term_daily_snapshot] cache hit scope=%s rows=%s -> %s",
        snapshot_scope,
        out_stat.get("num_rows"),
        dst,
    )
    return dst, meta


def materialize_mid_term_daily_snapshot(
    *,
    cleaned_bet_parquet: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    canonical_mapping_parquet: Path | None = None,
    canonical_universe_parquet: Path | None = None,
    lookback_days: int | None = None,
    anchor_gaming_day_start: date | None = None,
    anchor_gaming_day_end: date | None = None,
    bets_gaming_day_start: date | None = None,
    bets_gaming_day_end: date | None = None,
    snapshot_scope: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Write canonical mid-term daily snapshots keyed by ``anchor_gaming_day``."""

    src_root = Path(cleaned_bet_parquet).resolve()
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmap_path = (
        Path(canonical_mapping_parquet).resolve()
        if canonical_mapping_parquet is not None
        else default_canonical_mapping_parquet_path().resolve()
    )
    if not cleaned_bet_dataset_has_any_parquet(src_root):
        raise FileNotFoundError(f"No cleaned bet parquet under {src_root}")
    if not cmap_path.is_file():
        raise FileNotFoundError(f"canonical mapping parquet missing: {cmap_path}")
    if canonical_universe_parquet is not None and not Path(canonical_universe_parquet).is_file():
        raise FileNotFoundError(f"canonical universe parquet missing: {canonical_universe_parquet}")

    scope = str(snapshot_scope).strip() if snapshot_scope is not None else ""
    lb = int(lookback_days if lookback_days is not None else MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS)
    bet_from = resolved_cleaned_bet_read_parquet_sql(src_root)
    universe_esc = _path_esc(canonical_universe_parquet) if canonical_universe_parquet is not None else None
    sql = _daily_snapshot_sql(
        bet_from=bet_from,
        cmap_esc=_path_esc(cmap_path),
        lookback_days=lb,
        anchor_start=anchor_gaming_day_start,
        anchor_end=anchor_gaming_day_end,
        canonical_universe_esc=universe_esc,
        bets_gday_start=bets_gaming_day_start,
        bets_gday_end=bets_gaming_day_end,
    )
    dst_esc = _path_esc(dst)
    copy_sql = f"COPY ({sql}) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
    t0 = time.perf_counter()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        execute_sql_with_progress(con, copy_sql, desc="[Step 3.5] mid-term daily snapshot")
    finally:
        con.close()
    elapsed = round(time.perf_counter() - t0, 3)

    pf = pq.ParquetFile(dst)
    nrows = int(pf.metadata.num_rows) if pf.metadata is not None else 0
    schema_names = set(pf.schema_arrow.names)
    miss = [c for c in MID_TERM_SNAPSHOT_OUTPUT_COLUMNS if c not in schema_names]
    if miss:
        raise ValueError(f"mid-term snapshot missing columns {miss}; got {sorted(schema_names)}")

    anchor_max = None
    if nrows > 0 and "anchor_gaming_day" in schema_names:
        con2 = duckdb.connect(database=":memory:")
        try:
            raw = con2.execute(
                f"SELECT MAX(CAST(anchor_gaming_day AS DATE)) FROM read_parquet('{dst_esc}')",
            ).fetchone()[0]
            if raw is not None:
                anchor_max = raw.isoformat() if isinstance(raw, date) else str(raw)[:10]
        finally:
            con2.close()

    universe_fp = _sha256_file(canonical_universe_parquet) if canonical_universe_parquet is not None else None
    meta: dict[str, Any] = {
        "artifact_kind": "mid_term_daily_gaming_day_snapshot",
        "snapshot_scope": scope or None,
        "row_count": nrows,
        "lookback_days": lb,
        "anchor_gaming_day_start": (
            anchor_gaming_day_start.isoformat() if anchor_gaming_day_start is not None else None
        ),
        "anchor_gaming_day_end": anchor_gaming_day_end.isoformat() if anchor_gaming_day_end is not None else None,
        "bets_gaming_day_start": (
            bets_gaming_day_start.isoformat() if bets_gaming_day_start is not None else None
        ),
        "bets_gaming_day_end": bets_gaming_day_end.isoformat() if bets_gaming_day_end is not None else None,
        "canonical_universe_sha256": universe_fp,
        "mid_term_anchor_gaming_day_max": anchor_max,
        "materialize_seconds": elapsed,
        "sha256": _sha256_file(dst),
        "path": str(dst),
        "cache_hit": False,
    }
    _meta_sidecar_path(dst).write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    out_stat = _parquet_quick_stat(dst)
    if scope:
        _write_cache_manifest(
            _cache_manifest_path(dst),
            snapshot_scope=scope,
            cleaned_fp=cleaned_bet_artifact_fingerprint_block(src_root),
            mapping_fp_sha256=_sha256_file(cmap_path),
            universe_fp_sha256=universe_fp,
            lookback_days=lb,
            anchor_start=anchor_gaming_day_start,
            anchor_end=anchor_gaming_day_end,
            code_fp=_module_sha256(),
            out_stat=out_stat,
        )
    logger.info(
        "[mid_term_daily_snapshot] wrote scope=%s rows=%d anchor_end=%s elapsed=%.3fs -> %s",
        scope or "unspecified",
        nrows,
        anchor_gaming_day_end,
        elapsed,
        dst,
    )
    return dst, meta
