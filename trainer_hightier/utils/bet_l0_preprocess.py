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
from typing import Any

import duckdb
import pyarrow.parquet as pq
import yaml
from trainer.training.data_sources import _BET_INGEST_READ_COLS_ORDERED
from trainer_hightier.config import BetPreprocessConfig, DuckDbRuntimeConfig
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas, execute_sql_with_progress

logger = logging.getLogger("trainer_hightier")

_BET_CLEAN_CACHE_MANIFEST_VERSION = 5


def _duckdb_quote_ident(name: str) -> str:
    """Return a double-quoted DuckDB identifier (escape embedded quotes)."""
    return '"' + str(name).replace('"', '""') + '"'


def _adt_segment_cache_block(
    *,
    quantile: float | None,
    patron_profile_csv: Path | None,
    canonical_mapping_parquet: Path | None,
    adt_allowed_players_parquet: Path | None,
) -> dict[str, Any] | None:
    """Fingerprint fragment for ADT-based bet segmentation (optional)."""
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
    block: dict[str, Any] = {
        "quantile": qf,
        "adt_allowed_players_parquet": _registry_stat_dict(ap_f),
    }
    if patron_profile_csv is not None:
        pf = Path(patron_profile_csv).resolve()
        if pf.is_file():
            block["profile_csv"] = _registry_stat_dict(pf)
    if canonical_mapping_parquet is not None:
        mf = Path(canonical_mapping_parquet).resolve()
        if mf.is_file():
            block["canonical_mapping_parquet"] = _registry_stat_dict(mf)
    return block


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


def _duckdb_bet_clean_pipeline_select_sql(
    *,
    src_posix: str,
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
    SELECT {l0_projection} FROM read_parquet('{src_posix}')
    WHERE {l0_where}{bucket_filter}
  ) AS raw
  INNER JOIN allowed AS ap ON TRY_CAST(raw.player_id AS BIGINT) = ap.player_id
)"""
    else:
        with_lf = f"""WITH lf AS (
  SELECT {l0_projection} FROM read_parquet('{src_posix}')
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
    bet_parquet: Path,
    output_path: Path,
    *,
    registry_yaml: Path,
    duckdb_cfg: DuckDbRuntimeConfig,
    cfg: BetPreprocessConfig,
) -> tuple[int, tuple[tuple[str, str], ...]]:
    """Run DuckDB ``COPY`` pipeline; multi-pass hash buckets then merge when *dedup_hash_buckets* > 1."""

    src = Path(bet_parquet).resolve()
    out = Path(output_path).resolve()
    n = int(cfg.dedup_hash_buckets)
    if n < 1:
        raise ValueError(f"dedup_hash_buckets must be >= 1, got {n}")
    schema_names = frozenset(pq.read_schema(src).names)
    read_ordered = _bet_preprocess_read_columns_ordered(schema_names)
    cap_sec, _applied = _bet_cap_applied_rules(registry_yaml)
    tags = bulk_bet_episode_calendar_tags(registry_yaml)
    src_esc = _path_posix(src).replace("'", "''")
    out_esc = _path_posix(out).replace("'", "''")
    adt_allowed_esc = _resolve_adt_allowed_players_posix(cfg)
    if adt_allowed_esc is not None:
        logger.info("[Step 2b] bet preprocess ADT segment: early join to allowlist Parquet")

    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_cfg)
        if n == 1:
            inner = _duckdb_bet_clean_pipeline_select_sql(
                src_posix=src_esc,
                read_cols_ordered=read_ordered,
                cap_sec=cap_sec,
                tags=tags,
                dedup_bucket_id=None,
                dedup_buckets=1,
                adt_allowed_players_posix=adt_allowed_esc,
            )
            sql = f"COPY ({inner}) TO '{out_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
            execute_sql_with_progress(con, sql, desc="[Step 2b] DuckDB bet COPY")
        else:
            with tempfile.TemporaryDirectory(prefix="hightier_bet_bkt_", dir=out.parent) as tdir:
                parts_dir = Path(tdir)
                for b in range(n):
                    inner = _duckdb_bet_clean_pipeline_select_sql(
                        src_posix=src_esc,
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
                    execute_sql_with_progress(
                        con,
                        bsql,
                        desc=f"[Step 2b] DuckDB bet COPY bucket {b + 1}/{n}",
                    )
                glob_pat = str(parts_dir / "part_*.parquet").replace("\\", "/").replace("'", "''")
                merge_sql = (
                    f"COPY (SELECT * FROM read_parquet('{glob_pat}')) "
                    f"TO '{out_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
                )
                execute_sql_with_progress(con, merge_sql, desc="[Step 2b] DuckDB bet merge buckets")
    finally:
        con.close()
    return cap_sec, tags


def preprocess_bets_from_parquet_streaming(
    bet_parquet: Path,
    output_path: Path,
    *,
    cfg: BetPreprocessConfig | None = None,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> Path:
    """Clean full L0 ``gmwds_t_bet`` Parquet (DQ + synthetic observed cap + episode tags + dedupe)."""
    src = Path(bet_parquet).resolve()
    if not src.is_file():
        raise FileNotFoundError(src)
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
        src, out, registry_yaml=reg, duckdb_cfg=_ddb, cfg=_cfg
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
) -> dict[str, Any]:
    """Fingerprint for cleaned bet parquet cache (registry + optional cleaned session binding)."""
    src = Path(source_bet_parquet).resolve()
    if not src.is_file():
        raise FileNotFoundError(src)
    st = src.stat()
    meta = pq.ParquetFile(src).metadata
    nrows = int(meta.num_rows) if meta is not None else -1
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
        "source_bet": {
            "path": str(src),
            "mtime_ns": int(st.st_mtime_ns),
            "size_bytes": int(st.st_size),
            "num_rows": nrows,
        },
    }
    dep = _fingerprint_cleaned_session_dependency(cleaned_session_parquet)
    if dep is not None:
        rec["cleaned_session_dependency"] = dep
    seg = _adt_segment_cache_block(
        quantile=adt_filter_quantile,
        patron_profile_csv=patron_profile_csv,
        canonical_mapping_parquet=canonical_mapping_parquet,
        adt_allowed_players_parquet=adt_allowed_players_parquet,
    )
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
    )
    mp = bet_clean_cache_manifest_path(Path(cleaned_parquet))
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("[Step 2b] wrote bet clean cache manifest: %s", mp.resolve())
    return mp
