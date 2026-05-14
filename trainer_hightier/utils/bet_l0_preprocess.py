"""Bet L0 preprocess: ``t_bet`` → cleaned Parquet (DuckDB).

Downstream training assumes session clean exists first; bet clean cache fingerprints
optionally include the **cleaned session** Parquet stats so session rewrites invalidate bet cache.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Sequence

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import yaml
from trainer.training.data_sources import _BET_INGEST_READ_COLS_ORDERED
from trainer_hightier.config import BetPreprocessConfig, DuckDbRuntimeConfig
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas, execute_sql_with_progress

logger = logging.getLogger("trainer_hightier")

_BET_CLEAN_CACHE_MANIFEST_VERSION = 7


def _duckdb_quote_ident(name: str) -> str:
    """Return a double-quoted DuckDB identifier (escape embedded quotes)."""
    return '"' + str(name).replace('"', '""') + '"'


def _adt_allowlist_distinct_player_ids_fingerprint(
    allowlist_parquet: Path,
) -> tuple[str, int]:
    """SHA-256 over sorted distinct allowlist ``player_id`` values (DuckDB BIGINT semantics).

    Mirrors the early-join CTE:
    ``SELECT DISTINCT TRY_CAST(player_id AS BIGINT) ... WHERE ... IS NOT NULL``.

    Parameters
    ----------
    allowlist_parquet
        ADT allowlist Parquet path; must contain a ``player_id`` column.

    Returns
    -------
    tuple[str, int]
        ``(sha256_hex, distinct_player_id_count)``.
    """
    ap = Path(allowlist_parquet).resolve()
    if not ap.is_file():
        raise FileNotFoundError(ap)
    names = frozenset(pq.read_schema(ap).names)
    if "player_id" not in names:
        raise ValueError(
            "ADT allowlist Parquet missing player_id column "
            f"(need early-join column set); got {sorted(names)}"
        )
    tbl = pq.read_table(ap, columns=["player_id"])
    series = tbl.column(0).combine_chunks().to_pandas()
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        uniq = np.array([], dtype=np.int64)
    else:
        as_float = numeric.to_numpy(dtype=np.float64, copy=False)
        as_bigint = np.trunc(as_float).astype(np.int64, copy=False)
        uniq = np.unique(as_bigint)
    payload_gen = (str(int(x)).encode("ascii") for x in uniq.tolist())
    payload = b"\n".join(payload_gen)
    digest = hashlib.sha256(payload).hexdigest()
    return digest, int(uniq.size)


def _adt_segment_cache_block(
    *,
    quantile: float | None,
    adt_allowed_players_parquet: Path | None,
) -> dict[str, Any] | None:
    """Fingerprint fragment for ADT-based bet segmentation (optional).

    Uses **content** of the allowlist Parquet (distinct ``player_id`` set), not upstream CSV/Parquet mtimes,
    so regenerating ``canonical_patron_profile.csv`` without changing the allowlist does not invalidate bet cache.
    """
    if quantile is None:
        return None
    qf = float(quantile)
    if not (0.0 < qf < 1.0):
        raise ValueError(f"adt_filter_quantile must be strictly between 0 and 1, got {qf!r}")
    if adt_allowed_players_parquet is None:
        raise ValueError(
            "adt_allowed_players_parquet is required when adt_filter_quantile is set "
            "(bet cache fingerprint)."
        )
    ap_f = Path(adt_allowed_players_parquet).resolve()
    if not ap_f.is_file():
        raise FileNotFoundError(f"ADT allowlist Parquet missing for bet cache fingerprint: {ap_f}")
    digest, n_ids = _adt_allowlist_distinct_player_ids_fingerprint(ap_f)
    return {
        "quantile": qf,
        "adt_allowlist_player_ids_sha256_hex": digest,
        "adt_allowlist_distinct_player_id_count": n_ids,
    }


def _resolve_adt_allowed_players_posix(cfg: BetPreprocessConfig) -> str | None:
    """Return escaped POSIX path for ADT allowlist Parquet, or ``None`` if ADT filter disabled."""
    q = cfg.adt_filter_quantile
    if q is None:
        return None
    ap = cfg.adt_allowed_players_parquet
    if ap is None:
        raise ValueError(
            "BetPreprocessConfig.adt_filter_quantile requires adt_allowed_players_parquet "
            "(materialize via patron_session_metrics.materialize_adt_allowed_players_parquet)."
        )
    ap_f = Path(ap).resolve()
    if not ap_f.is_file():
        raise FileNotFoundError(f"ADT allowlist Parquet missing for bet preprocess: {ap_f}")
    qf = float(q)
    if not (0.0 < qf < 1.0):
        raise ValueError(f"adt_filter_quantile must be strictly between 0 and 1, got {qf!r}")
    return _path_posix(ap_f).replace("'", "''")


def _path_posix(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def default_preprocess_registry_yaml_path() -> Path:
    """Repo ``schema/preprocess_l0_data_contract_registry.yaml``."""
    return Path(__file__).resolve().parents[2] / "schema" / "preprocess_l0_data_contract_registry.yaml"


def _placeholder_player_id_i64() -> int:
    """Trainer sentinel ``player_id`` (default -1); used in bet DQ."""
    try:
        from trainer.core._config_training_domain import PLACEHOLDER_PLAYER_ID as ph

        return int(ph)
    except Exception:
        return -1


def _resolve_preprocess_registry(registry_yaml: Path | None) -> Path:
    """Resolve ingest registry YAML path; default canonical repo schema file."""
    path = registry_yaml if registry_yaml is not None else default_preprocess_registry_yaml_path()
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Ingest registry YAML not found: {p}")
    return p


def _bet_cap_applied_rules(registry_yaml: Path) -> tuple[int, list[str]]:
    """Load ``tables.t_bet`` and return ``BET-INGEST-FIX-004`` cap seconds plus manifest tags."""
    try:
        from pipelines.layered_data_assets.core.preprocess_bet_ingestion_fix_registry_v1 import (
            load_preprocess_bet_ingestion_fix_registry,
            resolve_bet_ingest_fix004_cap_binding,
        )
    except ModuleNotFoundError as exc:
        raise ImportError(
            "Bet preprocess requires ``pipelines.layered_data_assets.core.preprocess_bet_ingestion_fix_registry_v1``."
        ) from exc
    doc = load_preprocess_bet_ingestion_fix_registry(registry_yaml)
    cap, _fid, _fver, applied = resolve_bet_ingest_fix004_cap_binding(doc)
    return int(cap), list(applied)


def bulk_bet_episode_calendar_tags(registry_yaml: Path) -> tuple[tuple[str, str], ...]:
    """``(calendar day, episode_id)`` pairs from ``tables.t_bet`` bulk episodes."""
    raw = yaml.safe_load(Path(registry_yaml).read_text(encoding="utf-8"))
    tables = raw.get("tables") if isinstance(raw, dict) else None
    if not isinstance(tables, dict):
        raise ValueError("registry root must contain tables:")
    tbet = tables.get("t_bet")
    if not isinstance(tbet, dict):
        raise ValueError("registry tables.t_bet missing")
    bulk = tbet.get("bulk_historical_ingest_episodes")
    if not isinstance(bulk, dict):
        return ()
    eps = bulk.get("episodes")
    if not isinstance(eps, list):
        return ()
    out: list[tuple[str, str]] = []
    for ep in eps:
        if not isinstance(ep, dict):
            continue
        eid = ep.get("episode_id")
        sql_frag = ep.get("match_rule_sql") or ""
        if not isinstance(eid, str) or not isinstance(sql_frag, str):
            continue
        m = re.search(r"DATE\s+'(\d{4}-\d{2}-\d{2})'", sql_frag.replace("\n", " "))
        if not m:
            continue
        out.append((m.group(1), eid.strip()))
    return tuple(out)


def _bet_episode_scalar_sql(tags: tuple[tuple[str, str], ...], *, obs_alias: str) -> str:
    """DuckDB ``CASE`` expression mapping synthetic observed calendar day → episode id."""
    if not tags:
        return "CAST(NULL AS VARCHAR)"
    parts: list[str] = []
    for day, eid in tags:
        esc = str(eid).replace("'", "''")
        parts.append(
            f"WHEN CAST(date_trunc('day', {obs_alias}.\"__etl_insert_Dtm_synthetic\") AS DATE) "
            f"= DATE '{day}' THEN '{esc}'"
        )
    return "CASE " + " ".join(parts) + " ELSE CAST(NULL AS VARCHAR) END"


def _bet_preprocess_read_columns_ordered(schema_names: frozenset[str]) -> tuple[str, ...]:
    """Verify ``gmwds_t_bet`` has full GDP ingest list; return fixed read order."""

    missing = tuple(c for c in _BET_INGEST_READ_COLS_ORDERED if c not in schema_names)
    if missing:
        raise ValueError(
            "gmwds_t_bet Parquet missing columns required for ingest/preprocess "
            f"(trainer.training.data_sources._BET_INGEST_READ_COLS_ORDERED): {list(missing)}"
        )
    return _BET_INGEST_READ_COLS_ORDERED


def _bet_optional_flag_sql(names: frozenset[str]) -> list[str]:
    """DQ fragments matching ``preprocess_bet_v1`` for optional cancelled/deleted/manual flags."""
    parts: list[str] = []
    if "is_deleted" in names:
        parts.append(
            '(TRY_CAST("is_deleted" AS INTEGER) IS NULL OR TRY_CAST("is_deleted" AS INTEGER) = 0)'
        )
    if "is_canceled" in names:
        parts.append(
            '(TRY_CAST("is_canceled" AS INTEGER) IS NULL OR TRY_CAST("is_canceled" AS INTEGER) = 0)'
        )
    if "is_manual" in names:
        parts.append(
            '(TRY_CAST("is_manual" AS INTEGER) IS NULL OR TRY_CAST("is_manual" AS INTEGER) = 0)'
        )
    return parts


def _bet_dq_where_sql(names: frozenset[str]) -> str:
    """Combined ``WHERE`` for rated bet rows (trainer ingress / preprocess_bet parity)."""
    ph = _placeholder_player_id_i64()
    frag = [
        'TRY_CAST("bet_id" AS DOUBLE) IS NOT NULL',
        f'TRY_CAST("player_id" AS BIGINT) IS NOT NULL '
        f'AND TRY_CAST("player_id" AS BIGINT) <> {ph}',
        'TRY_CAST("session_id" AS DOUBLE) IS NOT NULL',
        'TRY_CAST("payout_complete_dtm" AS TIMESTAMP) IS NOT NULL',
        'TRY_CAST("gaming_day" AS DATE) IS NOT NULL',
        'COALESCE(TRY_CAST("wager" AS DOUBLE), 0.0) > 0',
    ]
    frag.extend(_bet_optional_flag_sql(names))
    return " AND ".join(frag)


def _bet_feast_prediction_visible_alignment_params() -> tuple[int, int]:
    """Return ``(bet_avail_delay_min, poll_interval_sec)`` aligned with scorer defaults.

    Matches :data:`trainer.core._config_training_domain.BET_AVAIL_DELAY_MIN` and
    :data:`trainer.core._config_serving_runtime.SCORER_POLL_INTERVAL_SECONDS`
    when ``trainer`` is importable.
    """
    try:
        from trainer.core._config_serving_runtime import SCORER_POLL_INTERVAL_SECONDS
        from trainer.core._config_training_domain import BET_AVAIL_DELAY_MIN

        return int(BET_AVAIL_DELAY_MIN), int(SCORER_POLL_INTERVAL_SECONDS)
    except ImportError:
        return 1, 45


def _duckdb_read_parquet_sources_sql(paths: list[Path]) -> str:
    """Build DuckDB ``read_parquet([...])`` or single-file variant."""
    if not paths:
        raise ValueError("bet paths empty")
    if len(paths) == 1:
        esc = _path_posix(paths[0].resolve()).replace("'", "''")
        return f"read_parquet('{esc}')"
    parts = [_path_posix(p.resolve()).replace("'", "''") for p in paths]
    inner = ", ".join(f"'{p}'" for p in parts)
    return "read_parquet([" + inner + "])"


def _duckdb_bet_clean_pipeline_select_sql(
    *,
    src_read_parquet_clause: str,
    read_cols_ordered: tuple[str, ...],
    cap_sec: int,
    tags: tuple[tuple[str, str], ...],
    dedup_bucket_id: int | None = None,
    dedup_buckets: int = 1,
    adt_allowed_players_posix: str | None = None,
    bet_avail_delay_min: int | None = None,
    poll_interval_sec: int | None = None,
) -> str:
    """DuckSQL: optional ADT allowlist join → DQ → synthetic cap → episode tag → ``bet_id`` dedup.

    When **adt_allowed_players_posix** is set, raw bets inner-join that Parquet on ``player_id``
    before DQ so the heavy pipeline never sees off-segment rows.
    """

    n = int(dedup_buckets)
    if n < 1:
        raise ValueError(f"dedup_buckets must be >= 1, got {n}")
    bucket_filter = ""
    if n > 1:
        if dedup_bucket_id is None:
            raise ValueError("dedup_bucket_id is required when dedup_buckets > 1")
        b = int(dedup_bucket_id)
        if b < 0 or b >= n:
            raise ValueError(f"dedup_bucket_id must be in [0, {n}), got {b}")
        bucket_filter = f" AND (mod(abs(hash(TRY_CAST(\"bet_id\" AS DOUBLE))), {n}) = {b})"

    names = frozenset(read_cols_ordered)
    l0_where = _bet_dq_where_sql(names)
    cap = int(cap_sec)
    if bet_avail_delay_min is None or poll_interval_sec is None:
        _adm, _poll = _bet_feast_prediction_visible_alignment_params()
        bet_avail_delay_min = int(_adm if bet_avail_delay_min is None else bet_avail_delay_min)
        poll_interval_sec = int(_poll if poll_interval_sec is None else poll_interval_sec)
    else:
        bet_avail_delay_min = int(bet_avail_delay_min)
        poll_interval_sec = int(poll_interval_sec)
    if bet_avail_delay_min < 0:
        raise ValueError(f"bet_avail_delay_min must be >= 0, got {bet_avail_delay_min}")
    if poll_interval_sec < 1:
        raise ValueError(f"poll_interval_sec must be >= 1, got {poll_interval_sec}")
    l0_projection = ", ".join(_duckdb_quote_ident(c) for c in read_cols_ordered)
    if adt_allowed_players_posix is not None:
        with_lf = f"""WITH allowed AS (
  SELECT DISTINCT TRY_CAST(player_id AS BIGINT) AS player_id
  FROM read_parquet('{adt_allowed_players_posix}')
  WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
),
lf AS (
  SELECT raw.*
  FROM (
    SELECT {l0_projection} FROM {src_read_parquet_clause}
    WHERE {l0_where}{bucket_filter}
  ) AS raw
  INNER JOIN allowed AS ap ON TRY_CAST(raw.player_id AS BIGINT) = ap.player_id
)"""
    else:
        with_lf = f"""WITH lf AS (
  SELECT {l0_projection} FROM {src_read_parquet_clause}
  WHERE {l0_where}{bucket_filter}
)"""
    syn = f"""CASE
    WHEN TRY_CAST(lf."__etl_insert_Dtm" AS TIMESTAMP) IS NULL
      OR TRY_CAST(lf."payout_complete_dtm" AS TIMESTAMP) IS NULL
    THEN NULL
    ELSE LEAST(
      TRY_CAST(lf."__etl_insert_Dtm" AS TIMESTAMP),
      TRY_CAST(lf."payout_complete_dtm" AS TIMESTAMP) + INTERVAL {cap} SECOND
    )
  END"""
    eps = _bet_episode_scalar_sql(tags, obs_alias="obs")
    fin_tail = """
fin AS ( SELECT * EXCLUDE (_rn) FROM ranked WHERE _rn = 1 )
SELECT *
FROM fin
"""
    return f"""
{with_lf},
obs AS (
  SELECT
    lf.*,
    {syn} AS "__etl_insert_Dtm_synthetic"
  FROM lf AS lf
),
tagged AS (
  SELECT
    obs.*,
    {eps} AS "ingestion_episode_id"
  FROM obs AS obs
),
aligned AS (
  SELECT
    tagged.*,
    to_timestamp(
      ceil(
        epoch(
          GREATEST(
            COALESCE(
              tagged."__etl_insert_Dtm_synthetic",
              TRY_CAST(tagged."payout_complete_dtm" AS TIMESTAMP)
                + INTERVAL {bet_avail_delay_min} MINUTE
            ),
            TRY_CAST(tagged."payout_complete_dtm" AS TIMESTAMP)
              + INTERVAL {bet_avail_delay_min} MINUTE
          )
        ) / {poll_interval_sec}
      ) * {poll_interval_sec}
    ) AS prediction_visible_ts_cf
  FROM tagged AS tagged
),
ranked AS (
  SELECT
    aligned.*,
    ROW_NUMBER() OVER (
      PARTITION BY TRY_CAST(aligned."bet_id" AS DOUBLE)
      ORDER BY
        aligned."__etl_insert_Dtm_synthetic" DESC NULLS LAST,
        TRY_CAST(aligned."payout_complete_dtm" AS TIMESTAMP) DESC NULLS LAST,
        TRY_CAST(aligned."__etl_insert_Dtm" AS TIMESTAMP) DESC NULLS LAST,
        TRY_CAST(aligned."__ts_ms" AS BIGINT) DESC NULLS LAST
    ) AS _rn
  FROM aligned
),
{fin_tail.strip()}
""".strip()


def _preprocess_bets_duckdb_single_copy(
    source_parquets: list[Path],
    output_path: Path,
    *,
    registry_yaml: Path,
    duckdb_cfg: DuckDbRuntimeConfig,
    cfg: BetPreprocessConfig,
) -> tuple[int, tuple[tuple[str, str], ...]]:
    """Run DuckDB ``COPY`` pipeline; multi-pass hash buckets then merge when *dedup_hash_buckets* > 1."""

    if not source_parquets:
        raise ValueError("source_parquets must not be empty")
    out = Path(output_path).resolve()
    n = int(cfg.dedup_hash_buckets)
    if n < 1:
        raise ValueError(f"dedup_hash_buckets must be >= 1, got {n}")
    schema_names = frozenset(pq.read_schema(Path(source_parquets[0])).names)
    read_ordered = _bet_preprocess_read_columns_ordered(schema_names)
    cap_sec, _applied = _bet_cap_applied_rules(registry_yaml)
    tags = bulk_bet_episode_calendar_tags(registry_yaml)
    src_clause = _duckdb_read_parquet_sources_sql(source_parquets)
    out_esc = _path_posix(out).replace("'", "''")
    adt_allowed_esc = _resolve_adt_allowed_players_posix(cfg)
    if adt_allowed_esc is not None:
        logger.info("[Step 2b] bet preprocess ADT segment: early join to allowlist Parquet")

    if n == 1:
        con = duckdb.connect(database=":memory:")
        try:
            apply_duckdb_runtime_pragmas(con, duckdb_cfg)
            inner = _duckdb_bet_clean_pipeline_select_sql(
                src_read_parquet_clause=src_clause,
                read_cols_ordered=read_ordered,
                cap_sec=cap_sec,
                tags=tags,
                dedup_bucket_id=None,
                dedup_buckets=1,
                adt_allowed_players_posix=adt_allowed_esc,
            )
            sql = f"COPY ({inner}) TO '{out_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
            execute_sql_with_progress(con, sql, desc="[Step 2b] DuckDB bet COPY")
        finally:
            con.close()
    else:
        # Fresh connection per bucket (and merge): one long-lived :memory: run can retain
        # allocator peaks across buckets; t_bet dedup windows are heavier than session.
        with tempfile.TemporaryDirectory(prefix="hightier_bet_bkt_", dir=out.parent) as tdir:
            parts_dir = Path(tdir)
            for b in range(n):
                inner = _duckdb_bet_clean_pipeline_select_sql(
                    src_read_parquet_clause=src_clause,
                    read_cols_ordered=read_ordered,
                    cap_sec=cap_sec,
                    tags=tags,
                    dedup_bucket_id=b,
                    dedup_buckets=n,
                    adt_allowed_players_posix=adt_allowed_esc,
                )
                part_p = parts_dir / f"part_{b:04d}.parquet"
                part_esc = _path_posix(part_p).replace("'", "''")
                bsql = f"COPY ({inner}) TO '{part_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
                con_b = duckdb.connect(database=":memory:")
                try:
                    apply_duckdb_runtime_pragmas(con_b, duckdb_cfg)
                    execute_sql_with_progress(
                        con_b,
                        bsql,
                        desc=f"[Step 2b] DuckDB bet COPY bucket {b + 1}/{n}",
                    )
                finally:
                    con_b.close()
            glob_pat = str(parts_dir / "part_*.parquet").replace("\\", "/").replace("'", "''")
            merge_sql = (
                f"COPY (SELECT * FROM read_parquet('{glob_pat}')) "
                f"TO '{out_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
            )
            con_m = duckdb.connect(database=":memory:")
            try:
                apply_duckdb_runtime_pragmas(con_m, duckdb_cfg)
                execute_sql_with_progress(con_m, merge_sql, desc="[Step 2b] DuckDB bet merge buckets")
            finally:
                con_m.close()
    return cap_sec, tags


def preprocess_bets_from_parquet_streaming(
    bet_parquet: Path,
    output_path: Path,
    *,
    cfg: BetPreprocessConfig | None = None,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    extra_partition_sources: tuple[Path, ...] | None = None,
) -> Path:
    """Clean full L0 ``gmwds_t_bet`` Parquet (DQ + synthetic observed cap + episode tags + dedupe)."""
    src = Path(bet_parquet).resolve()
    if not src.is_file():
        raise FileNotFoundError(src)
    sources_list: list[Path] = [src]
    if extra_partition_sources:
        uniq = {str(src): src}
        for pp in extra_partition_sources:
            p = Path(pp).resolve()
            if not p.is_file():
                raise FileNotFoundError(p)
            uniq[str(p)] = p
        sources_list = sorted(uniq.values(), key=lambda x: str(x))
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file():
        out.unlink()
    _cfg = cfg if cfg is not None else BetPreprocessConfig()
    if _cfg.engine != "duckdb":
        raise ValueError(
            f"t_bet preprocess engine {_cfg.engine!r} unsupported; use BetPreprocessConfig(engine='duckdb')."
        )
    reg = _resolve_preprocess_registry(_cfg.preprocess_registry_yaml)
    _ddb = duckdb_runtime if duckdb_runtime is not None else DuckDbRuntimeConfig()
    _nb = int(_cfg.dedup_hash_buckets)
    if _nb < 1:
        raise ValueError(f"BetPreprocessConfig.dedup_hash_buckets must be >= 1, got {_nb}")
    logger.info(
        "[Step 2b] bet preprocess: DuckDB COPY registry=%s dedup_hash_buckets=%d -> %s",
        reg,
        _nb,
        out,
    )
    cap_sec, tags = _preprocess_bets_duckdb_single_copy(
        sources_list, out, registry_yaml=reg, duckdb_cfg=_ddb, cfg=_cfg
    )
    nrows = int(pq.ParquetFile(out).metadata.num_rows) if pq.ParquetFile(out).metadata else 0
    logger.info(
        "[Step 2b] bet preprocess done: rows=%d cap_sec=%s bulk_episode_days=%s written %s",
        nrows,
        cap_sec,
        len(tags),
        out,
    )
    return out


def default_cleaned_bet_base_parquet_path() -> Path:
    """Intermediate all-players cleaned bet prior to optional ADT projection."""
    return Path(__file__).resolve().parents[1] / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet_base.parquet"


def segment_cleaned_bet_from_base_parquet(
    base_cleaned_parquet: Path,
    allowlist_parquet: Path,
    output_parquet: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> Path:
    """Project cleaned all-players bets to ADT segment via semi-join allowlist."""

    base = Path(base_cleaned_parquet).resolve()
    allow = Path(allowlist_parquet).resolve()
    out = Path(output_parquet).resolve()
    if not base.is_file():
        raise FileNotFoundError(base)
    if not allow.is_file():
        raise FileNotFoundError(allow)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file():
        out.unlink()
    b_esc = _path_posix(base).replace("'", "''")
    a_esc = _path_posix(allow).replace("'", "''")
    o_esc = _path_posix(out).replace("'", "''")
    sql = f"""
COPY (
  SELECT DISTINCT b.*
  FROM read_parquet('{b_esc}') AS b
  INNER JOIN (
    SELECT TRY_CAST(player_id AS BIGINT) AS pid
    FROM read_parquet('{a_esc}')
    WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
  ) AS a ON TRY_CAST(b.player_id AS BIGINT) = a.pid
) TO '{o_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        ddb = duckdb_runtime if duckdb_runtime is not None else DuckDbRuntimeConfig()
        apply_duckdb_runtime_pragmas(con, ddb)
        execute_sql_with_progress(con, sql, desc="[Step 2b] DuckDB segment bet from base")
    finally:
        con.close()
    nrows = int(pq.ParquetFile(out).metadata.num_rows) if pq.ParquetFile(out).metadata else 0
    logger.info(
        "[Step 2b] segmented bet parquet from base rows=%d -> %s",
        nrows,
        out.resolve(),
    )
    return out


def bet_base_clean_cache_manifest_path(base_cleaned_parquet: Path) -> Path:
    """Sidecar manifest for intermediate base cleaned bet parquet."""
    p = Path(base_cleaned_parquet).resolve()
    return p.parent / f"{p.stem}.cache.json"


def build_bet_base_clean_cache_record(
    source_bet_parquets: Sequence[Path],
    *,
    preprocess_registry_yaml: Path,
    dedup_hash_buckets: int | None = None,
    cleaned_session_parquet: Path | None = None,
    partition_inventory_fingerprint_sha256_hex: str | None = None,
) -> dict[str, Any]:
    """Fingerprint intermediate base bet (sources + cleaned session dependency; no ADT)."""

    spaths = sorted({str(Path(x).resolve()) for x in source_bet_parquets})
    paths = [Path(s) for s in spaths]
    if not paths:
        raise ValueError("source_bet_parquets must not be empty")
    stats: list[dict[str, Any]] = []
    for pth in paths:
        p = pth.resolve()
        if not p.is_file():
            raise FileNotFoundError(p)
        st = p.stat()
        meta = pq.ParquetFile(p).metadata
        nrows = int(meta.num_rows) if meta is not None else -1
        stats.append(
            {
                "path": str(p),
                "mtime_ns": int(st.st_mtime_ns),
                "size_bytes": int(st.st_size),
                "num_rows": nrows,
            }
        )
    cap_sec, applied = _bet_cap_applied_rules(Path(preprocess_registry_yaml).resolve())
    _buckets = (
        int(dedup_hash_buckets)
        if dedup_hash_buckets is not None
        else BetPreprocessConfig().dedup_hash_buckets
    )
    if _buckets < 1:
        raise ValueError(f"dedup_hash_buckets must be >= 1, got {_buckets}")
    rec: dict[str, Any] = {
        "manifest_kind": "bet_base_clean_only",
        "manifest_version": _BET_CLEAN_CACHE_MANIFEST_VERSION,
        "bet_l0_preprocess_py_sha256": _bet_l0_preprocess_py_sha256(),
        "bet_dedup_hash_buckets": _buckets,
        "bet_ingest_cap_sec": cap_sec,
        "applied_registry_fix_rules": applied,
        "preprocess_registry": _registry_stat_dict(preprocess_registry_yaml),
        "source_bets": stats,
    }
    dep = _fingerprint_cleaned_session_dependency(cleaned_session_parquet)
    if dep is not None:
        rec["cleaned_session_dependency"] = dep
    if partition_inventory_fingerprint_sha256_hex is not None:
        rec["partition_inventory_fingerprint_sha256_hex"] = str(
            partition_inventory_fingerprint_sha256_hex,
        ).strip()
    return rec


def bet_base_clean_cache_is_hit(
    source_bet_parquets: Sequence[Path],
    base_cleaned_parquet: Path,
    *,
    preprocess_registry_yaml: Path | None = None,
    dedup_hash_buckets: int | None = None,
    cleaned_session_parquet: Path | None = None,
    partition_inventory_fingerprint_sha256_hex: str | None = None,
) -> bool:
    """Return True if base cleaned bet exists and manifest matches."""

    bp = Path(base_cleaned_parquet).resolve()
    man = bet_base_clean_cache_manifest_path(bp)
    if not bp.is_file() or not man.is_file():
        return False
    try:
        prev = json.loads(man.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    try:
        reg = _resolve_preprocess_registry(preprocess_registry_yaml)
        cur = build_bet_base_clean_cache_record(
            source_bet_parquets,
            preprocess_registry_yaml=reg,
            dedup_hash_buckets=dedup_hash_buckets,
            cleaned_session_parquet=cleaned_session_parquet,
            partition_inventory_fingerprint_sha256_hex=partition_inventory_fingerprint_sha256_hex,
        )
    except (FileNotFoundError, OSError, ImportError, ValueError):
        return False
    return prev == cur


def write_bet_base_clean_cache_manifest(
    source_bet_parquets: Sequence[Path],
    base_cleaned_parquet: Path,
    *,
    preprocess_registry_yaml: Path | None = None,
    dedup_hash_buckets: int | None = None,
    cleaned_session_parquet: Path | None = None,
    partition_inventory_fingerprint_sha256_hex: str | None = None,
) -> Path:
    """Write manifest next to intermediate base cleaned parquet."""

    reg = _resolve_preprocess_registry(preprocess_registry_yaml)
    rec = build_bet_base_clean_cache_record(
        source_bet_parquets,
        preprocess_registry_yaml=reg,
        dedup_hash_buckets=dedup_hash_buckets,
        cleaned_session_parquet=cleaned_session_parquet,
        partition_inventory_fingerprint_sha256_hex=partition_inventory_fingerprint_sha256_hex,
    )
    mp = bet_base_clean_cache_manifest_path(Path(base_cleaned_parquet))
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("[Step 2b] wrote bet base clean cache manifest: %s", mp.resolve())
    return mp


def merge_bet_source_paths(
    primary: Path,
    extra_partition_sources: tuple[Path, ...] | None,
) -> tuple[Path, ...]:
    """Return sorted union of resolved bet parquet paths (primary + extras, de-duplicated)."""

    uniq: dict[str, Path] = {}
    p0 = Path(primary).resolve()
    uniq[str(p0)] = p0
    if extra_partition_sources:
        for raw in extra_partition_sources:
            p = Path(raw).resolve()
            uniq[str(p)] = p
    return tuple(sorted(uniq.values(), key=lambda x: str(x)))


def _cleaned_parquet_row_stat(path: Path) -> dict[str, Any]:
    """mtime/size/path/num_rows fingerprint for one Parquet artifact."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(p)
    st = p.stat()
    meta = pq.ParquetFile(p).metadata
    nrows = int(meta.num_rows) if meta is not None else -1
    return {
        "path": str(p),
        "mtime_ns": int(st.st_mtime_ns),
        "size_bytes": int(st.st_size),
        "num_rows": nrows,
    }


def _registry_stat_dict(path: Path) -> dict[str, Any]:
    """Small stat block for JSON cache fingerprints."""
    p = Path(path).resolve()
    st = p.stat()
    return {"path": str(p), "mtime_ns": int(st.st_mtime_ns), "size_bytes": int(st.st_size)}


def _bet_l0_preprocess_py_sha256() -> str:
    """SHA-256 of this module (bet pipeline only)."""
    path = Path(__file__).resolve()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fingerprint_cleaned_session_dependency(
    cleaned_session_parquet: Path | None,
) -> dict[str, Any] | None:
    """Stats for cleaned session artifact; ``None`` skips (bet-only / tests)."""
    if cleaned_session_parquet is None:
        return None
    p = Path(cleaned_session_parquet).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"cleaned session parquet expected for bet cache: {p}")
    st = p.stat()
    meta = pq.ParquetFile(p).metadata
    nrows = int(meta.num_rows) if meta is not None else -1
    return {
        "path": str(p),
        "mtime_ns": int(st.st_mtime_ns),
        "size_bytes": int(st.st_size),
        "num_rows": nrows,
    }


def build_bet_clean_cache_record(
    source_bet_parquet: Path,
    *,
    preprocess_registry_yaml: Path,
    dedup_hash_buckets: int | None = None,
    cleaned_session_parquet: Path | None = None,
    adt_filter_quantile: float | None = None,
    patron_profile_csv: Path | None = None,
    canonical_mapping_parquet: Path | None = None,
    adt_allowed_players_parquet: Path | None = None,
    extra_source_bet_parquets: tuple[Path, ...] | None = None,
    bet_base_cleaned_parquet: Path | None = None,
    partition_inventory_fingerprint_sha256_hex: str | None = None,
) -> dict[str, Any]:
    """Fingerprint for cleaned bet parquet cache (registry + optional cleaned session binding).

    When ``bet_base_cleaned_parquet`` is set, this targets the **segmented** parquet: fingerprint binds the
    all-players base artifact plus allowlist-derived ``adt_segment`` (distinct ``player_id`` content hash).

    When ``adt_filter_quantile`` is set without ``bet_base_cleaned_parquet``, the ADT fragment matches the
    single-stage preprocess path.

    ``patron_profile_csv`` / ``canonical_mapping_parquet`` are accepted for API compatibility but **do not**
    affect the fingerprint.
    """
    merged_sources = merge_bet_source_paths(source_bet_parquet, extra_source_bet_parquets)
    sources_stats = [_cleaned_parquet_row_stat(sp) for sp in merged_sources]
    cap_sec, applied = _bet_cap_applied_rules(Path(preprocess_registry_yaml).resolve())
    _buckets = (
        int(dedup_hash_buckets)
        if dedup_hash_buckets is not None
        else BetPreprocessConfig().dedup_hash_buckets
    )
    if _buckets < 1:
        raise ValueError(f"dedup_hash_buckets must be >= 1, got {_buckets}")
    rec: dict[str, Any] = {
        "manifest_version": _BET_CLEAN_CACHE_MANIFEST_VERSION,
        "bet_l0_preprocess_py_sha256": _bet_l0_preprocess_py_sha256(),
        "bet_dedup_hash_buckets": _buckets,
        "bet_ingest_cap_sec": cap_sec,
        "applied_registry_fix_rules": applied,
        "preprocess_registry": _registry_stat_dict(preprocess_registry_yaml),
    }
    if partition_inventory_fingerprint_sha256_hex is not None:
        rec["partition_inventory_fingerprint_sha256_hex"] = str(
            partition_inventory_fingerprint_sha256_hex,
        ).strip()
    dep = _fingerprint_cleaned_session_dependency(cleaned_session_parquet)
    if dep is not None:
        rec["cleaned_session_dependency"] = dep

    seg = _adt_segment_cache_block(
        quantile=adt_filter_quantile,
        adt_allowed_players_parquet=adt_allowed_players_parquet,
    )

    base_p = Path(bet_base_cleaned_parquet).resolve() if bet_base_cleaned_parquet is not None else None
    if base_p is not None:
        if seg is None:
            raise ValueError(
                "bet_base_cleaned_parquet requires ADT segmentation inputs "
                "(adt_filter_quantile + adt_allowed_players_parquet) for fingerprint."
            )
        rec.update(
            {
                "manifest_kind": "bet_clean_segment_projection_v1",
                "source_bets": sources_stats,
                "bet_base_cleaned": _cleaned_parquet_row_stat(base_p),
                "adt_segment": seg,
            }
        )
        return rec

    if len(merged_sources) > 1:
        rec.update({"manifest_kind": "bet_clean_direct_merge_v1", "source_bets": sources_stats})
    else:
        rec.update({"manifest_kind": "bet_clean_direct_v1", "source_bet": sources_stats[0]})
    if seg is not None:
        rec["adt_segment"] = seg
    return rec


def default_cleaned_bet_parquet_path() -> Path:
    """``trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_bet.parquet``."""
    return Path(__file__).resolve().parents[1] / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet.parquet"


def bet_clean_cache_manifest_path(cleaned_parquet: Path) -> Path:
    """Sidecar JSON next to cleaned bet parquet (``*.cache.json``)."""
    return Path(cleaned_parquet).parent / f"{Path(cleaned_parquet).stem}.cache.json"


def bet_clean_cache_is_hit(
    source_bet_parquet: Path,
    cleaned_parquet: Path,
    *,
    preprocess_registry_yaml: Path | None = None,
    dedup_hash_buckets: int | None = None,
    cleaned_session_parquet: Path | None = None,
    adt_filter_quantile: float | None = None,
    patron_profile_csv: Path | None = None,
    canonical_mapping_parquet: Path | None = None,
    adt_allowed_players_parquet: Path | None = None,
    extra_source_bet_parquets: tuple[Path, ...] | None = None,
    bet_base_cleaned_parquet: Path | None = None,
    partition_inventory_fingerprint_sha256_hex: str | None = None,
) -> bool:
    cleaned = Path(cleaned_parquet)
    reg = _resolve_preprocess_registry(preprocess_registry_yaml)
    man = bet_clean_cache_manifest_path(cleaned)
    if not cleaned.is_file() or not man.is_file():
        return False
    try:
        prev = json.loads(man.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    try:
        cur = build_bet_clean_cache_record(
            source_bet_parquet,
            preprocess_registry_yaml=reg,
            dedup_hash_buckets=dedup_hash_buckets,
            cleaned_session_parquet=cleaned_session_parquet,
            adt_filter_quantile=adt_filter_quantile,
            patron_profile_csv=patron_profile_csv,
            canonical_mapping_parquet=canonical_mapping_parquet,
            adt_allowed_players_parquet=adt_allowed_players_parquet,
            extra_source_bet_parquets=extra_source_bet_parquets,
            bet_base_cleaned_parquet=bet_base_cleaned_parquet,
            partition_inventory_fingerprint_sha256_hex=partition_inventory_fingerprint_sha256_hex,
        )
    except (FileNotFoundError, OSError, ImportError, ValueError):
        return False
    return prev == cur


def write_bet_clean_cache_manifest(
    source_bet_parquet: Path,
    cleaned_parquet: Path,
    *,
    preprocess_registry_yaml: Path | None = None,
    dedup_hash_buckets: int | None = None,
    cleaned_session_parquet: Path | None = None,
    adt_filter_quantile: float | None = None,
    patron_profile_csv: Path | None = None,
    canonical_mapping_parquet: Path | None = None,
    adt_allowed_players_parquet: Path | None = None,
    extra_source_bet_parquets: tuple[Path, ...] | None = None,
    bet_base_cleaned_parquet: Path | None = None,
    partition_inventory_fingerprint_sha256_hex: str | None = None,
) -> Path:
    reg = _resolve_preprocess_registry(preprocess_registry_yaml)
    rec = build_bet_clean_cache_record(
        source_bet_parquet,
        preprocess_registry_yaml=reg,
        dedup_hash_buckets=dedup_hash_buckets,
        cleaned_session_parquet=cleaned_session_parquet,
        adt_filter_quantile=adt_filter_quantile,
        patron_profile_csv=patron_profile_csv,
        canonical_mapping_parquet=canonical_mapping_parquet,
        adt_allowed_players_parquet=adt_allowed_players_parquet,
        extra_source_bet_parquets=extra_source_bet_parquets,
        bet_base_cleaned_parquet=bet_base_cleaned_parquet,
        partition_inventory_fingerprint_sha256_hex=partition_inventory_fingerprint_sha256_hex,
    )
    mp = bet_clean_cache_manifest_path(Path(cleaned_parquet))
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("[Step 2b] wrote bet clean cache manifest: %s", mp.resolve())
    return mp
