"""Session 表在進入 canonical mapping 之前的管線邊界。

Canonical mapping **只**讀這裡回傳的 Parquet 路徑（視為「供 mapping 用的已清洗
session」）。

預設：若 repo 內存在 ``schema/preprocess_l0_data_contract_registry.yaml``（``tables.t_session``），則依
registry 的 ``session_for_mapping_materialization_contract`` 與 ``SESSION-INGEST-FIX-001`` 物化
session 專屬清洗 Parquet（L0 原始 ``__etl_insert_Dtm`` 保留不變），並附加欄位供 canonical mapping /
稽核（見 registry 契約說明）。

此清洗邏輯 **僅適用 t_session**。
否則回傳 raw L0 路徑（passthrough）。

環境變數：

- ``PARALLEL_LDA_MVP_SESSION_INGEST_DISABLE``：設為 ``1`` / ``true`` 時強制 passthrough。
- ``PARALLEL_LDA_MVP_SESSION_INGEST_REGISTRY``：覆寫 registry YAML 路徑（檔案必須存在）。

``SESSION_MAPPING_CLEAN_LOGIC_VERSION`` 由 registry
``session_for_mapping_materialization_contract.clean_logic_version`` 解析；變更該欄位可失效
mapping 快取（即使 L0 檔未變）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from pipelines.layered_data_assets.core.preprocess_session_ingestion_fix_registry_v1 import (
    SessionMaterializationContract,
    load_preprocess_session_ingestion_fix_registry,
    resolve_session_for_mapping_materialization_contract,
    resolve_session_ingest_fix001_cap_binding,
)

_CHUNK_BYTES = 8 * 1024 * 1024
_ENV_SESSION_INGEST_DISABLE = "PARALLEL_LDA_MVP_SESSION_INGEST_DISABLE"
_ENV_SESSION_INGEST_REGISTRY = "PARALLEL_LDA_MVP_SESSION_INGEST_REGISTRY"


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
    default = (repo / "schema" / "preprocess_l0_data_contract_registry.yaml").resolve()
    return default if default.is_file() else None


def get_session_mapping_clean_logic_version() -> str:
    """Return ``clean_logic_version`` from the active session registry, or passthrough sentinel."""
    reg = _session_ingest_registry_path(_repo_root())
    if reg is None:
        return "passthrough-no-registry"
    doc = load_preprocess_session_ingestion_fix_registry(reg)
    return resolve_session_for_mapping_materialization_contract(doc).clean_logic_version


SESSION_MAPPING_CLEAN_LOGIC_VERSION = get_session_mapping_clean_logic_version()


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


def _ingestion_episode_case_sql(contract: SessionMaterializationContract) -> str:
    """Build CASE expression for ``ingestion_episode_id`` from registry episode tags."""
    if not contract.episode_calendar_tags:
        return "CAST(NULL AS VARCHAR) AS ingestion_episode_id"
    whens = " ".join(
        [
            f"WHEN observed_day = DATE '{day}' THEN '{eid.replace(chr(39), chr(39) * 2)}'"
            for day, eid in contract.episode_calendar_tags
        ]
    )
    return f"CASE {whens} ELSE NULL END AS ingestion_episode_id"


def _base_typed_ctes(*, rp_escaped: str, cap_sec: int) -> str:
    """Return ``typed`` / ``observed_norm`` / ``event_norm`` CTE SQL (WITH prefix, no trailing comma)."""
    cap = int(cap_sec)
    return f"""
WITH typed AS (
  SELECT
    src.*,
    TRY_CAST(session_end_dtm AS TIMESTAMP) AS session_end_ts,
    TRY_CAST(session_start_dtm AS TIMESTAMP) AS session_start_ts,
    TRY_CAST(__etl_insert_Dtm AS TIMESTAMP) AS observed_raw_ts,
    TRY_CAST(lud_dtm AS TIMESTAMP) AS lud_ts,
    TRY_CAST(player_id AS BIGINT) AS player_id_i64,
    TRY_CAST(is_manual AS BIGINT) AS is_manual_i64
  FROM read_parquet('{rp_escaped}') AS src
),
observed_norm AS (
  SELECT
    t.*,
    CASE
      WHEN observed_raw_ts IS NOT NULL AND session_end_ts IS NOT NULL
      THEN LEAST(observed_raw_ts, session_end_ts + INTERVAL {cap} SECOND)
      ELSE observed_raw_ts
    END AS __etl_insert_Dtm_synthetic
  FROM typed AS t
),
event_norm AS (
  SELECT
    o.*,
    o.session_end_ts AS event_time_true,
    CASE
      WHEN o.__etl_insert_Dtm_synthetic IS NOT NULL
      THEN LEAST(
        COALESCE(o.session_end_ts, o.session_start_ts, o.__etl_insert_Dtm_synthetic),
        o.__etl_insert_Dtm_synthetic
      )
      ELSE COALESCE(o.session_end_ts, o.session_start_ts)
    END AS event_time_effective,
    CASE
      WHEN o.session_end_ts IS NOT NULL THEN 'session_end'
      WHEN o.session_start_ts IS NOT NULL THEN 'session_start'
      WHEN o.__etl_insert_Dtm_synthetic IS NOT NULL THEN 'observed_fallback'
      ELSE 'missing'
    END AS event_time_source,
    CAST(date_trunc('day', o.__etl_insert_Dtm_synthetic) AS DATE) AS observed_day
  FROM observed_norm AS o
)
""".strip()


def _pairing_ctes_sql(order_by: str) -> str:
    """Return correction pairing CTEs (comma after ``event_norm`` must be added by caller)."""
    return f"""
pair_counts AS (
  SELECT
    player_id_i64,
    session_start_ts,
    SUM(CASE WHEN is_manual_i64 = 1 THEN 1 ELSE 0 END) AS n_manual,
    SUM(CASE WHEN is_manual_i64 = 0 THEN 1 ELSE 0 END) AS n_nonmanual
  FROM event_norm
  WHERE player_id_i64 IS NOT NULL AND session_start_ts IS NOT NULL
  GROUP BY 1, 2
),
pair_mixed AS (
  SELECT
    player_id_i64,
    session_start_ts
  FROM pair_counts
  WHERE n_manual > 0 AND n_nonmanual > 0
),
pair_ranked AS (
  SELECT
    e.player_id_i64,
    e.session_start_ts,
    e.session_id,
    e.is_manual_i64,
    e.lud_ts,
    e.observed_raw_ts,
    ROW_NUMBER() OVER (
      PARTITION BY e.player_id_i64, e.session_start_ts
      ORDER BY {order_by}
    ) AS correction_rank
  FROM event_norm AS e
  INNER JOIN pair_mixed AS m
    ON e.player_id_i64 = m.player_id_i64
   AND e.session_start_ts = m.session_start_ts
)
""".strip()


def _materialization_select_excl_sql() -> str:
    """Return SELECT list prefix (through ``event_time_source``) for materialization."""
    return """
SELECT
  e.* EXCLUDE (
    session_end_ts,
    session_start_ts,
    observed_raw_ts,
    lud_ts,
    player_id_i64,
    is_manual_i64,
    observed_day
  ),
  e.event_time_true,
  e.event_time_effective,
  e.event_time_source,
""".strip()


def _materialization_lag_cols_sql() -> str:
    """Return lag metric columns for materialization output."""
    return """
  CASE
    WHEN e.event_time_true IS NOT NULL AND e.observed_raw_ts IS NOT NULL
    THEN EXTRACT(EPOCH FROM (e.observed_raw_ts - e.event_time_true))
    ELSE NULL
  END AS obs_lag_sec_raw,
  CASE
    WHEN e.event_time_effective IS NOT NULL AND e.__etl_insert_Dtm_synthetic IS NOT NULL
    THEN EXTRACT(EPOCH FROM (e.__etl_insert_Dtm_synthetic - e.event_time_effective))
    ELSE NULL
  END AS obs_lag_sec_logical
""".strip()


def _materialization_sql_with_pairing(
    *, base: str, episode_sql: str, select_core: str, lag_cols: str, contract: SessionMaterializationContract
) -> str:
    """Assemble SQL when ``correction_pairing.enabled`` is true."""
    pair_sql = _pairing_ctes_sql(contract.correction_winner_order_sql)
    flags = """
  CASE WHEN m.player_id_i64 IS NOT NULL THEN TRUE ELSE FALSE END AS is_correction_pair,
  CASE WHEN r.correction_rank = 1 THEN TRUE ELSE FALSE END AS is_correction_winner,
""".strip()
    tail = """
FROM event_norm AS e
LEFT JOIN pair_mixed AS m
  ON e.player_id_i64 = m.player_id_i64
 AND e.session_start_ts = m.session_start_ts
LEFT JOIN pair_ranked AS r
  ON e.player_id_i64 = r.player_id_i64
 AND e.session_start_ts = r.session_start_ts
 AND e.session_id = r.session_id
 AND e.is_manual_i64 = r.is_manual_i64
 AND (e.lud_ts = r.lud_ts OR (e.lud_ts IS NULL AND r.lud_ts IS NULL))
 AND (
   e.observed_raw_ts = r.observed_raw_ts
   OR (e.observed_raw_ts IS NULL AND r.observed_raw_ts IS NULL)
 )
 AND r.correction_rank = 1
""".strip()
    return f"{base},\n{pair_sql}\n{select_core}\n  {flags}\n  {episode_sql},\n{lag_cols}\n{tail}"


def _materialization_sql_without_pairing(*, base: str, episode_sql: str, select_core: str, lag_cols: str) -> str:
    """Assemble SQL when correction pairing is disabled."""
    flags = """
  CAST(FALSE AS BOOLEAN) AS is_correction_pair,
  CAST(FALSE AS BOOLEAN) AS is_correction_winner,
""".strip()
    return f"{base}\n{select_core}\n  {flags}\n  {episode_sql},\n{lag_cols}\nFROM event_norm AS e"


def _session_materialization_inner_sql(
    *,
    rp_escaped: str,
    cap_sec: int,
    contract: SessionMaterializationContract,
) -> str:
    """Return DuckDB SQL body (WITH … SELECT …) for session materialization."""
    base = _base_typed_ctes(rp_escaped=rp_escaped, cap_sec=cap_sec)
    episode_sql = _ingestion_episode_case_sql(contract)
    select_core = _materialization_select_excl_sql()
    lag_cols = _materialization_lag_cols_sql()
    if contract.correction_pairing_enabled:
        return _materialization_sql_with_pairing(
            base=base, episode_sql=episode_sql, select_core=select_core, lag_cols=lag_cols, contract=contract
        )
    return _materialization_sql_without_pairing(
        base=base, episode_sql=episode_sql, select_core=select_core, lag_cols=lag_cols
    )


def _materialize_session_with_synthetic_observed(
    *,
    raw_parquet: Path,
    output_parquet: Path,
    cap_sec: int,
    contract: SessionMaterializationContract,
) -> None:
    """Write session-cleaned parquet for canonical mapping via DuckDB COPY."""
    try:
        import duckdb
    except ImportError as e:
        raise RuntimeError(
            "prepare_session_parquet_for_canonical_mapping materialization requires duckdb"
        ) from e
    need = frozenset(contract.required_l0_columns)
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
            f"t_session L0 missing columns required for session cleaning: {missing}; have sample of columns."
        )
    rp = str(raw_parquet.resolve()).replace("'", "''")
    inner = _session_materialization_inner_sql(rp_escaped=rp, cap_sec=cap_sec, contract=contract)
    raw_sz = raw_parquet.stat().st_size
    print(
        "[parallel_lda_mvp] session_for_mapping: L0 column check OK; "
        f"materializing cleaned parquet (raw_bytes={raw_sz:,}, cap_sec={cap_sec}, "
        f"out={output_parquet.name}) ...",
        flush=True,
    )

    con2 = duckdb.connect()
    try:
        output_parquet.parent.mkdir(parents=True, exist_ok=True)
        tmp = output_parquet.with_suffix(".parquet.tmp")
        if tmp.is_file():
            tmp.unlink()
        to_sql = str(tmp.resolve()).replace("\\", "/").replace("'", "''")
        print(
            "[parallel_lda_mvp] session_for_mapping: DuckDB COPY (full scan + rewrite) "
            f"-> {tmp.name} - can take minutes on large t_session; started at "
            f"{time.strftime('%H:%M:%S')} ...",
            flush=True,
        )
        t_copy = time.perf_counter()
        con2.execute(f"COPY ({inner}) TO '{to_sql}' (FORMAT PARQUET);")
        print(
            "[parallel_lda_mvp] session_for_mapping: DuckDB COPY finished "
            f"in {time.perf_counter() - t_copy:.1f}s",
            flush=True,
        )
        if output_parquet.is_file():
            output_parquet.unlink()
        tmp.replace(output_parquet)
    finally:
        con2.close()


def _ensure_session_materialized_with_registry(p: Path, reg_path: Path) -> Path:
    """Return path to cleaned session parquet; materialize when cache miss."""
    reg_doc = load_preprocess_session_ingestion_fix_registry(reg_path)
    cap_sec, fix_id, fix_ver, _applied = resolve_session_ingest_fix001_cap_binding(reg_doc)
    contract = resolve_session_for_mapping_materialization_contract(reg_doc)
    raw_sha = _streaming_sha256_hex_file(p)
    reg_sha = _streaming_sha256_hex_file(reg_path)
    clean_ver = contract.clean_logic_version
    out_name = f"session_for_mapping_{raw_sha[:16]}_{reg_sha[:16]}_{clean_ver}.parquet"
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
        and meta_ok.get("session_mapping_clean_logic_version") == clean_ver
    ):
        print(
            f"[parallel_lda_mvp] session_for_mapping: cache hit, using {out_p.name}",
            flush=True,
        )
        return out_p

    print(
        "[parallel_lda_mvp] session_for_mapping: cache miss - "
        f"materializing {out_name} (registry + raw changed or first run)",
        flush=True,
    )
    _materialize_session_with_synthetic_observed(
        raw_parquet=p, output_parquet=out_p, cap_sec=cap_sec, contract=contract
    )
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
                "session_mapping_clean_logic_version": clean_ver,
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
        物化後含 session 專屬清洗欄位（含 ``__etl_insert_Dtm_synthetic``）之路徑，
        或無 registry / 停用時為 raw resolve 路徑。

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
