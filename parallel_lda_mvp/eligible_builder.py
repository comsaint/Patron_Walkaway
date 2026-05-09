"""Rated-eligible ``player_id`` list via **trainer** DuckDB canonical path + local cache.

Caches **only under** ``parallel_lda_mvp/canonical_cache/`` (never writes trainer
``data/canonical_mapping.*``).

Canonical mapping 讀入的是 **供 mapping 用的 session Parquet**（見
``session_for_mapping.prepare_session_parquet_for_canonical_mapping``）；預設可能為
ingestion-cap 物化檔（含 ``__etl_insert_Dtm_synthetic``），見該模組說明。

**Fingerprint** = SHA-256 of ( **SHA-256 over entire mapping-input file bytes** + ``|`` +
cutoff (naive-HK ISO) + ``|`` + ``SESSION_MAPPING_CLEAN_LOGIC_VERSION`` ). 來源檔位元組、
cutoff、或 session→mapping 清洗邏輯版本任一變更都會失效快取（含「raw 不變僅改清洗
程式」的情況）。仍須對 mapping 輸入檔做 **整檔串流** hash（成本見 README）。

**Staleness**：on-disk mapping 在 fingerprint 相符時 **一律載入**（服務 fail-open）。
``MAPPING_MAX_STALENESS_MIN`` / ``MAPPING_HARD_STALE_LIMIT_MIN`` 僅用於
``mapping_identity_health_from_meta`` 的稽核欄位（L0/L1/L2）；L2 保守降級行為尚未實作。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from parallel_lda_mvp.canonical_mapping_runtime_config import (
    MAPPING_HARD_STALE_LIMIT_MIN,
    MAPPING_MAX_STALENESS_MIN,
)
from parallel_lda_mvp.session_for_mapping import (
    SESSION_MAPPING_CLEAN_LOGIC_VERSION,
    prepare_session_parquet_for_canonical_mapping,
)

_CHUNK_BYTES = 8 * 1024 * 1024


def _repo_parallel_root() -> Path:
    """Directory containing this module (``.../parallel_lda_mvp``)."""
    return Path(__file__).resolve().parent


def canonical_cache_dir() -> Path:
    """Return ``parallel_lda_mvp/canonical_cache`` (MVP-owned artifacts)."""
    return _repo_parallel_root() / "canonical_cache"


def _cutoff_naive_hk_like_trainer(dt: datetime) -> pd.Timestamp:
    """Match trainer ``train_end`` naive-HK convention (DEC-018 style)."""
    te = pd.Timestamp(dt)
    if te.tzinfo is not None:
        return te.tz_convert("Asia/Hong_Kong").replace(tzinfo=None)
    return te


def streaming_sha256_hex_file(path: Path, *, chunk_bytes: int = _CHUNK_BYTES) -> str:
    """Return lowercase hex SHA-256 of the full file contents (streaming, bounded buffer)."""
    p = path.resolve()
    if not p.is_file():
        raise FileNotFoundError(f"parquet not found: {p}")
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            block = f.read(chunk_bytes)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def mapping_input_fingerprint(
    mapping_input_parquet: Path,
    cutoff_dtm: datetime,
    *,
    cleaning_logic_version: str,
) -> tuple[str, str]:
    """Return ``(fingerprint, mapping_input_content_sha256_hex)`` for cache keys."""
    p = mapping_input_parquet.resolve()
    content_hex = streaming_sha256_hex_file(p)
    cutoff_key = _cutoff_naive_hk_like_trainer(cutoff_dtm).isoformat()
    outer = hashlib.sha256()
    outer.update(content_hex.encode("ascii"))
    outer.update(b"|")
    outer.update(cutoff_key.encode("utf-8"))
    outer.update(b"|")
    outer.update(cleaning_logic_version.encode("utf-8"))
    return outer.hexdigest(), content_hex


def _mapping_to_eligible_player_ids(mapping: pd.DataFrame) -> pd.DataFrame:
    """Same rows as ``trainer.identity.build_rated_eligible_player_ids_df`` output column."""
    if mapping.empty or "player_id" not in mapping.columns:
        return pd.DataFrame({"player_id": pd.Series([], dtype="int64")})
    out = mapping[["player_id"]].drop_duplicates().reset_index(drop=True)
    out["player_id"] = pd.to_numeric(out["player_id"], errors="coerce").dropna().astype("int64")
    return out


def _build_mapping_via_trainer_duckdb(session_parquet: Path, cutoff_dtm: datetime) -> pd.DataFrame:
    """Trainer Step-3 DuckDB path: links in DuckDB, M:N in ``identity``."""
    from trainer.identity import build_canonical_mapping_from_links
    from trainer.training.identity_runtime import build_canonical_links_and_dummy_from_duckdb

    links_df, dummy_pids = build_canonical_links_and_dummy_from_duckdb(session_parquet, cutoff_dtm)
    return build_canonical_mapping_from_links(links_df, dummy_pids)


def _cache_paths(fp: str) -> tuple[Path, Path]:
    """Return ``(mapping.parquet, meta.json)`` paths for fingerprint ``fp``."""
    base = canonical_cache_dir()
    return base / f"mapping_{fp}.parquet", base / f"mapping_{fp}.meta.json"


def _parse_meta_built_at_utc(meta: dict[str, object]) -> datetime | None:
    """Parse ``built_at`` from mapping sidecar meta as timezone-aware UTC, or ``None``."""
    raw = meta.get("built_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        built = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if built.tzinfo is None:
        built = built.replace(tzinfo=timezone.utc)
    return built.astimezone(timezone.utc)


def mapping_identity_health_from_meta(
    meta: dict[str, object] | None,
    *,
    fingerprint: str,
) -> dict[str, Any]:
    """Audit fields for a mapping cache hit (stale snapshot still allowed for serving).

    ``degrade_level``: 0=fresh, 1=stale beyond ``MAPPING_MAX_STALENESS_MIN``,
    2=beyond hard limit (conservative degrade **not implemented**; same data as L1 for now).
    """
    hard_limit = max(int(MAPPING_HARD_STALE_LIMIT_MIN), int(MAPPING_MAX_STALENESS_MIN))
    base: dict[str, Any] = {
        "hard_stale_limit_min": hard_limit,
        "identity_mode": "unknown",
        "l2_conservative_degrade": "not_implemented",
        "mapping_age_min": None,
        "mapping_max_staleness_min": int(MAPPING_MAX_STALENESS_MIN),
        "mapping_snapshot_id": fingerprint,
        "degrade_level": 0,
    }
    if meta is None:
        base["identity_mode"] = "unknown"
        return base
    built = _parse_meta_built_at_utc(meta)
    if built is None:
        base["identity_mode"] = "unknown"
        return base
    age_min = (datetime.now(timezone.utc) - built).total_seconds() / 60.0
    base["mapping_age_min"] = round(age_min, 3)
    if age_min <= float(MAPPING_MAX_STALENESS_MIN):
        base["identity_mode"] = "fresh"
        base["degrade_level"] = 0
        return base
    base["identity_mode"] = "stale_snapshot"
    if age_min <= float(hard_limit):
        base["degrade_level"] = 1
    else:
        base["degrade_level"] = 2
    return base


def _try_load_mapping_cache(fp: str) -> tuple[pd.DataFrame | None, dict[str, object] | None]:
    """Return cached ``(mapping_df, meta)`` when fingerprint matches; never drops on staleness."""
    pq_path, meta_path = _cache_paths(fp)
    if not pq_path.is_file() or not meta_path.is_file():
        return None, None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if meta.get("fingerprint") != fp:
        return None, None
    mapping = pd.read_parquet(pq_path)
    if not {"player_id", "canonical_id"}.issubset(set(mapping.columns)):
        return None, None
    return mapping, meta


def _write_cached_mapping(
    fp: str,
    mapping: pd.DataFrame,
    *,
    raw_session_parquet: Path,
    mapping_input_parquet: Path,
    cutoff_dtm: datetime,
    mapping_input_content_sha256_hex: str,
    cleaning_logic_version: str,
) -> None:
    """Atomically write mapping + sidecar meta under ``canonical_cache_dir()``."""
    canonical_cache_dir().mkdir(parents=True, exist_ok=True)
    pq_path, meta_path = _cache_paths(fp)
    tmp_pq = pq_path.with_suffix(".parquet.tmp")
    tmp_meta = meta_path.with_suffix(".json.tmp")
    mapping.to_parquet(tmp_pq, index=False)
    raw_p = raw_session_parquet.resolve()
    in_p = mapping_input_parquet.resolve()
    st = in_p.stat()
    payload = {
        "fingerprint": fp,
        "session_cleaning_logic_version": cleaning_logic_version,
        "mapping_input_content_sha256": mapping_input_content_sha256_hex,
        "mapping_input_parquet": str(in_p.as_posix()),
        "mapping_input_size_bytes": int(st.st_size),
        "raw_session_parquet": str(raw_p.as_posix()),
        "session_content_sha256": mapping_input_content_sha256_hex,
        "session_parquet": str(in_p.as_posix()),
        "session_size_bytes": int(st.st_size),
        "cutoff_dtm": cutoff_dtm.isoformat(),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp_meta.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_pq.replace(pq_path)
    tmp_meta.replace(meta_path)


def resolve_canonical_mapping_parquet_path(
    session_parquet: Path,
    cutoff_dtm: datetime,
) -> Path:
    """Return on-disk canonical mapping Parquet path for the resolved fingerprint (may not exist yet)."""
    raw = session_parquet.resolve()
    if not raw.is_file():
        raise FileNotFoundError(f"t_session parquet not found: {raw}")
    mapping_input = prepare_session_parquet_for_canonical_mapping(raw)
    fp, _ = mapping_input_fingerprint(
        mapping_input,
        cutoff_dtm,
        cleaning_logic_version=SESSION_MAPPING_CLEAN_LOGIC_VERSION,
    )
    return _cache_paths(fp)[0]


def get_or_build_cached_canonical_mapping_with_health(
    session_parquet: Path,
    cutoff_dtm: datetime,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return ``(mapping_df, identity_health)``; stale cache rows remain usable (serving fail-open).

    ``identity_health`` includes ``mapping_snapshot_id``, ``mapping_age_min``, ``identity_mode``,
    ``degrade_level`` (0/1/2; level 2 conservative degrade is not implemented yet).
    """
    raw = session_parquet.resolve()
    if not raw.is_file():
        raise FileNotFoundError(f"t_session parquet not found: {raw}")
    mapping_input = prepare_session_parquet_for_canonical_mapping(raw)
    fp, content_hex = mapping_input_fingerprint(
        mapping_input,
        cutoff_dtm,
        cleaning_logic_version=SESSION_MAPPING_CLEAN_LOGIC_VERSION,
    )
    hit, meta = _try_load_mapping_cache(fp)
    if hit is not None:
        health = mapping_identity_health_from_meta(meta, fingerprint=fp)
        return hit, health
    mapping = _build_mapping_via_trainer_duckdb(mapping_input, cutoff_dtm)
    _write_cached_mapping(
        fp,
        mapping,
        raw_session_parquet=raw,
        mapping_input_parquet=mapping_input,
        cutoff_dtm=cutoff_dtm,
        mapping_input_content_sha256_hex=content_hex,
        cleaning_logic_version=SESSION_MAPPING_CLEAN_LOGIC_VERSION,
    )
    _, meta_after = _try_load_mapping_cache(fp)
    if meta_after is None:
        return mapping, mapping_identity_health_from_meta(
            {"built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
            fingerprint=fp,
        )
    health = mapping_identity_health_from_meta(meta_after, fingerprint=fp)
    return mapping, health


def get_or_build_cached_canonical_mapping(
    session_parquet: Path,
    cutoff_dtm: datetime,
) -> pd.DataFrame:
    """Return canonical mapping DataFrame; rebuild when mapping input, cutoff, or clean version change.

    Args:
        session_parquet: L0 raw ``t_session`` Parquet path; cleaned input 由此模組解析。
    """
    return get_or_build_cached_canonical_mapping_with_health(session_parquet, cutoff_dtm)[0]


def build_eligible_player_ids_parquet(
    *,
    session_parquet: Path,
    cutoff_dtm: datetime,
    output_parquet: Path,
) -> tuple[Path, dict[str, Any]]:
    """Write single-column ``player_id`` Parquet (BET-DQ-03).

    Uses ``get_or_build_cached_canonical_mapping_with_health``; fingerprint 變更才重建，
    **不因** ``MAPPING_MAX_STALENESS_MIN`` 丟棄 on-disk mapping（服務可繼續用舊 snapshot）。

    Args:
        session_parquet: L0 ``gmwds_t_session.parquet``（或 env 覆寫）；mapping 實際讀入
            路徑由 ``prepare_session_parquet_for_canonical_mapping`` 決定。
        cutoff_dtm: Leakage cutoff (B1).
        output_parquet: Destination path (per-run eligible list).

    Returns:
        ``(resolved output_parquet path, identity_health dict)``.

    Raises:
        RuntimeError: If eligible list is empty after build.
    """
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    mapping, health = get_or_build_cached_canonical_mapping_with_health(session_parquet, cutoff_dtm)
    eligible = _mapping_to_eligible_player_ids(mapping)
    if eligible.empty:
        raise RuntimeError(
            "rated eligible player_id list is empty; check t_session export and cutoff."
        )
    eligible.to_parquet(output_parquet, index=False)
    return output_parquet.resolve(), health
