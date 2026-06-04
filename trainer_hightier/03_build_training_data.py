"""Offline training-set export for high-tier trainer (step 3).

Pipeline order:

1. :mod:`trainer_hightier.01_data_ingest` — raw Parquet ingress / schema QC.
2. :mod:`trainer_hightier.02_preprocess` — cleaned ``t_session`` + ``t_bet``
   (and optional walkaway labels via :mod:`trainer_hightier.trainer`).
3. **This module** — Feast ``get_historical_features`` on cleaned bet entities +
   left-join ``walkaway_labels.parquet`` → ``artifacts/training_data/``.

Invoke with ``python -m trainer_hightier.03_build_training_data`` (numeric
prefix matches steps 1–2; use :func:`importlib.import_module` if importing from code).

Steps (see :func:`build_training_data`):

1. Optional: materialize derived slow patron 180d monthly Parquet (short-term ``bet__*`` / ``fe__*`` come from Step 3.5 bounded hot pool).
2. Write entity Parquet (``bet_id``, ``event_timestamp``) from cleaned bet.
3. ``get_historical_features`` + ``persist`` → staging feature Parquet (Ibis/DuckDB).
4. DuckDB left-join labels on ``bet_id`` + ``gaming_day_event`` from cleaned bet → ``artifacts/training_data/training_set.parquet``.

Prerequisites: ``feast_repo/data/registry.db`` must exist before Feast retrieval.
By default Step 3 will run ``feast apply`` in ``feast_repo`` when registry is missing
(pass ``--disable-auto-feast-apply`` to enforce a manual apply beforehand). Offline
store paths in ``definitions.py`` must resolve to existing materialized Parquets.

Memory / scale: Feast’s Ibis+DuckDB offline path loads the **full entity**
``DataFrame`` into memory (``bet_id`` + ``event_timestamp`` only — ~16 bytes/row
order-of-magnitude plus pandas overhead). Join and feature SQL run in Ibis/DuckDB;
ensure ``feature_store.yaml`` ``staging_location`` has disk space. Align
``duckdb`` / ``ibis-framework`` versions with repo ``requirements.txt`` (mismatches
can break ``ibis.read_parquet`` at retrieval time).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    SHORT_TERM_TRIAL_BET_COLUMNS,
    configs_from_run_profile,
    get_run_profile,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas
from trainer_hightier.utils.bet_l0_preprocess import (
    cleaned_bet_artifact_fingerprint_block,
    cleaned_bet_dataset_has_any_parquet,
    resolved_cleaned_bet_read_parquet_sql,
)
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path
from trainer_hightier.utils.slow_patron_180d_monthly import (
    default_slow_patron_180d_monthly_parquet_path,
    materialize_slow_patron_180d_monthly,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeastRegistryEnsureResult:
    """Outcome of :func:`ensure_feast_registry_ready`."""

    feast_repo: Path
    registry_path: Path
    feast_registry_ready: bool
    feast_auto_apply_requested: bool
    feast_auto_apply_attempted: bool
    feast_auto_apply_succeeded: bool | None
    feast_apply_wall_sec: float | None
    feast_schema_drift_issues: tuple[str, ...] = ()


def feast_registry_ensure_result_to_metrics(result: FeastRegistryEnsureResult) -> dict[str, Any]:
    """Serialize ensure result for ``run_report.json`` / experiment reports."""

    return {
        "feast_repo": str(result.feast_repo.resolve()),
        "feast_registry_path": str(result.registry_path.resolve()),
        "feast_registry_ready": bool(result.feast_registry_ready),
        "feast_auto_apply_requested": bool(result.feast_auto_apply_requested),
        "feast_auto_apply_attempted": bool(result.feast_auto_apply_attempted),
        "feast_auto_apply_succeeded": result.feast_auto_apply_succeeded,
        "feast_apply_wall_sec": result.feast_apply_wall_sec,
        "feast_schema_drift_issues": list(result.feast_schema_drift_issues),
    }


def ensure_feast_registry_ready(feast_repo: Path | str, *, auto_apply: bool) -> FeastRegistryEnsureResult:
    """Ensure Feast registry exists and matches production scorer v2 schema (conditional apply).

    Delegates to :func:`trainer_hightier.serving.feast_online_adapter.ensure_feast_schema_ready`.
    """
    from trainer_hightier.serving.feast_online_adapter import ensure_feast_schema_ready

    schema_res = ensure_feast_schema_ready(feast_repo, auto_apply=auto_apply)
    return FeastRegistryEnsureResult(
        feast_repo=schema_res.feast_repo,
        registry_path=schema_res.registry_path,
        feast_registry_ready=schema_res.feast_registry_ready,
        feast_auto_apply_requested=schema_res.feast_auto_apply_requested,
        feast_auto_apply_attempted=schema_res.feast_auto_apply_attempted,
        feast_auto_apply_succeeded=schema_res.feast_auto_apply_succeeded,
        feast_apply_wall_sec=schema_res.feast_apply_wall_sec,
        feast_schema_drift_issues=schema_res.feast_schema_drift_issues,
    )

_DEFAULT_RUN_PROFILE = "default"

TRAINER_HIGHTIER_ROOT = Path(__file__).resolve().parent
REPO_ROOT = TRAINER_HIGHTIER_ROOT.parent
DEFAULT_FEAST_REPO = TRAINER_HIGHTIER_ROOT / "feast_repo"
DEFAULT_CLEANED_BET = TRAINER_HIGHTIER_ROOT / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet"
DEFAULT_LABELS = TRAINER_HIGHTIER_ROOT / "artifacts" / "labels" / "walkaway_labels.parquet"
DEFAULT_TRAINING_DIR = TRAINER_HIGHTIER_ROOT / "artifacts" / "training_data"
DEFAULT_OUTPUT = DEFAULT_TRAINING_DIR / "training_set.parquet"
DEFAULT_FEATURE_SERVICE = "walkaway_bet_trial_v1"

FEAST_GROUP_CACHE_SCHEMA_VERSION = 1

# Aggregates eligible for calendar-month decomposition + disk cache keys.
SERVICES_WITH_DECOMPOSED_MONTH_CACHE = frozenset({"walkaway_bet_trial_v1", "walkaway_bet_v1"})

_RE_GAMING_DAY_KEY = re.compile(r"gaming_day_key=(\d{4}-\d{2}-\d{2})")

_TRIAL_MERGE_COLS = SHORT_TERM_TRIAL_BET_COLUMNS
_SLOW_MERGE_COLS = (
    "patron__theo_win_sum__w180d_m1snap",
    "patron__gaming_days_cnt__w180d_m1snap",
    "patron__adt__w180d_m1snap",
)


def _path_posix(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def _build_training_data_module_sha256_hex() -> str:
    """SHA-256 hex of this module (cache manifest invalidation on logic change)."""
    path = Path(__file__).resolve()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _month_yyyymm(month_start: date) -> str:
    return f"{month_start.year:04d}{month_start.month:02d}"


def _cleaned_artifact_fingerprint_token(block: dict[str, Any]) -> str:
    """Stable token labeling the cleaned bet artifact root (partition hive digest or hashed single blob)."""

    hex_h = block.get("shard_list_sha256_hex")
    if isinstance(hex_h, str) and hex_h.strip():
        return hex_h.strip()
    blob = json.dumps(block, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _gaming_date_from_shard_rel(rel_path: str) -> date | None:
    m = _RE_GAMING_DAY_KEY.search(str(rel_path).replace("\\", "/"))
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _index_shard_stats(block: dict[str, Any]) -> dict[str, dict[str, Any]]:
    shards = block.get("shard_stats") or []
    if not isinstance(shards, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for raw in shards:
        if isinstance(raw, dict):
            rp = raw.get("rel_path")
            if isinstance(rp, str) and rp:
                out[rp] = raw
    return out


def _dirty_shard_calendar_dates(prev_block: dict[str, Any] | None, cur_block: dict[str, Any]) -> frozenset[date] | None:
    """Returns None → invalidate all calendar months (no prior baseline or non-partition ambiguity).

    Empty frozenset → stable cleaned fingerprint vs previous success state (reuse all group-month caches).

    Otherwise → calendar gaming dates attached to materially changed parquet shard paths.
    """

    if prev_block is None:
        return None
    if _cleaned_artifact_fingerprint_token(prev_block) == _cleaned_artifact_fingerprint_token(cur_block):
        return frozenset()
    if cur_block.get("manifest_storage_kind") == "single_parquet_v1" or ("shard_stats" not in cur_block):
        return None
    ci = _index_shard_stats(cur_block)
    pi = _index_shard_stats(prev_block)
    dates: set[date] = set()
    union = set(pi) | set(ci)
    for rel in sorted(union):
        if pi.get(rel) != ci.get(rel):
            d = _gaming_date_from_shard_rel(rel)
            if d is None:
                return None
            dates.add(d)
    return frozenset(dates)


def _month_touches_expanded_dirty_window(
    month_start: date, *, dirty_lo: date, dirty_hi: date, lookback_calendar_days: int
) -> bool:
    lb = max(0, int(lookback_calendar_days))
    exp_lo = dirty_lo - timedelta(days=lb)
    exp_hi = dirty_hi + timedelta(days=lb)
    me_excl = _add_one_month_calendar(month_start)
    return month_start <= exp_hi and me_excl > exp_lo


def _affected_month_indices_by_group(
    months: list[date],
    dirty_dates: frozenset[date] | None,
    groups_plan: tuple[tuple[str, str, int], ...],
) -> dict[str, set[int]]:
    if dirty_dates is None:
        full = set(range(len(months)))
        return {gid: set(full) for gid, _, _ in groups_plan}
    if len(dirty_dates) == 0:
        return {gid: set() for gid, _, _ in groups_plan}
    lo = min(dirty_dates)
    hi = max(dirty_dates)
    out_m: dict[str, set[int]] = {}
    for gid, _, lb in groups_plan:
        touched: set[int] = set()
        for mi, ms in enumerate(months):
            if _month_touches_expanded_dirty_window(ms, dirty_lo=lo, dirty_hi=hi, lookback_calendar_days=lb):
                touched.add(mi)
        out_m[gid] = touched
    return out_m


def _feast_group_plan(aggregate_feature_service: str) -> tuple[tuple[str, str, int], ...]:
    """(group_id, feast_subgroup_service, lookback_calendar_days)."""

    s = aggregate_feature_service.strip()
    if s == "walkaway_bet_trial_v1":
        return (
            ("cleaned", "walkaway_bet_v1", 31),
            ("slow_snap", "walkaway_canonical_slow_snap_v1", 180),
        )
    if s == "walkaway_bet_v1":
        return (("cleaned", "walkaway_bet_v1", 31),)
    raise ValueError(f"unsupported decomposed feast aggregate {aggregate_feature_service!r}")


def _parquet_quick_stat(path: Path) -> dict[str, Any]:
    """Lightweight fingerprint for Feast persist outputs / derived parquet artifacts."""

    p = Path(path).resolve()
    st = p.stat()
    meta = pq.ParquetFile(p).metadata
    nrows = int(meta.num_rows) if meta is not None else -1
    return {
        "path": _path_posix(p),
        "mtime_ns": int(st.st_mtime_ns),
        "size_bytes": int(st.st_size),
        "num_rows": int(nrows),
    }


def _load_json_optional(path: Path) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Corrupt JSON state at %s; ignoring.", p.resolve())
        return None


def _save_json_atomic(path: Path, obj: dict[str, Any]) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(obj, indent=2, sort_keys=True, default=str).encode("utf-8")
    with tempfile.NamedTemporaryFile(
        prefix=dest.name + ".",
        suffix=".tmp.json",
        delete=False,
        dir=str(dest.parent),
    ) as tmp:
        tmp.write(blob)
        tmp_path = Path(tmp.name)
    try:
        tmp_path.replace(dest)
    finally:
        if tmp_path.is_file():
            try:
                tmp_path.unlink(missing_ok=True)  # type: ignore[call-arg]
            except TypeError:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


def _group_cache_manifest_path(parquet_cache_file: Path) -> Path:
    return parquet_cache_file.parent / (parquet_cache_file.name + ".manifest.json")


def _derived_dependency_stat(repo_root: Path, group_id: str) -> dict[str, Any] | None:
    if group_id == "slow_snap":
        p = default_slow_patron_180d_monthly_parquet_path(repo_root=repo_root)
        if not p.is_file():
            raise FileNotFoundError(f"group {group_id} requires derived Parquet at {p}")
        return _parquet_quick_stat(p)
    return None


def _manifest_compatible(
    manifest: dict[str, Any],
    *,
    aggregate_name: str,
    group_id: str,
    feast_subgroup: str,
    month_yyyymm: str,
    cleaned_token: str,
    derived_stat: dict[str, Any] | None,
    code_fp: str,
) -> bool:
    if int(manifest.get("schema_version", -1)) != FEAST_GROUP_CACHE_SCHEMA_VERSION:
        return False
    if str(manifest.get("aggregate_feature_service", "")).strip() != aggregate_name:
        return False
    if str(manifest.get("group_id", "")).strip() != group_id:
        return False
    if str(manifest.get("feast_subgroup_service", "")).strip() != feast_subgroup:
        return False
    if str(manifest.get("month_yyyymm", "")).strip() != month_yyyymm:
        return False
    if str(manifest.get("cleaned_fingerprint_token", "")).strip() != cleaned_token:
        return False
    if str(manifest.get("code_fingerprint", "")).strip() != code_fp:
        return False
    have = manifest.get("derived_source_stat")
    if derived_stat is None:
        return have is None
    return isinstance(have, dict) and have.get("mtime_ns") == derived_stat.get("mtime_ns") and have.get(
        "size_bytes"
    ) == derived_stat.get("size_bytes") and have.get("num_rows") == derived_stat.get("num_rows")


def _duckdb_join_decomposed_month_features(
    *,
    cleaned_p: Path,
    trial_p: Path | None,
    slow_p: Path | None,
    merged_out: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> None:
    """Align ``walkaway_bet_trial_v1`` feature columns from separate Feast persists."""

    c_esc = _path_posix(cleaned_p).replace("'", "''")
    me = _path_posix(merged_out).replace("'", "''")
    if trial_p is None and slow_p is None:
        merged_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(cleaned_p).resolve(), Path(merged_out).resolve())
        return
    trial_cols = ", ".join(f"t.{c}" for c in _TRIAL_MERGE_COLS)
    slow_cols = ", ".join(f"s.{c}" for c in _SLOW_MERGE_COLS)
    from_clause = f"read_parquet('{c_esc}') AS c"
    join_sql = ""
    if trial_p is not None:
        t_esc = _path_posix(trial_p).replace("'", "''")
        join_sql += f"""
        LEFT JOIN read_parquet('{t_esc}') AS t
          ON TRY_CAST(c.bet_id AS DOUBLE) = TRY_CAST(t.bet_id AS DOUBLE)
         AND CAST(c.event_timestamp AS TIMESTAMPTZ) = CAST(t.event_timestamp AS TIMESTAMPTZ)
        """.strip()
    if slow_p is not None:
        s_esc = _path_posix(slow_p).replace("'", "''")
        slow_schema = set(pq.read_schema(slow_p).names)
        if "canonical_id" in slow_schema and "bet_id" not in slow_schema:
            join_sql += f"""
        LEFT JOIN read_parquet('{s_esc}') AS s
          ON TRIM(CAST(c.canonical_id AS VARCHAR)) = TRIM(CAST(s.canonical_id AS VARCHAR))
            """.strip()
        else:
            join_sql += f"""
        LEFT JOIN read_parquet('{s_esc}') AS s
          ON TRY_CAST(c.bet_id AS DOUBLE) = TRY_CAST(s.bet_id AS DOUBLE)
         AND CAST(c.event_timestamp AS TIMESTAMPTZ) = CAST(s.event_timestamp AS TIMESTAMPTZ)
            """.strip()
    select_extra = ""
    if trial_p is not None:
        select_extra += ", " + trial_cols
    if slow_p is not None:
        select_extra += ", " + slow_cols
    sql = f"""
COPY (
  SELECT c.*{select_extra}
  FROM {from_clause}
  {join_sql}
) TO '{me}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    Path(merged_out).parent.mkdir(parents=True, exist_ok=True)
    if Path(merged_out).is_file():
        Path(merged_out).unlink()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(sql)
    finally:
        con.close()


def _slow_parquet_grain(path: Path) -> str:
    """Return ``canonical`` or ``bet`` from materialized slow Parquet columns."""

    names = set(pq.read_schema(path).names)
    if "canonical_id" in names and "bet_id" not in names:
        return "canonical"
    if "bet_id" in names:
        return "bet"
    raise ValueError(
        f"slow parquet has unsupported schema at {path.resolve()}; "
        f"expected canonical_id+anchor or bet_id grain, got {sorted(names)}"
    )


def _attach_canonical_slow_snap_for_entities(
    *,
    entity_parquet: Path,
    cleaned_bet_parquet: Path,
    canonical_mapping_parquet: Path,
    slow_parquet: Path,
    output_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> None:
    """Project canonical slow features onto bet entities (train-serve parity path).

    Skips Feast ``walkaway_canonical_slow_snap_v1`` when slow artifact is
    ``canonical_active_month`` grain (no ``prediction_visible_ts_cf``).
    """

    e_esc = _path_posix(entity_parquet).replace("'", "''")
    bet_from = resolved_cleaned_bet_read_parquet_sql(cleaned_bet_parquet)
    map_esc = _path_posix(canonical_mapping_parquet).replace("'", "''")
    slow_esc = _path_posix(slow_parquet).replace("'", "''")
    out_esc = _path_posix(output_parquet).replace("'", "''")
    slow_cols = ", ".join(f's."{c}"' for c in _SLOW_MERGE_COLS)
    sql = f"""
COPY (
  SELECT
    e.bet_id,
    e.event_timestamp,
    {slow_cols}
  FROM read_parquet('{e_esc}') AS e
  INNER JOIN (
    SELECT
      TRY_CAST(bet_id AS DOUBLE) AS bet_id,
      TRY_CAST(player_id AS BIGINT) AS player_id
    FROM {bet_from} AS _cbd
    WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
      AND TRY_CAST(player_id AS BIGINT) IS NOT NULL
  ) AS b ON TRY_CAST(e.bet_id AS DOUBLE) = b.bet_id
  LEFT JOIN (
    SELECT DISTINCT
      TRY_CAST(player_id AS BIGINT) AS player_id,
      TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
    FROM read_parquet('{map_esc}')
    WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
      AND TRIM(CAST(canonical_id AS VARCHAR)) <> ''
  ) AS m ON b.player_id = m.player_id
  LEFT JOIN read_parquet('{slow_esc}') AS s
    ON TRIM(CAST(m.canonical_id AS VARCHAR)) = TRIM(CAST(s.canonical_id AS VARCHAR))
) TO '{out_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    if output_parquet.is_file():
        output_parquet.unlink()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(sql)
    finally:
        con.close()


def _write_group_cache_manifest(
    manifest_path: Path,
    *,
    aggregate_name: str,
    group_id: str,
    feast_subgroup: str,
    month_yyyymm: str,
    cleaned_token: str,
    derived_stat: dict[str, Any] | None,
    code_fp: str,
    out_stat: dict[str, Any],
) -> None:
    blob = {
        "schema_version": FEAST_GROUP_CACHE_SCHEMA_VERSION,
        "aggregate_feature_service": aggregate_name,
        "group_id": group_id,
        "feast_subgroup_service": feast_subgroup,
        "month_yyyymm": month_yyyymm,
        "cleaned_fingerprint_token": cleaned_token,
        "derived_source_stat": derived_stat,
        "code_fingerprint": code_fp,
        "output_parquet_stat": out_stat,
    }
    _save_json_atomic(manifest_path, blob)


@dataclass(frozen=True)
class BuildTrainingDataArgs:
    """Configuration for :func:`build_training_data`."""

    feast_repo: Path
    cleaned_bet_parquet: Path
    labels_parquet: Path
    output_parquet: Path
    feature_service_name: str
    materialize_derived_features: bool
    max_entity_rows: int | None
    duckdb_runtime: DuckDbRuntimeConfig
    feast_entity_batch_by_calendar_month: bool = False
    training_set_keep_last_n_versions: int = 10
    feast_retrieval_cache_enabled: bool = True
    auto_feast_apply: bool = True


def _validate_prereqs(
    *,
    feast_repo: Path,
    cleaned_bet: Path,
    labels_parquet: Path,
    materialize_derived: bool,
    feature_service_name: str,
) -> None:
    reg = feast_repo / "data" / "registry.db"
    if not reg.is_file():
        raise FileNotFoundError(
            f"Feast registry missing at {reg}; run `feast apply` from {feast_repo.resolve()}."
        )
    cb = Path(cleaned_bet).resolve()
    if not (cb.is_file() or cleaned_bet_dataset_has_any_parquet(cb)):
        raise FileNotFoundError(f"Cleaned bet artefact not found: {cb}")
    if not labels_parquet.is_file():
        raise FileNotFoundError(
            f"Labels Parquet not found: {labels_parquet} "
            f"(run trainer pipeline Step 2c or materialize_walkaway_labels_from_cleaned_bet)."
        )
    needs_derived = feature_service_name.strip() == DEFAULT_FEATURE_SERVICE
    if needs_derived and not materialize_derived:
        slow = default_slow_patron_180d_monthly_parquet_path(repo_root=REPO_ROOT)
        if not slow.is_file():
            raise FileNotFoundError(
                "Derived slow patron 180d Parquet missing. "
                f"Missing: {slow}. "
                "Pass --materialize-derived to build it, use --feature-service walkaway_bet_v1 "
                "for cleaned bet features only, or materialize manually."
            )


def _maybe_materialize_derived(cfg: BuildTrainingDataArgs) -> None:
    if not cfg.materialize_derived_features:
        return
    if cfg.feature_service_name.strip() != DEFAULT_FEATURE_SERVICE:
        logger.info(
            "Skip trial/slow materialization: feature service %r !== %r.",
            cfg.feature_service_name,
            DEFAULT_FEATURE_SERVICE,
        )
        return
    logger.info("Materializing slow patron 180d monthly Parquet (training derived; trial 1h skipped) …")
    materialize_slow_patron_180d_monthly(duckdb_runtime=cfg.duckdb_runtime)


def _add_one_month_calendar(d: date) -> date:
    """First day of following calendar month (``d`` is expected month-start date)."""
    y, m = d.year, d.month
    if m == 12:
        return date(y + 1, 1, 1)
    return date(y, m + 1, 1)


def _prediction_visible_month_starts(cleaned_bet: Path, *, duckdb_runtime: DuckDbRuntimeConfig) -> list[date]:
    """Distinct calendar months (UTC TIMESTAMP cast) covering ``prediction_visible_ts_cf``."""

    bet_from = resolved_cleaned_bet_read_parquet_sql(cleaned_bet)
    sql = f"""
SELECT CAST(DATE_TRUNC('month', CAST(prediction_visible_ts_cf AS TIMESTAMP)) AS DATE) AS m
FROM {bet_from} AS _cb
WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
  AND prediction_visible_ts_cf IS NOT NULL
GROUP BY 1 ORDER BY 1
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    out: list[date] = []
    for r in rows:
        if not r or r[0] is None:
            continue
        raw = r[0]
        if isinstance(raw, date):
            out.append(raw)
        else:
            out.append(datetime.fromisoformat(str(raw)).date())
    return out


def _write_entity_parquet(
    cleaned_bet: Path,
    entity_out: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
    max_rows: int | None,
    month_start: date | None = None,
    month_end_exclusive: date | None = None,
) -> int:
    bet_from = resolved_cleaned_bet_read_parquet_sql(cleaned_bet)
    ent_esc = _path_posix(entity_out).replace("'", "''")
    lim = f"LIMIT {int(max_rows)}" if max_rows is not None else ""
    filt = ""
    if month_start is not None and month_end_exclusive is not None:
        ms = month_start.isoformat()
        me = month_end_exclusive.isoformat()
        filt = (
            f"\n    AND CAST(prediction_visible_ts_cf AS TIMESTAMP) >= TIMESTAMP '{ms}' "
            f"AND CAST(prediction_visible_ts_cf AS TIMESTAMP) < TIMESTAMP '{me}'"
        )
    sql = f"""
COPY (
  SELECT
    TRY_CAST(bet_id AS DOUBLE) AS bet_id,
    CAST(prediction_visible_ts_cf AS TIMESTAMPTZ) AS event_timestamp
  FROM {bet_from} AS _cb
  WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
    AND prediction_visible_ts_cf IS NOT NULL
    {filt}
  {lim}
) TO '{ent_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    Path(entity_out).parent.mkdir(parents=True, exist_ok=True)
    if Path(entity_out).is_file():
        Path(entity_out).unlink()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(sql)
    finally:
        con.close()
    ent_path = Path(entity_out)
    if not ent_path.is_file():
        return 0
    meta = pq.ParquetFile(ent_path).metadata
    return int(meta.num_rows) if meta is not None else 0


def _duckdb_union_parquet_into(
    parquet_inputs: list[Path],
    target: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> None:
    """Concat multiple Parquets with identical schema into ``target`` (DuckDB ``read_parquet`` list)."""

    if not parquet_inputs:
        raise ValueError("duckdb_union: parquet_inputs must not be empty")
    targ = Path(target).resolve()
    targ.parent.mkdir(parents=True, exist_ok=True)
    if targ.is_file():
        targ.unlink()
    if len(parquet_inputs) == 1:
        shutil.copyfile(Path(parquet_inputs[0]).resolve(), targ)
        return
    esc = "[" + ",".join(f"'{_path_posix(p)}'" for p in parquet_inputs) + "]"
    o_esc = _path_posix(targ).replace("'", "''")
    sql = f"COPY (SELECT * FROM read_parquet({esc})) TO '{o_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(sql)
    finally:
        con.close()


def _prune_versioned_training_sets(versions_dir: Path, *, keep_last_n: int) -> None:
    """Keep newest ``keep_last_n`` ``training_set_*.parquet`` under *versions_dir*."""

    vd = Path(versions_dir).resolve()
    keep = int(keep_last_n)
    if keep < 1 or not vd.is_dir():
        return
    cand = sorted(
        vd.glob("training_set_*.parquet"),
        key=lambda p: p.stat().st_mtime_ns,
        reverse=True,
    )
    for stale in cand[keep:]:
        try:
            stale.unlink()
            logger.info("Pruned old training set version %s", stale.name)
        except OSError as exc:
            logger.warning("Could not prune %s: %s", stale, exc)


def _feast_features_to_parquet(
    *,
    feast_repo: Path,
    entity_parquet: Path,
    feature_service_name: str,
    features_parquet: Path,
) -> None:
    """Load entity rows as a DataFrame (Feast Ibis backend requires in-memory entity_df)."""

    import pandas as pd

    from feast import FeatureStore
    from feast.infra.offline_stores.file_source import SavedDatasetFileStorage

    entity_df = pd.read_parquet(entity_parquet)
    src = FeatureStore(repo_path=str(feast_repo.resolve()))
    try:
        svc = src.get_feature_service(feature_service_name)
    except Exception as exc:
        raise ValueError(
            f"Could not load feature service {feature_service_name!r} from registry. "
            f"Check `feast apply` and definitions.py."
        ) from exc

    job = src.get_historical_features(
        entity_df=entity_df,
        features=svc,
        full_feature_names=False,
    )
    features_parquet.parent.mkdir(parents=True, exist_ok=True)
    job.persist(
        SavedDatasetFileStorage(path=str(features_parquet.resolve())),
        allow_overwrite=True,
    )


def _training_manifest(
    *,
    output_parquet: Path,
    feast_repo: Path,
    feature_service: str,
    row_count: int | None,
    labels_parquet: Path,
    versioned_parquet: Path | None,
) -> dict[str, Any]:
    blob: dict[str, Any] = {
        "output_parquet": str(output_parquet.resolve()),
        "row_count": row_count,
        "feast_repo": str(feast_repo.resolve()),
        "feature_service": feature_service,
        "labels_parquet": str(labels_parquet.resolve()),
    }
    if versioned_parquet is not None:
        blob["versioned_output_parquet"] = str(Path(versioned_parquet).resolve())
    return blob


def _join_labels_to_features(
    *,
    features_parquet: Path,
    labels_parquet: Path,
    cleaned_bet_parquet: Path,
    output_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> int | None:
    """Merge Feast features with labels and split keys for downstream Step 4.

    Adds ``canonical_id`` from ``walkaway_labels`` and ``player_id`` / ``game_id`` /
    ``gaming_day_event`` from cleaned bet via ``bet_id``; no change to Feast retrieval.
    """

    f_esc = _path_posix(features_parquet).replace("'", "''")
    l_esc = _path_posix(labels_parquet).replace("'", "''")
    o_esc = _path_posix(output_parquet).replace("'", "''")
    bet_from = resolved_cleaned_bet_read_parquet_sql(Path(cleaned_bet_parquet).resolve())
    sql = f"""
COPY (
  SELECT
    h.*,
    y.label AS walkaway_label,
    y.censored AS walkaway_censored,
    y.canonical_id AS canonical_id,
    b.gaming_day_event AS gaming_day_event,
    b.player_id AS player_id,
    b.game_id AS game_id
  FROM read_parquet('{f_esc}') h
  LEFT JOIN (
    SELECT
      TRY_CAST(bet_id AS DOUBLE) AS bet_id,
      CAST(label AS SMALLINT) AS label,
      CAST(censored AS BOOLEAN) AS censored,
      TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
    FROM read_parquet('{l_esc}')
  ) y ON TRY_CAST(h.bet_id AS DOUBLE) = y.bet_id
  LEFT JOIN (
    SELECT
      TRY_CAST(bet_id AS DOUBLE) AS bet_id,
      MIN(CAST(gaming_day_event AS DATE)) AS gaming_day_event,
      ANY_VALUE(TRY_CAST(player_id AS BIGINT)) AS player_id,
      ANY_VALUE(TRY_CAST(game_id AS DOUBLE)) AS game_id
    FROM {bet_from} AS _cbd
    WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
    GROUP BY 1
  ) b ON TRY_CAST(h.bet_id AS DOUBLE) = b.bet_id
) TO '{o_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(sql)
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{o_esc}')").fetchone()
        return int(n[0]) if n else None
    finally:
        con.close()


def _persist_last_cleaned_fp_state(cache_root_dir: Path, block: dict[str, Any]) -> None:
    _save_json_atomic(cache_root_dir / "last_success_cleaned_fingerprint.json", {"cleaned_bet_fingerprint_block": block})


def build_training_data(cfg: BuildTrainingDataArgs) -> Path:
    """Produce versioned artifact + symlink-style copy at ``cfg.output_parquet``.

    Raises:
        FileNotFoundError: Missing inputs or derived Parquets when not materializing.
        ValueError: Feast feature service or join failure.
    """
    ensure_feast_registry_ready(cfg.feast_repo, auto_apply=cfg.auto_feast_apply)
    _validate_prereqs(
        feast_repo=cfg.feast_repo,
        cleaned_bet=cfg.cleaned_bet_parquet,
        labels_parquet=cfg.labels_parquet,
        materialize_derived=cfg.materialize_derived_features,
        feature_service_name=cfg.feature_service_name,
    )
    _maybe_materialize_derived(cfg)

    out_dir = cfg.output_parquet.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    versions_dir = out_dir / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    versioned_out = versions_dir / f"training_set_{stamp}.parquet"

    entity_pq = out_dir / "_entity_bet_ts.parquet"
    staging_feat = out_dir / "_staging_feast_features.parquet"
    batch_tmp = tempfile.mkdtemp(prefix="hightier_feast_mo_", dir=str(out_dir))
    month_batch = cfg.feast_entity_batch_by_calendar_month and cfg.max_entity_rows is None
    use_group_cache = (
        month_batch
        and cfg.feast_retrieval_cache_enabled
        and cfg.feature_service_name.strip() in SERVICES_WITH_DECOMPOSED_MONTH_CACHE
    )
    aggregate_name = cfg.feature_service_name.strip()
    if aggregate_name == DEFAULT_FEATURE_SERVICE and not use_group_cache:
        raise ValueError(
            f"feature_service {aggregate_name!r} requires month-batch decomposed Feast cache "
            "(feast_entity_batch_by_calendar_month=True and feast_retrieval_cache_enabled=True). "
            "Short-term bet__* features are supplied in Step 3.5 via bounded hot pool, not Feast trial_clock.",
        )
    n_rows: int | None = None
    code_fp = _build_training_data_module_sha256_hex()
    cleaned_fp_root = cleaned_bet_artifact_fingerprint_block(Path(cfg.cleaned_bet_parquet).resolve())
    cleaned_token = _cleaned_artifact_fingerprint_token(cleaned_fp_root)
    prev_state = _load_json_optional(out_dir / "cache" / "feast_month_group_v1" / "last_success_cleaned_fingerprint.json")
    prev_fp_block = prev_state.get("cleaned_bet_fingerprint_block") if isinstance(prev_state, dict) else None
    dirty_dates = (
        None
        if not use_group_cache
        else _dirty_shard_calendar_dates(prev_fp_block if isinstance(prev_fp_block, dict) else None, cleaned_fp_root)
    )

    cache_root_layout = Path(out_dir) / "cache" / "feast_month_group_v1" / cleaned_token / cfg.feature_service_name.strip()
    slow_derived_p = default_slow_patron_180d_monthly_parquet_path(repo_root=REPO_ROOT)
    slow_canonical_attach = (
        use_group_cache
        and slow_derived_p.is_file()
        and _slow_parquet_grain(slow_derived_p) == "canonical"
    )
    if slow_canonical_attach:
        logger.info(
            "Slow snap uses canonical_active_month attach (skip Feast %s).",
            "walkaway_canonical_slow_snap_v1",
        )

    try:
        if month_batch:
            months = _prediction_visible_month_starts(
                cfg.cleaned_bet_parquet,
                duckdb_runtime=cfg.duckdb_runtime,
            )
            if not months:
                raise ValueError("No prediction_visible_ts_cf months found for Feast batch retrieval.")
            batch_feats: list[Path] = []

            groups_plan = _feast_group_plan(cfg.feature_service_name.strip()) if use_group_cache else ()

            affected_by_gid: dict[str, set[int]] = (
                _affected_month_indices_by_group(months, dirty_dates, groups_plan)
                if use_group_cache and groups_plan
                else {}
            )

            for mi, ms in enumerate(months):
                me = _add_one_month_calendar(ms)
                ent_fp = Path(batch_tmp) / f"entity_{mi:04d}.parquet"
                nrow = _write_entity_parquet(
                    cfg.cleaned_bet_parquet,
                    ent_fp,
                    duckdb_runtime=cfg.duckdb_runtime,
                    max_rows=None,
                    month_start=ms,
                    month_end_exclusive=me,
                )
                if nrow <= 0:
                    continue
                f_fp = Path(batch_tmp) / f"feat_{mi:04d}.parquet"

                if use_group_cache and groups_plan:
                    yrmo = _month_yyyymm(ms)
                    paths_by_gid: dict[str, Path] = {}
                    derived_memo: dict[str, dict[str, Any] | None] = {}

                    for gid, feast_svc, _lb in groups_plan:
                        if gid not in derived_memo:
                            derived_memo[gid] = _derived_dependency_stat(REPO_ROOT, gid)
                        agg_dir = Path(cache_root_layout) / gid
                        agg_dir.mkdir(parents=True, exist_ok=True)
                        cache_p = agg_dir / f"{yrmo}.parquet"
                        mpath = _group_cache_manifest_path(cache_p)

                        reuse_ok = mi not in affected_by_gid.get(gid, set()) and cache_p.is_file()
                        if reuse_ok and mpath.is_file():
                            man = _load_json_optional(mpath) or {}
                            reuse_ok = _manifest_compatible(
                                man if isinstance(man, dict) else {},
                                aggregate_name=aggregate_name,
                                group_id=gid,
                                feast_subgroup=feast_svc,
                                month_yyyymm=yrmo,
                                cleaned_token=cleaned_token,
                                derived_stat=derived_memo[gid],
                                code_fp=code_fp,
                            ) and (_parquet_quick_stat(cache_p).get("num_rows") ==
                                   (man.get("output_parquet_stat") or {}).get("num_rows"))

                        if reuse_ok:
                            paths_by_gid[gid] = cache_p
                            logger.info(
                                "Feast group cache hit %s/%s slice=%s.",
                                gid,
                                yrmo,
                                cache_p.resolve(),
                            )
                            continue

                        tm_p = agg_dir / f"_{yrmo}.{gid}.staging.parquet"
                        if tm_p.is_file():
                            tm_p.unlink()
                        logger.info(
                            "Feast group cache miss→retrieve %s service=%s month=%s entities=%s",
                            gid,
                            feast_svc,
                            yrmo,
                            nrow,
                        )
                        if gid == "slow_snap" and slow_canonical_attach:
                            _attach_canonical_slow_snap_for_entities(
                                entity_parquet=ent_fp,
                                cleaned_bet_parquet=cfg.cleaned_bet_parquet,
                                canonical_mapping_parquet=default_canonical_mapping_parquet_path(),
                                slow_parquet=slow_derived_p,
                                output_parquet=tm_p,
                                duckdb_runtime=cfg.duckdb_runtime,
                            )
                        else:
                            _feast_features_to_parquet(
                                feast_repo=cfg.feast_repo,
                                entity_parquet=ent_fp,
                                feature_service_name=feast_svc,
                                features_parquet=tm_p,
                            )
                        if cache_p.is_file():
                            cache_p.unlink()
                        shutil.move(str(tm_p.resolve()), str(cache_p.resolve()))
                        dq = derived_memo[gid]
                        wrote_stat = _parquet_quick_stat(cache_p)
                        _write_group_cache_manifest(
                            mpath,
                            aggregate_name=aggregate_name,
                            group_id=gid,
                            feast_subgroup=feast_svc,
                            month_yyyymm=yrmo,
                            cleaned_token=cleaned_token,
                            derived_stat=dq,
                            code_fp=code_fp,
                            out_stat=wrote_stat,
                        )
                        paths_by_gid[gid] = cache_p

                    cleaned_only = paths_by_gid["cleaned"]
                    sp = paths_by_gid.get("slow_snap")

                    _duckdb_join_decomposed_month_features(
                        cleaned_p=cleaned_only,
                        trial_p=None,
                        slow_p=sp,
                        merged_out=f_fp,
                        duckdb_runtime=cfg.duckdb_runtime,
                    )
                    batch_feats.append(f_fp)
                else:
                    _feast_features_to_parquet(
                        feast_repo=cfg.feast_repo,
                        entity_parquet=ent_fp,
                        feature_service_name=cfg.feature_service_name,
                        features_parquet=f_fp,
                    )
                    batch_feats.append(f_fp)

            if not batch_feats:
                raise ValueError("Feast month batches produced no non-empty slices.")
            if staging_feat.is_file():
                staging_feat.unlink()
            _duckdb_union_parquet_into(batch_feats, staging_feat, duckdb_runtime=cfg.duckdb_runtime)
            logger.info(
                "Feast month batches merged %d slice(s) → %s (group_cache=%s)",
                len(batch_feats),
                staging_feat.resolve(),
                bool(use_group_cache),
            )
        else:
            nrow = _write_entity_parquet(
                cfg.cleaned_bet_parquet,
                entity_pq,
                duckdb_runtime=cfg.duckdb_runtime,
                max_rows=cfg.max_entity_rows,
            )
            if nrow <= 0:
                logger.warning("Entity parquet is empty (%s)", entity_pq)
            _feast_features_to_parquet(
                feast_repo=cfg.feast_repo,
                entity_parquet=entity_pq,
                feature_service_name=cfg.feature_service_name,
                features_parquet=staging_feat,
            )
        if versioned_out.is_file():
            versioned_out.unlink()
        n_rows = _join_labels_to_features(
            features_parquet=staging_feat,
            labels_parquet=cfg.labels_parquet,
            cleaned_bet_parquet=cfg.cleaned_bet_parquet,
            output_parquet=versioned_out,
            duckdb_runtime=cfg.duckdb_runtime,
        )
        shutil.copy2(versioned_out, cfg.output_parquet)
        _prune_versioned_training_sets(versions_dir, keep_last_n=cfg.training_set_keep_last_n_versions)
        if month_batch and use_group_cache:
            _persist_last_cleaned_fp_state(Path(out_dir) / "cache" / "feast_month_group_v1", cleaned_fp_root)
    finally:
        shutil.rmtree(batch_tmp, ignore_errors=True)
        for p in (entity_pq, staging_feat):
            if p.is_file():
                try:
                    p.unlink()
                except OSError:
                    logger.warning("Could not remove staging file %s", p)

    meta = out_dir / "training_set.manifest.json"
    meta.write_text(
        json.dumps(
            _training_manifest(
                output_parquet=cfg.output_parquet,
                feast_repo=cfg.feast_repo,
                feature_service=cfg.feature_service_name,
                row_count=n_rows,
                labels_parquet=cfg.labels_parquet,
                versioned_parquet=versioned_out,
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(
        "Training set written %s (versioned=%s rows=%s)",
        cfg.output_parquet.resolve(),
        versioned_out.resolve(),
        n_rows,
    )
    return cfg.output_parquet.resolve()


def _parse_args() -> BuildTrainingDataArgs:
    p = argparse.ArgumentParser(description="Feast features + labels → training Parquet.")
    p.add_argument(
        "--feast-repo",
        type=Path,
        default=DEFAULT_FEAST_REPO,
        help="Feast feature repo (default: trainer_hightier/feast_repo).",
    )
    p.add_argument(
        "--cleaned-bet",
        type=Path,
        default=DEFAULT_CLEANED_BET,
        help="Cleaned t_bet Parquet.",
    )
    p.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABELS,
        help="walkaway_labels.parquet (bet_id, canonical_id, label, censored, …).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output training Parquet path.",
    )
    p.add_argument(
        "--feature-service",
        type=str,
        default=DEFAULT_FEATURE_SERVICE,
        help="Feast feature service name (default: walkaway_bet_trial_v1).",
    )
    p.add_argument(
        "--disable-auto-feast-apply",
        action="store_true",
        dest="disable_auto_feast_apply",
        help="Do not run `feast apply` when feast_repo/data/registry.db is missing (fail instead).",
    )
    p.add_argument(
        "--materialize-derived",
        action="store_true",
        help="Run trial 1h + slow 180d materializers before Feast (heavy).",
    )
    p.add_argument(
        "--max-entity-rows",
        type=int,
        default=None,
        help="Limit entity rows for debugging (LIMIT in entity Parquet). Disables Feast month slicing.",
    )
    p.add_argument(
        "--feast-batch-by-month",
        dest="feast_batch_by_month",
        action="store_true",
        help=(
            "Run Feast retrieval in calendar-month batches (recommended for laptops; "
            "ignored when combined with --max-entity-rows)."
        ),
    )
    p.add_argument(
        "--training-retention",
        type=int,
        default=10,
        metavar="N",
        help="Keep newest N timestamped artifacts under training_data/versions/ (default: 10).",
    )
    p.add_argument(
        "--disable-feast-retrieval-cache",
        dest="disable_feast_retrieval_cache",
        action="store_true",
        help=(
            "When --feast-batch-by-month plus walkaway_bet_trial/v1 aggregate: bypass per-group Parquet caches."
        ),
    )
    p.add_argument(
        "--run-profile",
        type=str,
        default=_DEFAULT_RUN_PROFILE,
        help="DuckDB PRAGMA preset for non-Feast steps (see config.RUN_PROFILES).",
    )
    ns = p.parse_args()
    duckdb_rt, _, _ = configs_from_run_profile(get_run_profile(ns.run_profile))
    return BuildTrainingDataArgs(
        feast_repo=Path(ns.feast_repo).resolve(),
        cleaned_bet_parquet=Path(ns.cleaned_bet).resolve(),
        labels_parquet=Path(ns.labels).resolve(),
        output_parquet=Path(ns.output).resolve(),
        feature_service_name=str(ns.feature_service),
        materialize_derived_features=bool(ns.materialize_derived),
        max_entity_rows=ns.max_entity_rows,
        duckdb_runtime=duckdb_rt,
        feast_entity_batch_by_calendar_month=bool(ns.feast_batch_by_month),
        training_set_keep_last_n_versions=int(ns.training_retention),
        feast_retrieval_cache_enabled=not bool(ns.disable_feast_retrieval_cache),
        auto_feast_apply=not bool(ns.disable_auto_feast_apply),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    build_training_data(_parse_args())


if __name__ == "__main__":
    main()
