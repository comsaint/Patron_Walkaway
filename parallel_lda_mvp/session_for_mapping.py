"""Session 表在進入 canonical mapping 之前的管線邊界。

Canonical mapping **只**讀這裡回傳的 Parquet 路徑（視為「供 mapping 用的已清洗
session」）。

預設：若 repo 內存在 ``schema/preprocess_ingestion_fix_registry.yaml``（``tables.t_session``），則依
``SESSION-INGEST-FIX-001`` 物化一份含 ``__etl_insert_Dtm_synthetic`` 的 Parquet（L0 原始
``__etl_insert_Dtm`` 保留不變），供與 ``preprocess_session_v1`` / time semantics 對齊。
否則回傳 raw L0 路徑（passthrough）。

環境變數：

- ``PARALLEL_LDA_MVP_SESSION_INGEST_DISABLE``：設為 ``1`` / ``true`` 時強制 passthrough。
- ``PARALLEL_LDA_MVP_SESSION_INGEST_REGISTRY``：覆寫 registry YAML 路徑（檔案必須存在）。

規則或 cap 變更時請 **遞增** ``SESSION_MAPPING_CLEAN_LOGIC_VERSION``，以便在來源位元組
不變時仍能失效 mapping 快取。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

_CHUNK_BYTES = 8 * 1024 * 1024
_ENV_SESSION_INGEST_DISABLE = "PARALLEL_LDA_MVP_SESSION_INGEST_DISABLE"
_ENV_SESSION_INGEST_REGISTRY = "PARALLEL_LDA_MVP_SESSION_INGEST_REGISTRY"

# Bump when session→mapping 的列級規則變更（含 passthrough / cap 行為），即使 raw 檔未變。
SESSION_MAPPING_CLEAN_LOGIC_VERSION = "v1-session-ingest-cap-636"


def _repo_root() -> Path:
    """Repository root (parent of ``parallel_lda_mvp``)."""
    return Path(__file__).resolve().parent.parent


def _streaming_sha256_hex_file(path: Path, *, chunk_bytes: int = _CHUNK_BYTES) -> str:
    """Return lowercase hex SHA-256 of file contents (streaming)."""
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


def _session_ingest_registry_path(repo: Path) -> Path | None:
    """Resolve session ingestion registry path, or ``None`` to skip materialization."""
    v = os.environ.get(_ENV_SESSION_INGEST_DISABLE, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return None
    custom = os.environ.get(_ENV_SESSION_INGEST_REGISTRY, "").strip()
    if custom:
        p = Path(custom).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(
                f"{_ENV_SESSION_INGEST_REGISTRY}={custom!r} is not an existing file"
            )
        return p
    default = (repo / "schema" / "preprocess_ingestion_fix_registry.yaml").resolve()
    return default if default.is_file() else None


def _session_mapping_staging_dir() -> Path:
    """Directory for materialized session-for-mapping Parquet (under MVP canonical_cache)."""
    d = Path(__file__).resolve().parent / "canonical_cache" / "session_mapping_input"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _materialize_meta_path(output_parquet: Path) -> Path:
    """Sidecar JSON path for cache invalidation of materialized session parquet."""
    return output_parquet.with_suffix(".parquet.ingest_meta.json")


def _load_meta(path: Path) -> dict[str, Any] | None:
    """Load meta JSON if present and valid."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _materialize_session_with_synthetic_observed(
    *,
    raw_parquet: Path,
    output_parquet: Path,
    cap_sec: int,
) -> None:
    """Write Parquet = all L0 columns plus ``__etl_insert_Dtm_synthetic`` via DuckDB COPY."""
    try:
        import duckdb
    except ImportError as e:
        raise RuntimeError(
            "prepare_session_parquet_for_canonical_mapping materialization requires duckdb"
        ) from e
    need = frozenset({"__etl_insert_Dtm", "session_end_dtm"})
    con = duckdb.connect()
    try:
        cols = {
            str(r[0])
            for r in con.execute(
                "SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet(?))",
                [str(raw_parquet.resolve())],
            ).fetchall()
        }
    finally:
        con.close()
    missing = sorted(need - cols)
    if missing:
        raise ValueError(
            f"t_session L0 missing columns required for ingestion cap: {missing}; have sample of columns."
        )
    cap = int(cap_sec)
    rp = str(raw_parquet.resolve()).replace("'", "''")
    inner = f"""
SELECT
  src.*,
  CASE
    WHEN TRY_CAST(__etl_insert_Dtm AS TIMESTAMP) IS NOT NULL
     AND TRY_CAST(session_end_dtm AS TIMESTAMP) IS NOT NULL
    THEN LEAST(
      TRY_CAST(__etl_insert_Dtm AS TIMESTAMP),
      TRY_CAST(session_end_dtm AS TIMESTAMP) + INTERVAL {cap} SECOND
    )
    ELSE TRY_CAST(__etl_insert_Dtm AS TIMESTAMP)
  END AS __etl_insert_Dtm_synthetic
FROM read_parquet('{rp}') AS src
""".strip()
    con2 = duckdb.connect()
    try:
        output_parquet.parent.mkdir(parents=True, exist_ok=True)
        tmp = output_parquet.with_suffix(".parquet.tmp")
        if tmp.is_file():
            tmp.unlink()
        to_sql = str(tmp.resolve()).replace("\\", "/").replace("'", "''")
        con2.execute(f"COPY ({inner}) TO '{to_sql}' (FORMAT PARQUET);")
        if output_parquet.is_file():
            output_parquet.unlink()
        tmp.replace(output_parquet)
    finally:
        con2.close()


def _ensure_session_materialized_with_registry(p: Path, reg_path: Path) -> Path:
    """Return path to Parquet with synthetic observed-at; materialize when cache miss."""
    from pipelines.layered_data_assets.core.preprocess_session_ingestion_fix_registry_v1 import (
        load_preprocess_session_ingestion_fix_registry,
        resolve_session_ingest_fix001_cap_binding,
    )

    reg_doc = load_preprocess_session_ingestion_fix_registry(reg_path)
    cap_sec, fix_id, fix_ver, _applied = resolve_session_ingest_fix001_cap_binding(reg_doc)
    raw_sha = _streaming_sha256_hex_file(p)
    reg_sha = _streaming_sha256_hex_file(reg_path)
    out_name = (
        f"session_for_mapping_{raw_sha[:16]}_{reg_sha[:16]}_{SESSION_MAPPING_CLEAN_LOGIC_VERSION}.parquet"
    )
    out_p = _session_mapping_staging_dir() / out_name
    meta_p = _materialize_meta_path(out_p)
    reg_ver = str(reg_doc.get("registry_version") or "")
    meta_ok = _load_meta(meta_p)
    if (
        out_p.is_file()
        and isinstance(meta_ok, dict)
        and meta_ok.get("raw_session_sha256_hex") == raw_sha
        and meta_ok.get("registry_sha256_hex") == reg_sha
        and int(meta_ok.get("ingest_delay_cap_sec") or -1) == int(cap_sec)
        and meta_ok.get("fix_rule_id") == fix_id
        and meta_ok.get("fix_rule_version") == fix_ver
        and meta_ok.get("session_mapping_clean_logic_version") == SESSION_MAPPING_CLEAN_LOGIC_VERSION
    ):
        return out_p

    _materialize_session_with_synthetic_observed(raw_parquet=p, output_parquet=out_p, cap_sec=cap_sec)
    meta_p.write_text(
        json.dumps(
            {
                "raw_session_parquet": str(p.as_posix()),
                "raw_session_sha256_hex": raw_sha,
                "registry_path": str(reg_path.as_posix()),
                "registry_sha256_hex": reg_sha,
                "registry_version": reg_ver,
                "ingest_delay_cap_sec": int(cap_sec),
                "fix_rule_id": fix_id,
                "fix_rule_version": fix_ver,
                "session_mapping_clean_logic_version": SESSION_MAPPING_CLEAN_LOGIC_VERSION,
                "output_parquet": str(out_p.as_posix()),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return out_p


def prepare_session_parquet_for_canonical_mapping(raw_session_parquet: Path) -> Path:
    """從 L0 ``t_session`` 產出（或解析出）供 DuckDB canonical 使用的 Parquet 路徑。

    Args:
        raw_session_parquet: L0 ``gmwds_t_session.parquet``（或同等匯出）。

    Returns:
        物化後含 ``__etl_insert_Dtm_synthetic`` 之路徑，或無 registry / 停用時為 raw resolve 路徑。

    Raises:
        FileNotFoundError: 若 raw 路徑不存在或不是檔案；或 env 指定 registry 不存在。
        ValueError: 若 registry 與啟用規則不一致。
        RuntimeError: 若需物化但未安裝 duckdb。
    """
    p = raw_session_parquet.resolve()
    if not p.is_file():
        raise FileNotFoundError(f"raw t_session parquet not found: {p}")
    repo = _repo_root()
    reg_path = _session_ingest_registry_path(repo)
    if reg_path is None:
        return p
    return _ensure_session_materialized_with_registry(p, reg_path)
