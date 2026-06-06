"""Month-sharded short-term PIT cache for training Step 3.5."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import duckdb
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DEFAULT_TRAINING_SHORT_TERM_MATERIALIZE_BATCH_SIZE,
    DuckDbRuntimeConfig,
    SHORT_TERM_PIT_CACHE_DIRNAME,
    SHORT_TERM_PIT_CACHE_SCHEMA_VERSION,
    SHORT_TERM_PIT_SOURCE_NEIGHBOR_MONTHS,
    SHORT_TERM_PIT_SUPPLIER_FAMILY,
    default_hightier_serving_config,
)
from trainer_hightier.utils.cache_invalidation_v1 import short_pit_invalid_months
from trainer_hightier.feature_experiment.materialize_fe_derived import (
    BOUNDED_SHORT_TERM_MATERIALIZER_VERSION,
    materialize_fe_derived_short_term_parquet,
)
from trainer_hightier.utils.bet_l0_preprocess import cleaned_bet_artifact_fingerprint_block
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

_CODE_MODULE_PATHS: Final[tuple[Path, ...]] = (
    Path(__file__).resolve().parent / "materialize_fe_derived.py",
    Path(__file__).resolve().parents[1] / "serving" / "short_term_scoring_context.py",
)

REASON_CODE_CHANGED: Final[str] = "code_changed"
REASON_POLICY_CHANGED: Final[str] = "policy_changed"
REASON_MAPPING_CHANGED: Final[str] = "mapping_changed"
REASON_SOURCE_CHANGED: Final[str] = "source_changed"
REASON_UNIVERSE_CHANGED: Final[str] = "universe_changed"
REASON_SCHEMA_CHANGED: Final[str] = "schema_changed"
REASON_FORCE_REFRESH: Final[str] = "force_refresh"
REASON_SHARD_MISSING: Final[str] = "shard_missing"
REASON_MANIFEST_MISSING: Final[str] = "manifest_missing"
REASON_ENTITY_DELTA_FILL: Final[str] = "entity_delta_fill"


@dataclass(frozen=True)
class ShortTermPitCachePlan:
    """Reuse plan for month-sharded short-term PIT cache."""

    cache_root: Path
    hit_shards: tuple[str, ...]
    miss_shards: tuple[str, ...]
    reason_counts: dict[str, int]
    shard_paths: dict[str, Path]
    out_columns: tuple[str, ...]


def short_term_pit_cache_root(training_data_dir: Path) -> Path:
    """Return cache root under training data artifacts."""
    return Path(training_data_dir).resolve() / "cache" / SHORT_TERM_PIT_CACHE_DIRNAME


def _global_manifest_path(cache_root: Path) -> Path:
    return Path(cache_root).resolve() / "manifest.json"


def _shard_parquet_path(cache_root: Path, yyyymm: str) -> Path:
    return Path(cache_root).resolve() / "shards" / f"yyyymm={yyyymm}" / "data.parquet"


def _shard_manifest_path(cache_root: Path, yyyymm: str) -> Path:
    return _shard_parquet_path(cache_root, yyyymm).parent / "shard_manifest.json"


def _path_esc(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_fingerprint() -> str:
    digest = hashlib.sha256()
    for module_path in _CODE_MODULE_PATHS:
        digest.update(module_path.read_bytes())
    digest.update(BOUNDED_SHORT_TERM_MATERIALIZER_VERSION.encode("utf-8"))
    return digest.hexdigest()


def _policy_fingerprint(*, batch_size: int) -> dict[str, Any]:
    cfg = default_hightier_serving_config()
    return {
        "hot_feature_pool_lookback_hours": int(cfg.hot_feature_pool_lookback_hours),
        "expand_canonical_aliases": False,
        "hightier_scorer_pool_player_fanout_cap": int(cfg.hightier_scorer_pool_player_fanout_cap),
        "training_materialize_batch_size": int(batch_size),
    }


def _columns_fingerprint(out_columns: tuple[str, ...]) -> str:
    payload = "|".join(out_columns)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parquet_quick_stat(path: Path) -> dict[str, Any]:
    resolved = Path(path).resolve()
    stat = resolved.stat()
    meta = pq.ParquetFile(resolved).metadata
    num_rows = int(meta.num_rows) if meta is not None else -1
    return {
        "path": str(resolved),
        "mtime_ns": int(stat.st_mtime_ns),
        "size_bytes": int(stat.st_size),
        "num_rows": num_rows,
    }


def _prev_month_yyyymm(yyyymm: str) -> str:
    year = int(yyyymm[:4])
    month = int(yyyymm[4:6])
    month -= 1
    if month < 1:
        month = 12
        year -= 1
    return f"{year:04d}{month:02d}"


def _source_invalidated_shards(
    shard_months: tuple[str, ...],
    *,
    recompute_months: tuple[str, ...],
) -> set[str]:
    """Return shard months that must recompute due to L1 dirty-month expansion."""
    if not recompute_months:
        return set()
    expanded = short_pit_invalid_months(
        set(recompute_months),
        neighbor_backfill=int(SHORT_TERM_PIT_SOURCE_NEIGHBOR_MONTHS),
    )
    invalidated: set[str] = set()
    for yyyymm in shard_months:
        if yyyymm in expanded or _prev_month_yyyymm(yyyymm) in expanded:
            invalidated.add(yyyymm)
    return invalidated


def _expanded_source_invalid_months(recompute_months: tuple[str, ...]) -> tuple[str, ...]:
    """Expand L1 dirty months to the short PIT invalidation window."""
    if not recompute_months:
        return ()
    expanded = short_pit_invalid_months(
        set(recompute_months),
        neighbor_backfill=int(SHORT_TERM_PIT_SOURCE_NEIGHBOR_MONTHS),
    )
    return tuple(sorted(expanded))


def list_training_payout_months(
    training_parquet: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> tuple[str, ...]:
    """List distinct ``payout_complete_dtm`` months present in training parquet."""
    tp = Path(training_parquet).resolve()
    if not tp.is_file():
        raise FileNotFoundError(training_parquet)
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        rows = con.execute(
            f"""
            SELECT DISTINCT strftime(CAST(payout_complete_dtm AS TIMESTAMPTZ), '%Y%m') AS yyyymm
            FROM read_parquet('{_path_esc(tp)}')
            WHERE payout_complete_dtm IS NOT NULL
              AND TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
            ORDER BY 1
            """,
        ).fetchall()
    finally:
        con.close()
    return tuple(str(r[0]) for r in rows if r and r[0] is not None)


def compute_shard_universe_fingerprint(
    training_parquet: Path,
    *,
    yyyymm: str,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> tuple[str, int]:
    """Return SHA-256 fingerprint and row count for one payout month shard."""
    tp = Path(training_parquet).resolve()
    ym = str(yyyymm).strip()
    if len(ym) != 6 or not ym.isdigit():
        raise ValueError(f"yyyymm must be six digits, got {yyyymm!r}")
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        row = con.execute(
            f"""
            SELECT
              COUNT(*)::BIGINT AS num_rows,
              sha256(string_agg(
                printf(
                  '%s|%s|%s',
                  CAST(TRY_CAST(bet_id AS DOUBLE) AS VARCHAR),
                  CAST(TRY_CAST(player_id AS BIGINT) AS VARCHAR),
                  CAST(CAST(payout_complete_dtm AS TIMESTAMPTZ) AS VARCHAR)
                ),
                '|' ORDER BY CAST(payout_complete_dtm AS TIMESTAMPTZ), TRY_CAST(bet_id AS DOUBLE)
              )) AS universe_fp
            FROM read_parquet('{_path_esc(tp)}')
            WHERE payout_complete_dtm IS NOT NULL
              AND TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
              AND TRY_CAST(player_id AS BIGINT) IS NOT NULL
              AND strftime(CAST(payout_complete_dtm AS TIMESTAMPTZ), '%Y%m') = ?
            """,
            [ym],
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return hashlib.sha256(b"").hexdigest(), 0
    num_rows = int(row[0] or 0)
    fp_raw = row[1]
    if num_rows == 0:
        return hashlib.sha256(b"").hexdigest(), 0
    if fp_raw is None:
        raise ValueError(f"universe fingerprint query returned null for shard {ym}")
    return str(fp_raw), num_rows


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _shard_manifest_compatible(
    manifest: dict[str, Any],
    *,
    yyyymm: str,
    code_fp: str,
    policy_fp: dict[str, Any],
    mapping_sha256: str,
    partition_inventory_fp: str | None,
    entity_set_fp: str | None,
    columns_fp: str,
    universe_fp: str,
    num_rows: int,
    out_stat: dict[str, Any],
) -> str | None:
    """Return miss reason code when shard manifest is stale; else ``None`` (hit)."""
    if int(manifest.get("schema_version", -1)) != SHORT_TERM_PIT_CACHE_SCHEMA_VERSION:
        return REASON_MANIFEST_MISSING
    if str(manifest.get("yyyymm", "")).strip() != yyyymm:
        return REASON_SHARD_MISSING
    if str(manifest.get("code_fingerprint", "")).strip() != code_fp:
        return REASON_CODE_CHANGED
    if manifest.get("policy_fingerprint") != policy_fp:
        return REASON_POLICY_CHANGED
    if str(manifest.get("canonical_mapping_sha256", "")).strip() != mapping_sha256:
        return REASON_MAPPING_CHANGED
    have_entity = str(manifest.get("entity_set_fingerprint", "")).strip()
    if entity_set_fp is not None and str(entity_set_fp).strip():
        if have_entity != str(entity_set_fp).strip():
            return REASON_UNIVERSE_CHANGED
    elif partition_inventory_fp is not None:
        have_inv = manifest.get("partition_inventory_fingerprint_sha256")
        if str(have_inv or "").strip() != partition_inventory_fp:
            return REASON_SOURCE_CHANGED
    schema_fp = str(
        manifest.get("output_schema_fingerprint") or manifest.get("columns_fingerprint") or "",
    ).strip()
    if schema_fp != columns_fp:
        return REASON_SCHEMA_CHANGED
    if str(manifest.get("training_universe_fingerprint", "")).strip() != universe_fp:
        return REASON_UNIVERSE_CHANGED
    if int(manifest.get("training_universe_num_rows", -1)) != int(num_rows):
        return REASON_UNIVERSE_CHANGED
    have_out = manifest.get("output_parquet_stat")
    if not isinstance(have_out, dict):
        return REASON_SHARD_MISSING
    if have_out.get("num_rows") != out_stat.get("num_rows"):
        return REASON_UNIVERSE_CHANGED
    return None


def _write_shard_manifest(
    cache_root: Path,
    yyyymm: str,
    *,
    code_fp: str,
    policy_fp: dict[str, Any],
    mapping_sha256: str,
    partition_inventory_fp: str | None,
    entity_set_fp: str | None,
    source_invalid_months: tuple[str, ...],
    columns_fp: str,
    universe_fp: str,
    num_rows: int,
    out_stat: dict[str, Any],
) -> None:
    mpath = _shard_manifest_path(cache_root, yyyymm)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SHORT_TERM_PIT_CACHE_SCHEMA_VERSION,
        "supplier_family": SHORT_TERM_PIT_SUPPLIER_FAMILY,
        "yyyymm": yyyymm,
        "materializer_version": BOUNDED_SHORT_TERM_MATERIALIZER_VERSION,
        "code_fingerprint": code_fp,
        "policy_fingerprint": policy_fp,
        "canonical_mapping_sha256": mapping_sha256,
        "entity_set_fingerprint": entity_set_fp,
        "source_invalid_months": list(source_invalid_months),
        "partition_inventory_fingerprint_sha256": partition_inventory_fp,
        "output_schema_fingerprint": columns_fp,
        "columns_fingerprint": columns_fp,
        "training_universe_fingerprint": universe_fp,
        "training_universe_num_rows": int(num_rows),
        "output_parquet_stat": out_stat,
    }
    mpath.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def plan_short_term_pit_cache(
    *,
    training_parquet: Path,
    cache_root: Path,
    out_columns: tuple[str, ...],
    canonical_mapping_parquet: Path,
    batch_size: int,
    duckdb_runtime: DuckDbRuntimeConfig,
    partition_inventory_fingerprint_sha256: str | None = None,
    entity_set_fingerprint_sha256_hex: str | None = None,
    recompute_months: tuple[str, ...] = (),
    force_refresh: bool = False,
) -> ShortTermPitCachePlan:
    """Decide month-shard cache hits and misses."""
    root = Path(cache_root).resolve()
    code_fp = _code_fingerprint()
    policy_fp = _policy_fingerprint(batch_size=int(batch_size))
    columns_fp = _columns_fingerprint(out_columns)
    mapping_sha256 = _sha256_file(Path(canonical_mapping_parquet).resolve())
    shard_months = list_training_payout_months(training_parquet, duckdb_runtime=duckdb_runtime)
    source_invalid = _source_invalidated_shards(shard_months, recompute_months=recompute_months)

    hit: list[str] = []
    miss: list[str] = []
    reasons: dict[str, int] = {}
    shard_paths: dict[str, Path] = {}

    for yyyymm in shard_months:
        shard_p = _shard_parquet_path(root, yyyymm)
        shard_paths[yyyymm] = shard_p
        if force_refresh:
            miss.append(yyyymm)
            reasons[REASON_FORCE_REFRESH] = reasons.get(REASON_FORCE_REFRESH, 0) + 1
            continue
        if yyyymm in source_invalid:
            miss.append(yyyymm)
            reasons[REASON_SOURCE_CHANGED] = reasons.get(REASON_SOURCE_CHANGED, 0) + 1
            continue
        if not shard_p.is_file():
            miss.append(yyyymm)
            reasons[REASON_SHARD_MISSING] = reasons.get(REASON_SHARD_MISSING, 0) + 1
            continue
        universe_fp, num_rows = compute_shard_universe_fingerprint(
            training_parquet,
            yyyymm=yyyymm,
            duckdb_runtime=duckdb_runtime,
        )
        manifest = _load_json(_shard_manifest_path(root, yyyymm)) or {}
        reason = _shard_manifest_compatible(
            manifest,
            yyyymm=yyyymm,
            code_fp=code_fp,
            policy_fp=policy_fp,
            mapping_sha256=mapping_sha256,
            partition_inventory_fp=partition_inventory_fingerprint_sha256,
            entity_set_fp=entity_set_fingerprint_sha256_hex,
            columns_fp=columns_fp,
            universe_fp=universe_fp,
            num_rows=num_rows,
            out_stat=_parquet_quick_stat(shard_p),
        )
        if reason is None:
            hit.append(yyyymm)
        else:
            miss.append(yyyymm)
            reasons[reason] = reasons.get(reason, 0) + 1

    return ShortTermPitCachePlan(
        cache_root=root,
        hit_shards=tuple(hit),
        miss_shards=tuple(miss),
        reason_counts=reasons,
        shard_paths=shard_paths,
        out_columns=out_columns,
    )


def _shard_delta_fill_eligible(
    *,
    cache_root: Path,
    yyyymm: str,
    previous_entity_set_fp: str,
    added_player_ids: tuple[int, ...],
) -> bool:
    """Return True when an existing shard can merge quantile-delta rows."""
    if not added_player_ids or not str(previous_entity_set_fp).strip():
        return False
    shard_p = _shard_parquet_path(cache_root, yyyymm)
    if not shard_p.is_file():
        return False
    manifest = _load_json(_shard_manifest_path(cache_root, yyyymm)) or {}
    have_fp = str(manifest.get("entity_set_fingerprint", "")).strip()
    return have_fp == str(previous_entity_set_fp).strip()


def _merge_delta_rows_into_shard(
    *,
    shard_parquet: Path,
    delta_parquet: Path,
    out_columns: tuple[str, ...],
    duckdb_runtime: DuckDbRuntimeConfig,
) -> None:
    """Merge delta short-PIT rows into an existing month shard (delta wins on ``bet_id``)."""
    shard = Path(shard_parquet).resolve()
    delta = Path(delta_parquet).resolve()
    if not shard.is_file() or not delta.is_file():
        raise FileNotFoundError(f"delta merge missing shard={shard} delta={delta}")
    col_sql = ", ".join(f'"{c}"' for c in out_columns)
    shard_esc = _path_esc(shard)
    delta_esc = _path_esc(delta)
    tmp_esc = _path_esc(shard.parent / "data.__delta_merge__.parquet")
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(
            f"""
            COPY (
              SELECT {col_sql} FROM (
                SELECT {col_sql}, 1 AS _prio FROM read_parquet('{delta_esc}')
                UNION ALL BY NAME
                SELECT {col_sql}, 0 AS _prio FROM read_parquet('{shard_esc}')
              )
              QUALIFY ROW_NUMBER() OVER (
                PARTITION BY TRY_CAST(bet_id AS DOUBLE) ORDER BY _prio DESC
              ) = 1
            ) TO '{tmp_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
            """,
        )
    finally:
        con.close()
    Path(shard.parent / "data.__delta_merge__.parquet").replace(shard)


def _assemble_short_term_shards(
    shard_paths: dict[str, Path],
    *,
    shard_order: tuple[str, ...],
    out_columns: tuple[str, ...],
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> None:
    """Merge month shards into the legacy single short-term PIT parquet."""
    ordered_paths = [shard_paths[ym] for ym in shard_order if shard_paths[ym].is_file()]
    if not ordered_paths:
        raise ValueError("short-term PIT cache assembly has no shard parquet files")
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    col_sql = ", ".join(f'"{c}"' for c in out_columns)
    dst_esc = _path_esc(dst)
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        if len(ordered_paths) == 1:
            src_esc = _path_esc(ordered_paths[0])
            con.execute(
                f"COPY (SELECT {col_sql} FROM read_parquet('{src_esc}')) "
                f"TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)",
            )
        else:
            paths_esc = "[" + ",".join(f"'{_path_esc(p)}'" for p in ordered_paths) + "]"
            con.execute(
                f"COPY (SELECT {col_sql} FROM read_parquet({paths_esc})) "
                f"TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)",
            )
    finally:
        con.close()


def _write_global_manifest(
    cache_root: Path,
    *,
    plan: ShortTermPitCachePlan,
    code_fp: str,
    policy_fp: dict[str, Any],
    mapping_sha256: str,
    partition_inventory_fp: str | None,
    entity_set_fp: str | None,
    source_invalid_months: tuple[str, ...],
    columns_fp: str,
    shard_months: tuple[str, ...],
) -> None:
    payload = {
        "schema_version": SHORT_TERM_PIT_CACHE_SCHEMA_VERSION,
        "supplier_family": SHORT_TERM_PIT_SUPPLIER_FAMILY,
        "materializer_version": BOUNDED_SHORT_TERM_MATERIALIZER_VERSION,
        "code_fingerprint": code_fp,
        "policy_fingerprint": policy_fp,
        "canonical_mapping_sha256": mapping_sha256,
        "entity_set_fingerprint": entity_set_fp,
        "source_invalid_months": list(source_invalid_months),
        "partition_inventory_fingerprint_sha256": partition_inventory_fp,
        "output_schema_fingerprint": columns_fp,
        "columns_fingerprint": columns_fp,
        "shard_months": list(shard_months),
        "hit_shards": list(plan.hit_shards),
        "miss_shards": list(plan.miss_shards),
        "reason_counts": dict(plan.reason_counts),
    }
    mpath = _global_manifest_path(cache_root)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def materialize_fe_derived_short_term_parquet_with_cache(
    *,
    cleaned_bet_parquet: Path,
    training_parquet_for_bet_ids: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    canonical_mapping_parquet: Path,
    short_term_columns: tuple[str, ...],
    trial_columns: tuple[str, ...],
    batch_size: int = DEFAULT_TRAINING_SHORT_TERM_MATERIALIZE_BATCH_SIZE,
    cache_root: Path | None = None,
    partition_inventory_fingerprint_sha256: str | None = None,
    entity_set_fingerprint_sha256_hex: str | None = None,
    entity_delta_added_player_ids: tuple[int, ...] = (),
    previous_entity_set_fingerprint_sha256_hex: str | None = None,
    recompute_months: tuple[str, ...] = (),
    force_refresh: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Materialize short-term PIT with month-sharded cache reuse."""
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    tp = Path(training_parquet_for_bet_ids).resolve()
    cmap = Path(canonical_mapping_parquet).resolve()
    dst = Path(out_parquet).resolve()
    out_cols = tuple(dict.fromkeys(("bet_id", *trial_columns, *short_term_columns)))
    root = short_term_pit_cache_root(tp.parent) if cache_root is None else Path(cache_root).resolve()

    source_invalid_months = _expanded_source_invalid_months(recompute_months)
    plan = plan_short_term_pit_cache(
        training_parquet=tp,
        cache_root=root,
        out_columns=out_cols,
        canonical_mapping_parquet=cmap,
        batch_size=int(batch_size),
        duckdb_runtime=duckdb_runtime,
        partition_inventory_fingerprint_sha256=partition_inventory_fingerprint_sha256,
        entity_set_fingerprint_sha256_hex=entity_set_fingerprint_sha256_hex,
        recompute_months=recompute_months,
        force_refresh=force_refresh,
    )
    code_fp = _code_fingerprint()
    policy_fp = _policy_fingerprint(batch_size=int(batch_size))
    columns_fp = _columns_fingerprint(out_cols)
    mapping_sha256 = _sha256_file(cmap)
    shard_months = list_training_payout_months(tp, duckdb_runtime=duckdb_runtime)

    delta_fill_shards: list[str] = []
    t_delta = time.perf_counter()
    added_ids = tuple(int(pid) for pid in entity_delta_added_player_ids)
    prev_entity_fp = (
        str(previous_entity_set_fingerprint_sha256_hex).strip()
        if previous_entity_set_fingerprint_sha256_hex is not None
        else ""
    )
    for yyyymm in plan.miss_shards:
        shard_out = _shard_parquet_path(root, yyyymm)
        shard_out.parent.mkdir(parents=True, exist_ok=True)
        used_delta = _shard_delta_fill_eligible(
            cache_root=root,
            yyyymm=yyyymm,
            previous_entity_set_fp=prev_entity_fp,
            added_player_ids=added_ids,
        )
        if used_delta:
            delta_tmp = shard_out.parent / "data.__delta__.parquet"
            materialize_fe_derived_short_term_parquet(
                cleaned_bet_parquet=cleaned_bet_parquet,
                training_parquet_for_bet_ids=tp,
                out_parquet=delta_tmp,
                duckdb_runtime=duckdb_runtime,
                canonical_mapping_parquet=cmap,
                short_term_columns=short_term_columns,
                trial_columns=trial_columns,
                batch_size=int(batch_size),
                payout_yyyymm=yyyymm,
                restrict_player_ids=added_ids,
            )
            _merge_delta_rows_into_shard(
                shard_parquet=shard_out,
                delta_parquet=delta_tmp,
                out_columns=out_cols,
                duckdb_runtime=duckdb_runtime,
            )
            delta_tmp.unlink(missing_ok=True)
            delta_fill_shards.append(yyyymm)
        else:
            materialize_fe_derived_short_term_parquet(
                cleaned_bet_parquet=cleaned_bet_parquet,
                training_parquet_for_bet_ids=tp,
                out_parquet=shard_out,
                duckdb_runtime=duckdb_runtime,
                canonical_mapping_parquet=cmap,
                short_term_columns=short_term_columns,
                trial_columns=trial_columns,
                batch_size=int(batch_size),
                payout_yyyymm=yyyymm,
            )
        universe_fp, num_rows = compute_shard_universe_fingerprint(
            tp,
            yyyymm=yyyymm,
            duckdb_runtime=duckdb_runtime,
        )
        _write_shard_manifest(
            root,
            yyyymm,
            code_fp=code_fp,
            policy_fp=policy_fp,
            mapping_sha256=mapping_sha256,
            partition_inventory_fp=partition_inventory_fingerprint_sha256,
            entity_set_fp=entity_set_fingerprint_sha256_hex,
            source_invalid_months=source_invalid_months,
            columns_fp=columns_fp,
            universe_fp=universe_fp,
            num_rows=num_rows,
            out_stat=_parquet_quick_stat(shard_out),
        )

    _assemble_short_term_shards(
        plan.shard_paths,
        shard_order=shard_months,
        out_columns=out_cols,
        out_parquet=dst,
        duckdb_runtime=duckdb_runtime,
    )
    _write_global_manifest(
        root,
        plan=plan,
        code_fp=code_fp,
        policy_fp=policy_fp,
        mapping_sha256=mapping_sha256,
        partition_inventory_fp=partition_inventory_fingerprint_sha256,
        entity_set_fp=entity_set_fingerprint_sha256_hex,
        source_invalid_months=source_invalid_months,
        columns_fp=columns_fp,
        shard_months=shard_months,
    )

    total_shards = len(shard_months)
    hit_ratio = float(len(plan.hit_shards) / total_shards) if total_shards else 1.0
    reason_counts = dict(plan.reason_counts)
    if delta_fill_shards:
        reason_counts[REASON_ENTITY_DELTA_FILL] = int(len(delta_fill_shards))
    meta = {
        "cache_root": str(root),
        "cache_hit_shards": list(plan.hit_shards),
        "cache_miss_shards": list(plan.miss_shards),
        "cache_reason_counts": reason_counts,
        "cache_hit_ratio": round(hit_ratio, 6),
        "short_term_pit_primitive_hit_ratio": round(hit_ratio, 6),
        "cache_hit": len(plan.miss_shards) == 0 and not force_refresh,
        "training_materialize_batch_size": int(batch_size),
        "shard_months": list(shard_months),
        "short_term_pit_recompute_months": list(recompute_months),
        "short_term_pit_source_invalid_months": list(source_invalid_months),
        "entity_set_fingerprint_sha256_hex": entity_set_fingerprint_sha256_hex,
        "short_term_pit_delta_fill_shards": list(delta_fill_shards),
        "entity_delta_fill_elapsed_seconds": round(time.perf_counter() - t_delta, 6),
    }
    logger.info(
        "[bounded_short_term] cache summary hit=%d miss=%d ratio=%.3f reasons=%s -> %s",
        len(plan.hit_shards),
        len(plan.miss_shards),
        hit_ratio,
        plan.reason_counts,
        dst.name,
    )
    return dst, meta
