"""Bet L0 preprocess: ``t_bet`` → cleaned Parquet (DuckDB).

Bet clean cache keys bind **raw t_bet shards**, ingest registry, optional **ADT allowlist**
(distinct ``player_id`` set hash), and partition inventory fingerprint — not cleaned session
artifact mtime/rows. For hit checks, legacy manifests that still contain
``cleaned_session_dependency`` are compared with that field ignored (backward compatible).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Sequence

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import yaml
from trainer_hightier.bet_contract import BET_INGEST_READ_COLS_ORDERED
from trainer_hightier.config import (
    BET_AVAIL_DELAY_MIN,
    DuckDbRuntimeConfig,
    PREPROCESS_DEDUP_BUCKET_ESCALATION_CEILING,
    BetPreprocessConfig,
    PLACEHOLDER_PLAYER_ID,
    SCORER_POLL_INTERVAL_SECONDS,
)
from trainer_hightier.preprocess_bet_fix_registry import (
    bundled_preprocess_registry_yaml_path,
    load_preprocess_bet_ingestion_fix_registry,
    resolve_bet_ingest_fix004_cap_binding,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas, execute_sql_with_progress_oom_retry

logger = logging.getLogger("trainer_hightier")

_BET_CLEAN_CACHE_MANIFEST_VERSION = 9


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
    """Bundled ``trainer_hightier/contracts/preprocess_l0_data_contract_registry.yaml``."""
    return bundled_preprocess_registry_yaml_path()


def _placeholder_player_id_i64() -> int:
    """Trainer sentinel ``player_id`` (`PLACEHOLDER_PLAYER_ID`); used in bet DQ."""

    return int(PLACEHOLDER_PLAYER_ID)


def _resolve_preprocess_registry(registry_yaml: Path | None) -> Path:
    """Resolve ingest registry YAML path; default bundled contracts YAML."""
    path = registry_yaml if registry_yaml is not None else default_preprocess_registry_yaml_path()
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Ingest registry YAML not found: {p}")
    return p


def _bet_cap_applied_rules(registry_yaml: Path) -> tuple[int, list[str]]:
    """Load ``tables.t_bet`` FIX-004 cap from preprocess registry YAML (local parser)."""

    doc = load_preprocess_bet_ingestion_fix_registry(Path(registry_yaml))
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

    missing = tuple(c for c in BET_INGEST_READ_COLS_ORDERED if c not in schema_names)
    if missing:
        raise ValueError(
            "gmwds_t_bet Parquet missing columns required for ingest/preprocess "
            f"(trainer_hightier.bet_contract.BET_INGEST_READ_COLS_ORDERED): {list(missing)}"
        )
    return BET_INGEST_READ_COLS_ORDERED


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
    """Return ``(bet_avail_delay_min, poll_interval_sec)`` aligned with Feast / serving semantics."""

    return int(BET_AVAIL_DELAY_MIN), int(SCORER_POLL_INTERVAL_SECONDS)


def cleaned_bet_dataset_glob_posix(dataset_root: Path) -> str:
    """POSIX glob matching all Parquet files under Hive-style cleaned-bet partitions."""

    root = Path(dataset_root).resolve()
    return str(root / "**" / "*.parquet").replace("\\", "/")


def resolved_cleaned_bet_read_parquet_sql(path: Path) -> str:
    """DuckDB ``read_parquet`` clause for cleaned bet artifacts (legacy file or partitioned dir).

    Hive-style dirs (``gaming_month=*/gaming_day=*/``) disable automatic hive partitioning so DATE
    ``gaming_day`` does not collide with partition folder keys.
    """
    p = Path(path).resolve()
    if p.is_file():
        esc = _path_posix(p).replace("'", "''")
        return f"read_parquet('{esc}')"
    if p.is_dir():
        glo = cleaned_bet_dataset_glob_posix(p).replace("'", "''")
        return f"read_parquet('{glo}', hive_partitioning=false)"
    raise FileNotFoundError(f"cleaned bet artifact not found: {p}")


def first_parquet_under_for_schema(dataset_root_or_file: Path) -> Path:
    """Pick one readable Parquet (for ``pyarrow`` schema probing) under a rooted dataset."""

    tgt = Path(dataset_root_or_file).resolve()
    if tgt.is_file():
        return tgt
    if tgt.is_dir():
        files = sorted(tgt.rglob("*.parquet"), key=lambda x: str(x))
        if not files:
            raise FileNotFoundError(f"No Parquet shards under partitioned cleaned bet dataset: {tgt}")
        return files[0]
    raise FileNotFoundError(tgt)


def partitioned_cleaned_bet_total_rows(dataset_root_or_file: Path) -> int:
    """Approximate ``SUM(num_rows)`` over Parquet footers (no full table scan)."""

    p = Path(dataset_root_or_file).resolve()
    if p.is_file():
        meta = pq.ParquetFile(p).metadata
        return int(meta.num_rows) if meta is not None else 0
    if not p.is_dir():
        return 0
    total = 0
    for f in sorted(p.rglob("*.parquet")):
        meta = pq.ParquetFile(f).metadata
        if meta is None:
            continue
        total += int(meta.num_rows)
    return total


def cleaned_bet_dataset_has_any_parquet(path: Path) -> bool:
    """Return ``True`` if cleaned bet artifact exists as a parquet file or a non-empty parquet tree."""

    p = Path(path).resolve()
    if p.is_file():
        return p.suffix.lower() == ".parquet"
    if p.is_dir():
        return any(p.rglob("*.parquet"))
    return False


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


def _clear_partitioned_dataset_dir(dataset_root: Path) -> None:
    """Remove an existing partitioned parquet tree before rewriting."""

    r = Path(dataset_root).resolve()
    if r.exists() and not r.is_dir():
        raise ValueError(f"partitioned cleaned bet dataset path must be absent or directory: {r}")
    if r.is_dir():
        shutil.rmtree(r)
    r.parent.mkdir(parents=True, exist_ok=True)
    r.mkdir(parents=True, exist_ok=False)


def _wrapped_bet_select_for_partitioned_copy(pipeline_sql: str) -> str:
    """Hive-style COPY wrapper: emits ``gaming_month`` / ``gaming_day_key`` PARTITION_BY dirs."""

    inner = pipeline_sql.strip()
    return (
        "SELECT\n"
        "  p.*,\n"
        "  strftime(TRY_CAST(p.gaming_day AS DATE), '%Y%m') AS gaming_month,\n"
        "  strftime(TRY_CAST(p.gaming_day AS DATE), '%Y-%m-%d') AS gaming_day_key\n"
        f"FROM ({inner}) AS p\n"
    )


def _consolidate_staged_bucket_partition_dirs(*, staged_root: Path, final_dataset_root: Path, n_buckets: int) -> None:
    """Move per-bucket partition leaves into ``final_dataset_root`` with deterministic ``bucket_*.parquet`` names."""

    for b in range(int(n_buckets)):
        st = staged_root / f"b{b:04d}"
        if not st.is_dir():
            raise FileNotFoundError(f"missing staged bucket partition root: {st}")
        bucket_label = f"bucket_{b:04d}.parquet"
        for pq_src in sorted(st.rglob("*.parquet")):
            rel_parent = pq_src.relative_to(st).parent  # hive dirs only
            dest_dir = final_dataset_root / rel_parent
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / bucket_label
            if dest.is_file():
                dest.unlink()
            shutil.move(str(pq_src), str(dest))


def _partitioned_parquet_manifest_block(dataset_root: Path) -> dict[str, Any]:
    """Fingerprint shard under a partitioned dataset root."""

    root = Path(dataset_root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"partitioned artifact expected directory: {root}")
    shards: list[dict[str, Any]] = []
    total_rows = 0
    root_s = root.as_posix()
    for f in sorted(root.rglob("*.parquet")):
        p = Path(f).resolve()
        rel = str(p.relative_to(root)).replace("\\", "/")
        st = p.stat()
        meta = pq.ParquetFile(p).metadata
        nrows = int(meta.num_rows) if meta is not None else -1
        total_rows += max(nrows, 0)
        shards.append(
            {
                "rel_path": rel,
                "mtime_ns": int(st.st_mtime_ns),
                "size_bytes": int(st.st_size),
                "num_rows": int(nrows),
            }
        )
    if not shards:
        raise ValueError(f"partitioned dataset has no parquet files: {root}")
    lines = ("\n".join(f"{x['rel_path']}:{x['mtime_ns']}:{x['size_bytes']}:{x['num_rows']}" for x in shards)).encode(
        "utf-8",
    )
    digest = hashlib.sha256(lines).hexdigest()
    return {
        "kind": "gaming_day_partitioned_parquet_dataset_v1",
        "dataset_root": root_s.replace("\\", "/"),
        "shard_count": int(len(shards)),
        "total_num_rows": int(total_rows),
        "shard_list_sha256_hex": digest,
        "shard_stats": shards,
    }


def _bet_artifact_manifest_block(path: Path) -> dict[str, Any]:
    """Stat block supporting legacy single parquet or partitioned dataset."""

    ap = Path(path).resolve()
    if ap.is_file():
        blk = _cleaned_parquet_row_stat(ap)
        blk["manifest_storage_kind"] = "single_parquet_v1"
        return blk
    if ap.is_dir():
        return _partitioned_parquet_manifest_block(ap)
    raise FileNotFoundError(ap)


def _enforce_no_null_gaming_day_partitioned(dataset_root: Path, *, duckdb_cfg: DuckDbRuntimeConfig) -> None:
    root = Path(dataset_root).resolve()
    if not any(root.rglob("*.parquet")):
        return
    glo = cleaned_bet_dataset_glob_posix(root).replace("'", "''")
    sql = f"""
SELECT COUNT(*) AS n_null
FROM read_parquet('{glo}', hive_partitioning=false) AS _
WHERE TRY_CAST(_.gaming_day AS DATE) IS NULL
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_cfg)
        n = int(con.execute(sql).fetchone()[0])
    finally:
        con.close()
    if n > 0:
        raise ValueError(
            f"gaming_day null gate failed after partitioned bet preprocess: rows_with_null_date={n} root={dataset_root}"
        )


def cleaned_bet_artifact_fingerprint_block(path: Path) -> dict[str, Any]:
    """JSON-serializable fingerprint for a cleaned bet file or partitioned dataset root."""

    return _bet_artifact_manifest_block(path)


def _preprocess_bets_duckdb_single_copy(
    source_parquets: list[Path],
    output_path: Path,
    *,
    registry_yaml: Path,
    duckdb_cfg: DuckDbRuntimeConfig,
    cfg: BetPreprocessConfig,
) -> tuple[int, tuple[tuple[str, str], ...]]:
    """Run DuckDB ``COPY`` into Hive-style ``gaming_month × gaming_day`` partition directories.

    With multiple dedup buckets, writes staged partitioned trees then consolidates deterministic
    ``bucket_*.parquet`` shards per leaf.
    """

    if not source_parquets:
        raise ValueError("source_parquets must not be empty")
    ds_root = Path(output_path).resolve()
    n = int(cfg.dedup_hash_buckets)
    if n < 1:
        raise ValueError(f"dedup_hash_buckets must be >= 1, got {n}")
    schema_names = frozenset(pq.read_schema(Path(source_parquets[0])).names)
    read_ordered = _bet_preprocess_read_columns_ordered(schema_names)
    cap_sec, _applied = _bet_cap_applied_rules(registry_yaml)
    tags = bulk_bet_episode_calendar_tags(registry_yaml)
    src_clause = _duckdb_read_parquet_sources_sql(source_parquets)
    out_esc = _path_posix(ds_root).replace("'", "''")
    adt_allowed_esc = _resolve_adt_allowed_players_posix(cfg)
    if adt_allowed_esc is not None:
        logger.info("[Step 2b] bet preprocess ADT segment: early join to allowlist Parquet")

    partition_opts = (
        "FORMAT PARQUET, COMPRESSION SNAPPY, PARTITION_BY (gaming_month, gaming_day_key), OVERWRITE_OR_IGNORE TRUE"
    )
    _clear_partitioned_dataset_dir(ds_root)

    if n == 1:
        pipeline = _duckdb_bet_clean_pipeline_select_sql(
            src_read_parquet_clause=src_clause,
            read_cols_ordered=read_ordered,
            cap_sec=cap_sec,
            tags=tags,
            dedup_bucket_id=None,
            dedup_buckets=1,
            adt_allowed_players_posix=adt_allowed_esc,
        )
        wrapped = _wrapped_bet_select_for_partitioned_copy(pipeline)
        sql = f"COPY ({wrapped}) TO '{out_esc}' ({partition_opts})"
        execute_sql_with_progress_oom_retry(
            duckdb_cfg,
            sql,
            desc="[Step 2b] DuckDB bet partitioned COPY",
            join_timeout_s=7200.0,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="hightier_bet_bkt_", dir=ds_root.parent) as tdir:
            staged_parent = Path(tdir)
            for b in range(n):
                staged_bucket = staged_parent / f"b{b:04d}"
                staged_bucket.mkdir(parents=True, exist_ok=False)
                st_esc = _path_posix(staged_bucket).replace("'", "''")
                pipeline = _duckdb_bet_clean_pipeline_select_sql(
                    src_read_parquet_clause=src_clause,
                    read_cols_ordered=read_ordered,
                    cap_sec=cap_sec,
                    tags=tags,
                    dedup_bucket_id=b,
                    dedup_buckets=n,
                    adt_allowed_players_posix=adt_allowed_esc,
                )
                wrapped = _wrapped_bet_select_for_partitioned_copy(pipeline)
                bsql = f"COPY ({wrapped}) TO '{st_esc}' ({partition_opts})"
                execute_sql_with_progress_oom_retry(
                    duckdb_cfg,
                    bsql,
                    desc=f"[Step 2b] DuckDB bet partitioned COPY bucket {b + 1}/{n}",
                    join_timeout_s=7200.0,
                )
            _consolidate_staged_bucket_partition_dirs(
                staged_root=staged_parent,
                final_dataset_root=ds_root,
                n_buckets=n,
            )
    _enforce_no_null_gaming_day_partitioned(ds_root, duckdb_cfg=duckdb_cfg)
    return cap_sec, tags


def preprocess_bets_from_parquet_streaming(
    bet_parquet: Path,
    output_path: Path,
    *,
    cfg: BetPreprocessConfig | None = None,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    extra_partition_sources: tuple[Path, ...] | None = None,
) -> tuple[Path, int]:
    """Clean full L0 ``gmwds_t_bet`` Parquet (DQ + synthetic observed cap + episode tags + dedupe).

    Returns
    -------
    tuple[Path, int]
        ``(resolved output path, effective dedup_hash_buckets used)``.
    """
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
    if out.exists() and out.is_dir():
        shutil.rmtree(out)
    elif out.exists() and out.is_file():
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
    ceiling_eff = max(int(PREPROCESS_DEDUP_BUCKET_ESCALATION_CEILING), int(_nb))
    b = int(_nb)
    while True:
        cfg_try = replace(_cfg, dedup_hash_buckets=b)
        logger.info(
            "[Step 2b] bet preprocess: DuckDB COPY registry=%s dedup_hash_buckets=%d -> %s",
            reg,
            b,
            out,
        )
        try:
            cap_sec, tags = _preprocess_bets_duckdb_single_copy(
                sources_list,
                out,
                registry_yaml=reg,
                duckdb_cfg=_ddb,
                cfg=cfg_try,
            )
        except duckdb.OutOfMemoryException as exc:
            if b >= ceiling_eff:
                logger.warning(
                    "[Step 2b] DuckDB OOM at dedup_hash_buckets=%d (ceiling=%d); aborting escalate.",
                    b,
                    ceiling_eff,
                )
                raise exc
            nb = min(ceiling_eff, b * 2)
            if nb <= b:
                raise exc
            logger.warning(
                "[Step 2b] DuckDB OOM at dedup_hash_buckets=%s: %s — doubling buckets -> %s (ceiling=%s)",
                b,
                exc,
                nb,
                ceiling_eff,
            )
            b = nb
            continue

        nrows = partitioned_cleaned_bet_total_rows(out)
        logger.info(
            "[Step 2b] bet preprocess done: rows=%d cap_sec=%s bulk_episode_days=%s "
            "dedup_hash_buckets_effective=%d written %s",
            nrows,
            cap_sec,
            len(tags),
            b,
            out,
        )
        return out, int(b)


def default_cleaned_bet_base_parquet_path() -> Path:
    """Intermediate all-players cleaned bet partition root (prior to optional ADT projection)."""

    return Path(__file__).resolve().parents[1] / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet_base"


def segment_cleaned_bet_from_base_parquet(
    base_cleaned_parquet: Path,
    allowlist_parquet: Path,
    output_parquet: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> Path:
    """Project partitioned all-player bets onto ADT allowlist rows; rewritten as Hive partitioned output."""

    base = Path(base_cleaned_parquet).resolve()
    allow = Path(allowlist_parquet).resolve()
    out = Path(output_parquet).resolve()
    legacy_base_ok = base.is_file() or cleaned_bet_dataset_has_any_parquet(base)
    if not legacy_base_ok:
        raise FileNotFoundError(base)
    if not allow.is_file():
        raise FileNotFoundError(allow)
    b_from = resolved_cleaned_bet_read_parquet_sql(base)
    a_esc = _path_posix(allow).replace("'", "''")
    o_esc = _path_posix(out).replace("'", "''")
    partition_opts = (
        "FORMAT PARQUET, COMPRESSION SNAPPY, PARTITION_BY (gaming_month, gaming_day_key), OVERWRITE_OR_IGNORE TRUE"
    )
    sql = f"""
COPY (
  SELECT
    s.*,
    strftime(TRY_CAST(s.gaming_day AS DATE), '%Y%m') AS gaming_month,
    strftime(TRY_CAST(s.gaming_day AS DATE), '%Y-%m-%d') AS gaming_day_key
  FROM (
    SELECT DISTINCT b.*
    FROM {b_from} AS b
    INNER JOIN (
      SELECT TRY_CAST(player_id AS BIGINT) AS pid
      FROM read_parquet('{a_esc}')
      WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
    ) AS a ON TRY_CAST(b.player_id AS BIGINT) = a.pid
  ) AS s
) TO '{o_esc}' ({partition_opts})
""".strip()
    ddb = duckdb_runtime if duckdb_runtime is not None else DuckDbRuntimeConfig()
    _clear_partitioned_dataset_dir(out)
    execute_sql_with_progress_oom_retry(
        ddb,
        sql,
        desc="[Step 2b] DuckDB segment bet from base",
        join_timeout_s=7200.0,
    )
    _enforce_no_null_gaming_day_partitioned(out, duckdb_cfg=ddb)
    nrows = partitioned_cleaned_bet_total_rows(out)
    logger.info(
        "[Step 2b] segmented bet dataset from base rows=%d -> %s",
        nrows,
        out.resolve(),
    )
    return out


def bet_base_clean_cache_manifest_path(base_cleaned_parquet: Path) -> Path:
    """Sidecar manifest for intermediate base cleaned bet artefact."""

    p = Path(base_cleaned_parquet).resolve()
    if p.is_file():
        return p.parent / f"{p.stem}.cache.json"
    return p.parent / f"{p.name}.cache.json"


def bet_base_manifest_dedup_hash_buckets(base_cleaned_parquet: Path) -> int | None:
    """Return ``bet_dedup_hash_buckets`` from existing base-clean cache manifest, or ``None``."""
    bp = Path(base_cleaned_parquet).resolve()
    man = bet_base_clean_cache_manifest_path(bp)
    if not man.is_file():
        return None
    try:
        prev = json.loads(man.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    raw = prev.get("bet_dedup_hash_buckets")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_bet_cache_manifest_for_compare(manifest: dict[str, Any]) -> dict[str, Any]:
    """Drop legacy-only fields so old sidecars still match current fingerprints.

    - Removes ``cleaned_session_dependency`` (no longer part of the cache key).
    - Canonicalizes ``manifest_version`` for v8→v9 migration so on-disk v8 manifests
      compare equal to newly written v9 when all semantic fields match.
    """

    out = dict(manifest)
    out.pop("cleaned_session_dependency", None)
    mv = out.get("manifest_version")
    try:
        mv_int = int(mv) if mv is not None else None
    except (TypeError, ValueError):
        mv_int = None
    if mv_int in (8, 9):
        out["manifest_version"] = _BET_CLEAN_CACHE_MANIFEST_VERSION
    return out


def _bet_cache_manifests_equal_for_compare(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Equality for hit checks after normalizing legacy session binding and manifest version."""

    return _normalize_bet_cache_manifest_for_compare(a) == _normalize_bet_cache_manifest_for_compare(
        b,
    )


def _bet_manifest_matches_with_bucket_alias(
    prev: dict[str, Any],
    *,
    nominal_buckets: int,
    build_cur: Callable[[int], dict[str, Any]],
) -> bool:
    """Cache hit if *prev* equals current fingerprint for nominal or persisted bucket count."""

    nb = int(nominal_buckets)
    if _bet_cache_manifests_equal_for_compare(prev, build_cur(nb)):
        return True
    raw = prev.get("bet_dedup_hash_buckets")
    try:
        stored = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return False
    if stored is None or stored == nb:
        return False
    return _bet_cache_manifests_equal_for_compare(prev, build_cur(stored))


def build_bet_base_clean_cache_record(
    source_bet_parquets: Sequence[Path],
    *,
    preprocess_registry_yaml: Path,
    dedup_hash_buckets: int | None = None,
    cleaned_session_parquet: Path | None = None,
    partition_inventory_fingerprint_sha256_hex: str | None = None,
) -> dict[str, Any]:
    """Fingerprint intermediate base bet (raw sources + registry; no ADT).

    ``cleaned_session_parquet`` is accepted for API compatibility but does not affect the fingerprint.
    """

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
    base_ready = bp.is_file() or (bp.is_dir() and cleaned_bet_dataset_has_any_parquet(bp))
    if not base_ready or not man.is_file():
        return False
    try:
        prev = json.loads(man.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    try:
        reg = _resolve_preprocess_registry(preprocess_registry_yaml)

        nominal = (
            int(dedup_hash_buckets)
            if dedup_hash_buckets is not None
            else BetPreprocessConfig().dedup_hash_buckets
        )

        def _cur(nb: int) -> dict[str, Any]:
            return build_bet_base_clean_cache_record(
                source_bet_parquets,
                preprocess_registry_yaml=reg,
                dedup_hash_buckets=nb,
                cleaned_session_parquet=None,
                partition_inventory_fingerprint_sha256_hex=partition_inventory_fingerprint_sha256_hex,
            )

        return _bet_manifest_matches_with_bucket_alias(
            prev,
            nominal_buckets=nominal,
            build_cur=_cur,
        )
    except (FileNotFoundError, OSError, ImportError, ValueError):
        return False


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
    """Fingerprint for cleaned bet parquet cache (raw t_bet + registry + optional ADT allowlist).

    When ``bet_base_cleaned_parquet`` is set, this targets the **segmented** parquet: fingerprint binds the
    all-players base artifact plus allowlist-derived ``adt_segment`` (distinct ``player_id`` content hash).

    When ``adt_filter_quantile`` is set without ``bet_base_cleaned_parquet``, the ADT fragment matches the
    single-stage preprocess path.

    ``patron_profile_csv`` / ``canonical_mapping_parquet`` / ``cleaned_session_parquet`` are accepted for API
    compatibility but **do not** affect the fingerprint.
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
                "bet_base_cleaned": _bet_artifact_manifest_block(base_p),
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
    """Segmented cleaned bet Hive-partition dataset root."""

    return Path(__file__).resolve().parents[1] / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet"


def bet_clean_cache_manifest_path(cleaned_parquet: Path) -> Path:
    """Sidecar JSON for cleaned bet cache (single Parquet legacy or partitioned dataset root)."""

    p = Path(cleaned_parquet).resolve()
    if p.is_file():
        return p.parent / f"{p.stem}.cache.json"
    return p.parent / f"{p.name}.cache.json"


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
    cleaned = Path(cleaned_parquet).resolve()
    reg = _resolve_preprocess_registry(preprocess_registry_yaml)
    man = bet_clean_cache_manifest_path(cleaned)
    ready = cleaned.is_file() or (cleaned.is_dir() and cleaned_bet_dataset_has_any_parquet(cleaned))
    if not ready or not man.is_file():
        return False
    try:
        prev = json.loads(man.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    nominal = (
        int(dedup_hash_buckets)
        if dedup_hash_buckets is not None
        else BetPreprocessConfig().dedup_hash_buckets
    )

    def _cur(nb: int) -> dict[str, Any]:
        return build_bet_clean_cache_record(
            source_bet_parquet,
            preprocess_registry_yaml=reg,
            dedup_hash_buckets=nb,
            cleaned_session_parquet=None,
            adt_filter_quantile=adt_filter_quantile,
            patron_profile_csv=patron_profile_csv,
            canonical_mapping_parquet=canonical_mapping_parquet,
            adt_allowed_players_parquet=adt_allowed_players_parquet,
            extra_source_bet_parquets=extra_source_bet_parquets,
            bet_base_cleaned_parquet=bet_base_cleaned_parquet,
            partition_inventory_fingerprint_sha256_hex=partition_inventory_fingerprint_sha256_hex,
        )

    try:
        return _bet_manifest_matches_with_bucket_alias(
            prev,
            nominal_buckets=nominal,
            build_cur=_cur,
        )
    except (FileNotFoundError, OSError, ImportError, ValueError):
        return False


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


def patch_bet_preprocess_cache_manifest_file(
    manifest_path: Path,
    *,
    preprocess_registry_yaml: Path,
    partition_inventory_fingerprint_sha256_hex: str | None = None,
) -> dict[str, Any]:
    """Rewrite fingerprint-only fields in an on-disk bet cache sidecar (no Parquet recompute).

    Updates module hash, registry stat, FIX-004 cap binding, and optional partition inventory
    fingerprint. Preserves ``bet_base_cleaned`` / ``shard_stats`` blocks for fast one-off repair.
    """

    mp = Path(manifest_path).resolve()
    if not mp.is_file():
        raise FileNotFoundError(f"bet cache manifest missing: {mp}")
    reg = _resolve_preprocess_registry(preprocess_registry_yaml)
    cap_sec, applied = _bet_cap_applied_rules(reg)
    data = dict(json.loads(mp.read_text(encoding="utf-8")))
    data["bet_l0_preprocess_py_sha256"] = _bet_l0_preprocess_py_sha256()
    data["preprocess_registry"] = _registry_stat_dict(reg)
    data["bet_ingest_cap_sec"] = int(cap_sec)
    data["applied_registry_fix_rules"] = list(applied)
    if partition_inventory_fingerprint_sha256_hex is not None:
        data["partition_inventory_fingerprint_sha256_hex"] = str(
            partition_inventory_fingerprint_sha256_hex,
        ).strip()
    mp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("[bet cache] patched manifest metadata: %s", mp)
    return data


def refresh_bet_preprocess_cache_manifests(
    *,
    partition_snapshot_dir: Path | None = None,
    base_cleaned_parquet: Path | None = None,
    segment_cleaned_parquet: Path | None = None,
    preprocess_registry_yaml: Path | None = None,
    adt_filter_quantile: float | None = None,
    adt_allowed_players_parquet: Path | None = None,
    dedup_hash_buckets: int | None = None,
) -> dict[str, bool]:
    """One-shot repair: patch bet cache sidecars so ``*_cache_is_hit`` passes without DuckDB.

    Reads ``bet_dedup_hash_buckets`` from the existing base sidecar when *dedup_hash_buckets*
    is omitted. Verifies hits after patching; raises if segment/base still miss.
    """

    from trainer_hightier.utils.partition_inventory import (
        infer_snapshot_id,
        inventory_to_manifest_dict,
        scan_partition_snapshot_dir,
    )

    snap = Path(partition_snapshot_dir or Path(__file__).resolve().parents[2] / "data" / "partitions").resolve()
    reg = _resolve_preprocess_registry(
        preprocess_registry_yaml or default_preprocess_registry_yaml_path(),
    )
    base_p = Path(base_cleaned_parquet or default_cleaned_bet_base_parquet_path()).resolve()
    seg_p = Path(segment_cleaned_parquet or default_cleaned_bet_parquet_path()).resolve()
    bet_rows, _ = scan_partition_snapshot_dir(snap)
    if not bet_rows:
        raise FileNotFoundError(f"no t_bet partition shards under {snap}")
    inv_fp = inventory_to_manifest_dict(
        infer_snapshot_id(snap),
        snapshot_dir=snap,
        bet_stats=bet_rows,
        session_stats=[],
    )["fingerprint_sha256_hex"]
    inv_fp_s = str(inv_fp).strip()
    merged_bets = tuple(sorted({r.path.resolve() for r in bet_rows}, key=str))
    base_man = bet_base_clean_cache_manifest_path(base_p)
    seg_man = bet_clean_cache_manifest_path(seg_p)
    if not base_man.is_file():
        raise FileNotFoundError(f"base bet cache sidecar missing: {base_man}")
    stored_buckets = bet_base_manifest_dedup_hash_buckets(base_p)
    buckets = int(dedup_hash_buckets if dedup_hash_buckets is not None else (stored_buckets or BetPreprocessConfig().dedup_hash_buckets))
    patch_bet_preprocess_cache_manifest_file(
        base_man,
        preprocess_registry_yaml=reg,
        partition_inventory_fingerprint_sha256_hex=inv_fp_s,
    )
    if seg_man.is_file():
        patch_bet_preprocess_cache_manifest_file(
            seg_man,
            preprocess_registry_yaml=reg,
            partition_inventory_fingerprint_sha256_hex=inv_fp_s,
        )
    q = adt_filter_quantile
    allow_p = Path(adt_allowed_players_parquet).resolve() if adt_allowed_players_parquet is not None else None
    if seg_man.is_file() and q is None:
        prev_seg = json.loads(seg_man.read_text(encoding="utf-8"))
        adt_blk = prev_seg.get("adt_segment")
        if isinstance(adt_blk, dict) and adt_blk.get("quantile") is not None:
            q = float(adt_blk["quantile"])
    if allow_p is None and q is not None:
        from trainer_hightier.utils.patron_session_metrics import default_adt_allowed_players_parquet_path

        allow_p = default_adt_allowed_players_parquet_path(float(q)).resolve()
    base_hit = bet_base_clean_cache_is_hit(
        merged_bets,
        base_p,
        preprocess_registry_yaml=reg,
        dedup_hash_buckets=buckets,
        partition_inventory_fingerprint_sha256_hex=inv_fp_s,
    )
    seg_hit = False
    if seg_man.is_file() and allow_p is not None and q is not None:
        seg_hit = bet_clean_cache_is_hit(
            merged_bets[0],
            seg_p,
            preprocess_registry_yaml=reg,
            dedup_hash_buckets=buckets,
            adt_filter_quantile=float(q),
            adt_allowed_players_parquet=allow_p,
            extra_source_bet_parquets=merged_bets[1:] or None,
            bet_base_cleaned_parquet=base_p,
            partition_inventory_fingerprint_sha256_hex=inv_fp_s,
        )
    out = {"bet_base_clean_cache_hit": bool(base_hit), "bet_segment_clean_cache_hit": bool(seg_hit)}
    if not base_hit:
        raise RuntimeError(
            "bet base cache still misses after manifest patch; "
            "source shard stats or dedup_hash_buckets may have drifted — inspect "
            f"{base_man}"
        )
    if seg_man.is_file() and not seg_hit:
        raise RuntimeError(
            "bet segment cache still misses after manifest patch; "
            "adt allowlist or bet_base_cleaned shard fingerprint may have drifted — inspect "
            f"{seg_man}"
        )
    logger.info("[bet cache] refresh OK: %s", out)
    return out


def _cli_refresh_bet_cache_manifests(argv: list[str] | None = None) -> int:
    """CLI: ``python -m trainer_hightier.utils.bet_l0_preprocess --refresh-cache-manifests``."""

    import argparse

    pr = argparse.ArgumentParser(description="Patch bet preprocess cache sidecars (no DuckDB recompute).")
    pr.add_argument("--partition-snapshot-dir", type=Path, default=None)
    pr.add_argument("--registry-yaml", type=Path, default=None)
    pr.add_argument("--adt-quantile", type=float, default=None)
    pr.add_argument("--adt-allowlist-parquet", type=Path, default=None)
    pr.add_argument("--dedup-hash-buckets", type=int, default=None)
    args = pr.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    refresh_bet_preprocess_cache_manifests(
        partition_snapshot_dir=args.partition_snapshot_dir,
        preprocess_registry_yaml=args.registry_yaml,
        adt_filter_quantile=args.adt_quantile,
        adt_allowed_players_parquet=args.adt_allowlist_parquet,
        dedup_hash_buckets=args.dedup_hash_buckets,
    )
    return 0


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--refresh-cache-manifests":
        raise SystemExit(_cli_refresh_bet_cache_manifests(sys.argv[2:]))
    raise SystemExit(
        "Usage: python -m trainer_hightier.utils.bet_l0_preprocess --refresh-cache-manifests"
    )
