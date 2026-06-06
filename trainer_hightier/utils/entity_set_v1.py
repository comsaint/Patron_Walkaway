"""Training entity set v1: ADT universe projection from cleaned bet base."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig, L0PreprocessDataScopeConfig
from trainer_hightier.utils.universe_cache_v1 import diff_selected_universe_added_player_ids
from trainer_hightier.utils.bet_l0_preprocess import (
    _bet_artifact_manifest_block,
    _clear_partitioned_dataset_dir,
    _enforce_no_null_gaming_day_event_partitioned,
    _path_posix,
    bet_clean_cache_manifest_path,
    cleaned_bet_dataset_has_any_parquet,
    default_cleaned_bet_parquet_path,
    partitioned_cleaned_bet_total_rows,
    resolved_cleaned_bet_read_parquet_sql,
)
from trainer_hightier.utils.duckdb_runtime import execute_sql_with_progress_oom_retry
from trainer_hightier.utils.source_manifest_v2 import default_cache_root, write_json_atomic

logger = logging.getLogger("trainer_hightier")

ENTITY_SET_KIND: Final[str] = "training_entity_set_v1"
ENTITY_SET_SCHEMA_VERSION: Final[int] = 1


def default_entity_set_cache_root(*, package_dir: Path | None = None) -> Path:
    """Return ``trainer_hightier/artifacts/cache/entity_set_v1``."""
    return default_cache_root(package_dir=package_dir) / "entity_set_v1"


def quantile_slug(quantile: float) -> str:
    """Filesystem-safe quantile slug."""
    qf = float(quantile)
    if not (0.0 < qf < 1.0):
        raise ValueError(f"quantile must be strictly between 0 and 1, got {qf!r}")
    return str(qf).replace(".", "p").replace("-", "neg")


def training_scope_fingerprint(scope: L0PreprocessDataScopeConfig) -> str:
    """SHA-256 hex of L0 training scope manifest block."""
    blob = json.dumps(scope.manifest_block(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _policy_blob_sha256(payload: dict[str, Any]) -> str:
    """Hash a sorted JSON policy blob."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def entity_set_policy_fingerprint_sha256_hex(
    *,
    selected_quantile: float,
    universe_fingerprint_sha256_hex: str,
    source_manifest_v2_fingerprint_sha256_hex: str,
    bet_base_fingerprint_sha256_hex: str,
    training_scope_fingerprint_sha256_hex: str,
) -> str:
    """Stable fingerprint for entity-set v1 universe used by labels / short PIT caches."""
    return _policy_blob_sha256(
        {
            "kind": ENTITY_SET_KIND,
            "selected_quantile": float(selected_quantile),
            "universe_fingerprint": str(universe_fingerprint_sha256_hex).strip(),
            "source_manifest_v2_fingerprint": str(source_manifest_v2_fingerprint_sha256_hex).strip(),
            "bet_base_cleaned_fingerprint": str(bet_base_fingerprint_sha256_hex).strip(),
            "training_scope_fingerprint": str(training_scope_fingerprint_sha256_hex).strip(),
        },
    )


def legacy_bet_segment_policy_fingerprint_sha256_hex(
    *,
    selected_quantile: float,
    bet_base_fingerprint_sha256_hex: str,
    training_scope_fingerprint_sha256_hex: str,
    partition_inventory_fingerprint_sha256_hex: str,
    source_manifest_v2_fingerprint_sha256_hex: str | None,
) -> str:
    """Fingerprint for legacy ADT segment path (pre entity-set v1)."""
    return _policy_blob_sha256(
        {
            "kind": "legacy_bet_segment_v1",
            "selected_quantile": float(selected_quantile),
            "bet_base_cleaned_fingerprint": str(bet_base_fingerprint_sha256_hex).strip(),
            "training_scope_fingerprint": str(training_scope_fingerprint_sha256_hex).strip(),
            "partition_inventory_fingerprint": str(partition_inventory_fingerprint_sha256_hex).strip(),
            "source_manifest_v2_fingerprint": str(source_manifest_v2_fingerprint_sha256_hex or "").strip(),
        },
    )


def bet_clean_policy_fingerprint_sha256_hex(
    *,
    cleaned_bet_fingerprint_sha256_hex: str,
    training_scope_fingerprint_sha256_hex: str,
    source_manifest_v2_fingerprint_sha256_hex: str | None,
    partition_inventory_fingerprint_sha256_hex: str | None,
) -> str:
    """Fingerprint for full cleaned bet without ADT entity projection."""
    return _policy_blob_sha256(
        {
            "kind": "bet_clean_v1",
            "cleaned_bet_fingerprint": str(cleaned_bet_fingerprint_sha256_hex).strip(),
            "training_scope_fingerprint": str(training_scope_fingerprint_sha256_hex).strip(),
            "source_manifest_v2_fingerprint": str(source_manifest_v2_fingerprint_sha256_hex or "").strip(),
            "partition_inventory_fingerprint": str(partition_inventory_fingerprint_sha256_hex or "").strip(),
        },
    )


def entity_set_policy_dir(
    *,
    cache_root: Path,
    quantile: float,
    scope_fingerprint: str,
    universe_fingerprint: str,
) -> Path:
    """Directory for one entity-set policy (quantile × scope × universe)."""
    root = Path(cache_root).resolve()
    return (
        root
        / f"quantile={quantile_slug(quantile)}"
        / f"scope={scope_fingerprint[:16]}"
        / f"universe={universe_fingerprint[:16]}"
    )


def _entity_projection_inner_sql(
    *,
    base_from: str,
    rank_esc: str,
    quantile: float,
) -> str:
    """DuckDB SELECT filtering base bets through ADT rank universe."""
    qf = float(quantile)
    return f"""
SELECT DISTINCT b.*
FROM {base_from} AS b
INNER JOIN (
  SELECT DISTINCT TRY_CAST(player_id AS BIGINT) AS pid
  FROM read_parquet('{rank_esc}')
  WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
    AND CAST(adt_percentile AS DOUBLE) >= {qf}
    AND has_slow_window_coverage
) AS u ON TRY_CAST(b.player_id AS BIGINT) = u.pid
""".strip()


def _copy_entity_set_to_output(
    *,
    inner_sql: str,
    output_root: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> None:
    """Write hive-partitioned training bet dataset (Step 3 compatible layout)."""
    o_esc = _path_posix(output_root).replace("'", "''")
    partition_opts = (
        "FORMAT PARQUET, COMPRESSION SNAPPY, PARTITION_BY (gaming_month, gaming_day_key), "
        "OVERWRITE_OR_IGNORE TRUE"
    )
    sql = f"""
COPY (
  SELECT
    s.*,
    strftime(TRY_CAST(s.gaming_day_event AS DATE), '%Y%m') AS gaming_month,
    strftime(TRY_CAST(s.gaming_day_event AS DATE), '%Y-%m-%d') AS gaming_day_key
  FROM ({inner_sql}) AS s
) TO '{o_esc}' ({partition_opts})
""".strip()
    _clear_partitioned_dataset_dir(output_root)
    execute_sql_with_progress_oom_retry(
        duckdb_runtime,
        sql,
        desc="[entity_set_v1] DuckDB project base bet to training universe",
        join_timeout_s=7200.0,
    )
    _enforce_no_null_gaming_day_event_partitioned(output_root, duckdb_cfg=duckdb_runtime)


def _archive_monthly_partitions(*, output_root: Path, partitions_dir: Path) -> list[str]:
    """Copy ``gaming_month=*`` shards into ``yyyymm=`` entity-set cache folders."""
    out = Path(output_root).resolve()
    dest_root = Path(partitions_dir).resolve()
    if dest_root.is_dir():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    months: list[str] = []
    for month_dir in sorted(out.glob("gaming_month=*")):
        if not month_dir.is_dir():
            continue
        ym = month_dir.name.split("=", 1)[-1].strip()
        if len(ym) != 6 or not ym.isdigit():
            continue
        target = dest_root / f"yyyymm={ym}"
        shutil.copytree(month_dir, target)
        months.append(ym)
    return sorted(months)


def find_stricter_cached_entity_set_quantile(
    *,
    cache_root: Path,
    scope_fingerprint: str,
    universe_fingerprint: str,
    current_quantile: float,
    bet_base_fingerprint_sha256_hex: str,
    source_manifest_v2_fingerprint_sha256_hex: str,
) -> tuple[float, dict[str, Any]] | None:
    """Return strictest cached quantile above *current_quantile* with matching base/source."""
    root = Path(cache_root).resolve()
    scope_s = str(scope_fingerprint)[:16]
    uni_s = str(universe_fingerprint)[:16]
    cur_q = float(current_quantile)
    best: tuple[float, dict[str, Any]] | None = None
    for qdir in sorted(root.glob("quantile=*")):
        manifest_path = qdir / f"scope={scope_s}" / f"universe={uni_s}" / "manifest.json"
        prev = load_entity_set_manifest(manifest_path)
        if prev is None:
            continue
        qf = float(prev.get("selected_quantile", -1))
        if qf <= cur_q:
            continue
        if str(prev.get("bet_base_cleaned_fingerprint")) != str(bet_base_fingerprint_sha256_hex):
            continue
        if str(prev.get("source_manifest_v2_fingerprint")) != str(
            source_manifest_v2_fingerprint_sha256_hex,
        ):
            continue
        if best is None or qf > best[0]:
            best = (qf, prev)
    return best


def write_entity_set_quantile_delta(
    *,
    policy_dir: Path,
    rank_table_path: Path,
    previous_quantile: float,
    current_quantile: float,
    universe_fingerprint_sha256_hex: str,
    source_manifest_v2_fingerprint_sha256_hex: str,
    bet_base_fingerprint_sha256_hex: str,
    training_scope_fingerprint_sha256_hex: str,
) -> dict[str, Any]:
    """Persist added ``player_id`` rows when quantile decreases (delta fill input)."""
    added = diff_selected_universe_added_player_ids(
        rank_table_path,
        previous_quantile=float(previous_quantile),
        current_quantile=float(current_quantile),
    )
    if not added:
        return {
            "entity_delta_row_count": 0,
            "entity_delta_previous_quantile": float(previous_quantile),
            "entity_delta_current_quantile": float(current_quantile),
        }
    delta_dir = Path(policy_dir).resolve() / "delta" / "latest"
    delta_dir.mkdir(parents=True, exist_ok=True)
    ids_path = delta_dir / "added_player_ids.parquet"
    pq.write_table(pa.table({"player_id": list(added)}), ids_path)
    prev_fp = entity_set_policy_fingerprint_sha256_hex(
        selected_quantile=float(previous_quantile),
        universe_fingerprint_sha256_hex=str(universe_fingerprint_sha256_hex),
        source_manifest_v2_fingerprint_sha256_hex=str(source_manifest_v2_fingerprint_sha256_hex),
        bet_base_fingerprint_sha256_hex=str(bet_base_fingerprint_sha256_hex),
        training_scope_fingerprint_sha256_hex=str(training_scope_fingerprint_sha256_hex),
    )
    manifest = {
        "schema_version": ENTITY_SET_SCHEMA_VERSION,
        "kind": "entity_set_quantile_delta_v1",
        "previous_quantile": float(previous_quantile),
        "current_quantile": float(current_quantile),
        "added_player_count": int(len(added)),
        "previous_entity_set_fingerprint": prev_fp,
        "added_player_ids_path": str(ids_path.resolve()),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = delta_dir / "manifest.json"
    write_json_atomic(manifest_path, manifest)
    return {
        "entity_delta_row_count": int(len(added)),
        "entity_delta_previous_quantile": float(previous_quantile),
        "entity_delta_current_quantile": float(current_quantile),
        "entity_delta_manifest_path": str(manifest_path.resolve()),
        "entity_delta_added_player_ids_path": str(ids_path.resolve()),
        "entity_delta_added_player_ids": list(added),
        "entity_delta_previous_entity_set_fingerprint_sha256_hex": prev_fp,
    }


def load_entity_set_manifest(path: Path) -> dict[str, Any] | None:
    """Load entity set manifest JSON or ``None`` when missing/corrupt."""
    p = Path(path).resolve()
    if not p.is_file():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def entity_set_cache_is_hit(
    *,
    manifest_path: Path,
    output_root: Path,
    selected_quantile: float,
    universe_fingerprint_sha256_hex: str,
    source_manifest_v2_fingerprint_sha256_hex: str,
    bet_base_fingerprint_sha256_hex: str,
    training_scope_fingerprint_sha256_hex: str,
) -> bool:
    """Return True when cached entity set matches policy and output exists."""
    if not cleaned_bet_dataset_has_any_parquet(output_root):
        return False
    prev = load_entity_set_manifest(manifest_path)
    if prev is None:
        return False
    return (
        float(prev.get("selected_quantile", -1)) == float(selected_quantile)
        and str(prev.get("universe_fingerprint")) == str(universe_fingerprint_sha256_hex)
        and str(prev.get("source_manifest_v2_fingerprint")) == str(source_manifest_v2_fingerprint_sha256_hex)
        and str(prev.get("bet_base_cleaned_fingerprint")) == str(bet_base_fingerprint_sha256_hex)
        and str(prev.get("training_scope_fingerprint")) == str(training_scope_fingerprint_sha256_hex)
    )


def bet_base_cleaned_fingerprint_sha256_hex(base_cleaned_parquet: Path) -> str:
    """Content fingerprint for base cleaned bet artifact block."""
    block = _bet_artifact_manifest_block(base_cleaned_parquet)
    blob = json.dumps(block, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def retire_bet_segment_cache_sidecar(cleaned_bet_output: Path) -> bool:
    """Remove legacy ADT segment ``.cache.json`` sidecar when present."""
    sidecar = bet_clean_cache_manifest_path(Path(cleaned_bet_output))
    if not sidecar.is_file():
        return False
    sidecar.unlink()
    logger.info("[entity_set_v1] retired legacy bet segment cache sidecar: %s", sidecar.resolve())
    return True


def materialize_entity_set_v1_cached(
    *,
    base_cleaned_parquet: Path,
    rank_table_path: Path,
    rank_fingerprint_sha256_hex: str,
    selected_quantile: float,
    training_scope: L0PreprocessDataScopeConfig,
    source_manifest_v2_fingerprint_sha256_hex: str,
    duckdb_runtime: DuckDbRuntimeConfig,
    cache_root: Path | None = None,
    output_parquet: Path | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Project cleaned bet base through rank universe; write Step 3-compatible output."""
    t0 = time.perf_counter()
    base = Path(base_cleaned_parquet).resolve()
    rank_p = Path(rank_table_path).resolve()
    if not cleaned_bet_dataset_has_any_parquet(base):
        raise FileNotFoundError(f"base cleaned bet missing: {base}")
    if not rank_p.is_file():
        raise FileNotFoundError(f"ADT rank table missing: {rank_p}")
    out = Path(output_parquet).resolve() if output_parquet is not None else default_cleaned_bet_parquet_path()
    scope_fp = training_scope_fingerprint(training_scope)
    base_fp = bet_base_cleaned_fingerprint_sha256_hex(base)
    uroot = default_entity_set_cache_root() if cache_root is None else Path(cache_root).resolve()
    policy_dir = entity_set_policy_dir(
        cache_root=uroot,
        quantile=float(selected_quantile),
        scope_fingerprint=scope_fp,
        universe_fingerprint=str(rank_fingerprint_sha256_hex),
    )
    manifest_path = policy_dir / "manifest.json"
    partitions_dir = policy_dir / "partitions"
    cache_hit = use_cache and entity_set_cache_is_hit(
        manifest_path=manifest_path,
        output_root=out,
        selected_quantile=float(selected_quantile),
        universe_fingerprint_sha256_hex=str(rank_fingerprint_sha256_hex),
        source_manifest_v2_fingerprint_sha256_hex=str(source_manifest_v2_fingerprint_sha256_hex),
        bet_base_fingerprint_sha256_hex=base_fp,
        training_scope_fingerprint_sha256_hex=scope_fp,
    )
    if not cache_hit:
        inner = _entity_projection_inner_sql(
            base_from=resolved_cleaned_bet_read_parquet_sql(base),
            rank_esc=_path_posix(rank_p).replace("'", "''"),
            quantile=float(selected_quantile),
        )
        _copy_entity_set_to_output(
            inner_sql=inner,
            output_root=out,
            duckdb_runtime=duckdb_runtime,
        )
        entity_months = _archive_monthly_partitions(output_root=out, partitions_dir=partitions_dir)
        row_n = partitioned_cleaned_bet_total_rows(out)
        manifest = {
            "schema_version": ENTITY_SET_SCHEMA_VERSION,
            "kind": ENTITY_SET_KIND,
            "selected_quantile": float(selected_quantile),
            "universe_fingerprint": str(rank_fingerprint_sha256_hex),
            "source_manifest_v2_fingerprint": str(source_manifest_v2_fingerprint_sha256_hex),
            "bet_base_cleaned_fingerprint": base_fp,
            "training_scope_fingerprint": scope_fp,
            "entity_months": entity_months,
            "row_count": int(row_n),
            "output_path": str(out.resolve()),
            "partitions_dir": str(partitions_dir.resolve()),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_json_atomic(manifest_path, manifest)
    else:
        prev = load_entity_set_manifest(manifest_path) or {}
        row_n = int(prev.get("row_count") or partitioned_cleaned_bet_total_rows(out))
        entity_months = list(prev.get("entity_months") or [])
    retire_bet_segment_cache_sidecar(out)
    policy_fp = entity_set_policy_fingerprint_sha256_hex(
        selected_quantile=float(selected_quantile),
        universe_fingerprint_sha256_hex=str(rank_fingerprint_sha256_hex),
        source_manifest_v2_fingerprint_sha256_hex=str(source_manifest_v2_fingerprint_sha256_hex),
        bet_base_fingerprint_sha256_hex=base_fp,
        training_scope_fingerprint_sha256_hex=scope_fp,
    )
    delta_meta: dict[str, Any] = {}
    stricter = find_stricter_cached_entity_set_quantile(
        cache_root=uroot,
        scope_fingerprint=scope_fp,
        universe_fingerprint=str(rank_fingerprint_sha256_hex),
        current_quantile=float(selected_quantile),
        bet_base_fingerprint_sha256_hex=base_fp,
        source_manifest_v2_fingerprint_sha256_hex=str(source_manifest_v2_fingerprint_sha256_hex),
    )
    if stricter is not None:
        prev_q, _ = stricter
        delta_meta = write_entity_set_quantile_delta(
            policy_dir=policy_dir,
            rank_table_path=rank_p,
            previous_quantile=float(prev_q),
            current_quantile=float(selected_quantile),
            universe_fingerprint_sha256_hex=str(rank_fingerprint_sha256_hex),
            source_manifest_v2_fingerprint_sha256_hex=str(source_manifest_v2_fingerprint_sha256_hex),
            bet_base_fingerprint_sha256_hex=base_fp,
            training_scope_fingerprint_sha256_hex=scope_fp,
        )
    return {
        "entity_set_cache_hit": bool(cache_hit),
        "entity_set_elapsed_seconds": round(time.perf_counter() - t0, 6),
        "entity_set_row_count": int(row_n),
        "entity_set_manifest_path": str(manifest_path.resolve()),
        "entity_set_partitions_dir": str(partitions_dir.resolve()),
        "entity_set_output_path": str(out.resolve()),
        "entity_set_months": entity_months,
        "entity_set_training_scope_fingerprint": scope_fp,
        "entity_set_policy_fingerprint_sha256_hex": policy_fp,
        **delta_meta,
    }
