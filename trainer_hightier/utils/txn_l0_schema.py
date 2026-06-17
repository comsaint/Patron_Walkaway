"""Canonical ``t_casino_txn`` L0 column types (ground truth: ClickHouse DDL / dictionary).

DDL source: ``schema/schema.txt`` → ``GDP_GMWDS_Raw.t_casino_txn``.
L0 DuckDB casts: ``TXN_L0_RAW_COLUMN_TYPES``; dictionary: §5 in
``schema/GDP_GMWDS_Raw_Schema_Dictionary.md``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Final

from trainer_hightier.config import _REPO_ROOT

TXN_L0_SCHEMA_CONTRACT_ID: Final[str] = "gmwds_t_casino_txn_l0_schema_v1"
TXN_L0_SCHEMA_DICTIONARY_REF: Final[str] = "schema/GDP_GMWDS_Raw_Schema_Dictionary.md#5"
TXN_L0_SCHEMA_DDL_REF: Final[str] = "schema/schema.txt#t_casino_txn"
_TXN_DDL_TABLE_MARKER: Final[str] = "CREATE TABLE GDP_GMWDS_Raw.t_casino_txn"
_TXN_DDL_ENGINE_MARKER: Final[str] = "ENGINE = ReplicatedReplacingMergeTree"

# Raw columns in DDL column order (65 columns).
TXN_L0_RAW_COLUMN_TYPES: Final[tuple[tuple[str, str], ...]] = (
    ("casino_txn_id", "BIGINT"),
    ("uuid", "VARCHAR"),
    ("topology_id", "INTEGER"),
    ("gaming_day", "DATE"),
    ("type", "VARCHAR"),
    ("action", "VARCHAR"),
    ("start_dtm", "TIMESTAMPTZ"),
    ("end_dtm", "TIMESTAMPTZ"),
    ("receive_dtm", "TIMESTAMPTZ"),
    ("complete_dtm", "TIMESTAMPTZ"),
    ("status", "VARCHAR"),
    ("user_id", "INTEGER"),
    ("auth_token", "VARCHAR"),
    ("player_id", "BIGINT"),
    ("chip_ids_in", "VARCHAR"),
    ("chip_ids_out", "VARCHAR"),
    ("updated_dtm", "TIMESTAMPTZ"),
    ("txn_value", "DECIMAL(19,4)"),
    ("dealer_id", "INTEGER"),
    ("supervisor_id", "INTEGER"),
    ("dealer_name", "VARCHAR"),
    ("supervisor_name", "VARCHAR"),
    ("player_type", "VARCHAR"),
    ("location", "VARCHAR"),
    ("user_name", "VARCHAR"),
    ("player_name", "VARCHAR"),
    ("casino_player_id", "VARCHAR"),
    ("sub_type", "VARCHAR"),
    ("agent_name", "VARCHAR"),
    ("agent_id", "VARCHAR"),
    ("buyin_status", "VARCHAR"),
    ("document_id", "VARCHAR"),
    ("chipset_label_in", "VARCHAR"),
    ("chipset_label_out", "VARCHAR"),
    ("pit_name", "VARCHAR"),
    ("gaming_area", "VARCHAR"),
    ("associated_with_session", "INTEGER"),
    ("sent_to_cms", "INTEGER"),
    ("session_id", "BIGINT"),
    ("device_type", "VARCHAR"),
    ("approver_name", "VARCHAR"),
    ("bet_id", "BIGINT"),
    ("marker_balance", "DECIMAL(19,4)"),
    ("rim_balance", "DECIMAL(19,4)"),
    ("cashier_id", "BIGINT"),
    ("cashier_name", "VARCHAR"),
    ("user_employee_number", "VARCHAR"),
    ("dealer_employee_number", "VARCHAR"),
    ("supervisor_employee_number", "VARCHAR"),
    ("cashier_employee_number", "VARCHAR"),
    ("approver_employee_number", "VARCHAR"),
    ("all_valid_chipsets", "UTINYINT"),
    ("above_threshold", "UTINYINT"),
    ("group_code", "VARCHAR"),
    ("rep_code", "VARCHAR"),
    ("program_id", "INTEGER"),
    ("mixed_stack", "UTINYINT"),
    ("currency_label", "VARCHAR"),
    ("cv_face_rec_id", "BIGINT"),
    ("cv_id", "VARCHAR"),
    ("manual", "VARCHAR"),
    ("__ts_ms", "BIGINT"),
    ("__op", "VARCHAR"),
    ("__deleted", "VARCHAR"),
    ("__etl_insert_Dtm", "TIMESTAMPTZ"),
)

TXN_L0_DERIVED_COLUMN_TYPES: Final[tuple[tuple[str, str], ...]] = (
    ("txn_event_ts", "TIMESTAMPTZ"),
    ("txn_observed_at_raw", "TIMESTAMPTZ"),
    ("txn_available_ts", "TIMESTAMPTZ"),
    ("observed_at_correction_rule_id", "VARCHAR"),
    ("is_suspicious_non_positive_txn_value", "BOOLEAN"),
    ("is_suspicious_observed_before_event_raw", "BOOLEAN"),
)


def default_schema_txt_path() -> Path:
    """Bundled ClickHouse DDL file containing ``t_casino_txn``."""

    return (_REPO_ROOT / "schema" / "schema.txt").resolve()


def _normalize_clickhouse_type(ch_type: str) -> str:
    """Collapse whitespace in a ClickHouse type literal."""

    return re.sub(r"\s+", " ", str(ch_type).strip())


def clickhouse_ddl_type_to_duckdb_l0(ch_type: str) -> str:
    """Map one ClickHouse DDL type to the L0 DuckDB cast target."""

    normalized = _normalize_clickhouse_type(ch_type)
    if normalized.startswith("Nullable(") and normalized.endswith(")"):
        return clickhouse_ddl_type_to_duckdb_l0(normalized[len("Nullable(") : -1])
    if normalized.startswith("DateTime64"):
        return "TIMESTAMPTZ"
    if normalized.startswith("DateTime"):
        return "TIMESTAMPTZ"
    if normalized.startswith("Int64"):
        return "BIGINT"
    if normalized.startswith("Int32"):
        return "INTEGER"
    if normalized == "String":
        return "VARCHAR"
    if normalized == "Date32":
        return "DATE"
    if normalized.startswith("Decimal(19"):
        return "DECIMAL(19,4)"
    if normalized.startswith("UInt8"):
        return "UTINYINT"
    raise ValueError(
        f"unsupported ClickHouse DDL type {ch_type!r}; expected mapping in "
        f"{TXN_L0_SCHEMA_DDL_REF}",
    )


def _extract_ddl_column_block(schema_text: str) -> str:
    """Return the parenthesized column block for ``t_casino_txn``."""

    start = schema_text.index(_TXN_DDL_TABLE_MARKER)
    end = schema_text.index(_TXN_DDL_ENGINE_MARKER, start)
    create_stmt = schema_text[start:end]
    open_idx = create_stmt.index("(")
    close_idx = create_stmt.rindex(")")
    return create_stmt[open_idx + 1 : close_idx]


def _scan_ddl_column_entries(column_block: str) -> list[tuple[str, str]]:
    """Parse ``(`name` type, ...)`` entries from a normalized DDL block."""

    text = _normalize_clickhouse_type(column_block)
    entries: list[tuple[str, str]] = []
    idx = 0
    while idx < len(text):
        if text[idx] != "`":
            idx += 1
            continue
        idx += 1
        name_end = text.index("`", idx)
        name = text[idx:name_end]
        idx = name_end + 1
        while idx < len(text) and text[idx].isspace():
            idx += 1
        type_start = idx
        depth = 0
        while idx < len(text):
            ch = text[idx]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                break
            idx += 1
        ch_type = text[type_start:idx].strip()
        entries.append((name, ch_type))
        if idx < len(text) and text[idx] == ",":
            idx += 1
    return entries


def parse_t_casino_txn_ddl_columns(schema_txt_path: Path | None = None) -> tuple[tuple[str, str], ...]:
    """Parse ``GDP_GMWDS_Raw.t_casino_txn`` column names and ClickHouse types from DDL."""

    path = Path(schema_txt_path or default_schema_txt_path()).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"schema DDL file not found: {path}")
    block = _extract_ddl_column_block(path.read_text(encoding="utf-8"))
    return tuple(_scan_ddl_column_entries(block))


def assert_txn_l0_schema_matches_ddl(schema_txt_path: Path | None = None) -> None:
    """Validate L0 raw schema name order and DuckDB casts against ClickHouse DDL."""

    ddl_cols = parse_t_casino_txn_ddl_columns(schema_txt_path)
    l0_cols = TXN_L0_RAW_COLUMN_TYPES
    if len(ddl_cols) != len(l0_cols):
        raise AssertionError(
            f"column count mismatch: ddl={len(ddl_cols)} l0={len(l0_cols)}",
        )
    mismatches: list[str] = []
    for (ddl_name, ddl_type), (l0_name, l0_type) in zip(ddl_cols, l0_cols, strict=True):
        if ddl_name != l0_name:
            mismatches.append(f"name {ddl_name!r} != {l0_name!r}")
            continue
        expected = clickhouse_ddl_type_to_duckdb_l0(ddl_type)
        if expected != l0_type:
            mismatches.append(
                f"{ddl_name}: ddl={ddl_type!r} maps to {expected!r}, l0={l0_type!r}",
            )
    if mismatches:
        raise AssertionError("t_casino_txn schema drift vs DDL:\n" + "\n".join(mismatches))


def _ddl_type_and_default(ch_type: str) -> tuple[str, str]:
    """Split ClickHouse DDL type string into type-only and DEFAULT clause."""

    normalized = _normalize_clickhouse_type(ch_type)
    default_match = re.search(r"\bDEFAULT\s+(.+)$", normalized)
    default = default_match.group(1).strip() if default_match else ""
    type_only = re.sub(r"\s+DEFAULT\s+.+$", "", normalized)
    return type_only, default


def parse_dictionary_section5_ddl_columns(
    dictionary_path: Path | None = None,
) -> tuple[tuple[str, str, str, str, str], ...]:
    """Parse §5 raw-column DDL metadata from the schema dictionary markdown table."""

    path = Path(dictionary_path or (_REPO_ROOT / "schema" / "GDP_GMWDS_Raw_Schema_Dictionary.md"))
    text = path.read_text(encoding="utf-8")
    start = text.index("## 5. t_casino_txn")
    derived = text.index("### 5.1 L0 derived", start)
    block = text[start:derived]
    rows: list[tuple[str, str, str, str, str]] = []
    for line in block.splitlines():
        if not line.startswith("| `"):
            continue
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        if len(parts) < 5:
            continue
        name = parts[0].strip("`")
        ch_type = parts[1].strip("`")
        nullable = parts[2]
        default = parts[3]
        l0_cast = parts[4].strip("`")
        rows.append((name, ch_type, nullable, default, l0_cast))
    if not rows:
        raise ValueError(f"no §5 DDL rows parsed from dictionary: {path}")
    return tuple(rows)


def assert_dictionary_section5_matches_ddl(
    *,
    dictionary_path: Path | None = None,
    schema_txt_path: Path | None = None,
) -> None:
    """Validate dictionary §5 ClickHouse types/nullable/default against schema.txt DDL."""

    ddl_cols = parse_t_casino_txn_ddl_columns(schema_txt_path)
    dict_cols = parse_dictionary_section5_ddl_columns(dictionary_path)
    if len(ddl_cols) != len(dict_cols):
        raise AssertionError(
            f"dictionary §5 row count {len(dict_cols)} != ddl {len(ddl_cols)}",
        )
    mismatches: list[str] = []
    for (ddl_name, ddl_type), (dict_name, dict_type, dict_null, dict_default, dict_l0) in zip(
        ddl_cols,
        dict_cols,
        strict=True,
    ):
        type_only, default = _ddl_type_and_default(ddl_type)
        expected_null = "Y" if _normalize_clickhouse_type(ddl_type).startswith("Nullable(") else "N"
        expected_l0 = clickhouse_ddl_type_to_duckdb_l0(ddl_type)
        if ddl_name != dict_name:
            mismatches.append(f"name {ddl_name!r} != dictionary {dict_name!r}")
        if type_only != dict_type:
            mismatches.append(f"{ddl_name}: ddl type {type_only!r} != dictionary {dict_type!r}")
        if expected_null != dict_null:
            mismatches.append(
                f"{ddl_name}: nullable {expected_null!r} != dictionary {dict_null!r}",
            )
        if default != dict_default:
            mismatches.append(
                f"{ddl_name}: default {default!r} != dictionary {dict_default!r}",
            )
        if expected_l0 != dict_l0:
            mismatches.append(
                f"{ddl_name}: l0 cast {expected_l0!r} != dictionary {dict_l0!r}",
            )
    if mismatches:
        raise AssertionError("dictionary §5 drift vs DDL:\n" + "\n".join(mismatches))


def _quote_ident(name: str) -> str:
    """Return a double-quoted SQL identifier."""

    return '"' + str(name).replace('"', '""') + '"'


def duckdb_canonical_cast(alias: str, column: str, duckdb_type: str) -> str:
    """Build ``TRY_CAST`` / ``CAST`` expression for one canonical column."""

    col = _quote_ident(column)
    src = f"{alias}.{col}"
    if duckdb_type == "VARCHAR":
        return f"CAST(TRY_CAST({src} AS VARCHAR) AS VARCHAR) AS {col}"
    return f"TRY_CAST({src} AS {duckdb_type}) AS {col}"


def canonical_raw_select_list(alias: str = "d") -> str:
    """Comma-separated SELECT list casting raw columns to dictionary types."""

    parts = [duckdb_canonical_cast(alias, col, typ) for col, typ in TXN_L0_RAW_COLUMN_TYPES]
    return ",\n    ".join(parts)


def txn_l0_schema_fingerprint_sha256_hex() -> str:
    """Stable hash over raw + derived canonical type contract."""

    payload = {
        "schema_contract_id": TXN_L0_SCHEMA_CONTRACT_ID,
        "dictionary_ref": TXN_L0_SCHEMA_DICTIONARY_REF,
        "raw_columns": list(TXN_L0_RAW_COLUMN_TYPES),
        "derived_columns": list(TXN_L0_DERIVED_COLUMN_TYPES),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
