#!/usr/bin/env python3
"""Validate schema/time_semantics_registry.yaml structure and column refs vs schema dict.

Run from repo root::

    python scripts/validate_time_semantics_registry.py

Exit code 0 on success; non-zero with stderr messages on failure.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    print("PyYAML is required (project dependency: pyyaml).", file=sys.stderr)
    raise SystemExit(2) from exc

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REGISTRY_PATH = _REPO_ROOT / "schema" / "time_semantics_registry.yaml"
_DICTIONARY_PATH = _REPO_ROOT / "schema" / "GDP_GMWDS_Raw_Schema_Dictionary.md"

_TOP_LEVEL_KEYS = (
    "registry_version",
    "owner",
    "default_timezone",
    "default_observed_at_col",
    "tables",
)

_TABLE_KEYS = (
    "description",
    "business_key",
    "event_time_col",
    "observed_at_col",
    "update_time_col",
    "partition_cols",
    "dedup_rule_id",
    "preprocessing_contract",
    "correction_expected",
    "late_arrival_expected",
    "expected_delay_profile",
    "late_threshold",
    "notes",
)

_SQLISH_SKIP = frozenset(
    {
        "COALESCE",
        "NULL",
        "CASE",
        "WHEN",
        "THEN",
        "ELSE",
        "END",
        "AND",
        "OR",
        "NOT",
        "CAST",
        "AS",
        "OVER",
        "PARTITION",
        "BY",
        "DESC",
        "ASC",
        "TRUE",
        "FALSE",
        "INTERVAL",
    }
)

_SECTION_RE = re.compile(r"^##\s+\d+\.\s+(t_\w+)\b", re.MULTILINE)
_FIELD_RE = re.compile(r"\|\s*`([^`]+)`\s*\|")


def _parse_args() -> argparse.Namespace:
    """Build CLI for registry path, dictionary path, and optional column-skip flag."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--registry",
        type=Path,
        default=_REGISTRY_PATH,
        help="Path to time_semantics_registry.yaml",
    )
    p.add_argument(
        "--dictionary",
        type=Path,
        default=_DICTIONARY_PATH,
        help="Path to GDP_GMWDS_Raw_Schema_Dictionary.md",
    )
    p.add_argument(
        "--no-dictionary-columns",
        action="store_true",
        help="Skip column presence checks against the schema dictionary.",
    )
    return p.parse_args()


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load YAML from ``path``; require a mapping root."""
    if not path.is_file():
        raise FileNotFoundError(f"Registry file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise TypeError(f"Registry root must be a mapping, got {type(data).__name__}")
    return data


def _parse_dictionary_columns(md_path: Path) -> dict[str, set[str]]:
    """Return table name -> set of column names from markdown pipe tables."""
    if not md_path.is_file():
        raise FileNotFoundError(f"Schema dictionary not found: {md_path}")
    text = md_path.read_text(encoding="utf-8")
    matches = list(_SECTION_RE.finditer(text))
    out: dict[str, set[str]] = {}
    for i, m in enumerate(matches):
        table = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end]
        cols: set[str] = set()
        for fm in _FIELD_RE.finditer(chunk):
            name = fm.group(1).strip()
            if name and not name.startswith("---") and name != "欄位名稱":
                cols.add(name)
        out[table] = cols
    return out


def _identifiers_in_sqlish(expr: str) -> list[str]:
    """Extract bare identifiers from a small SQL-ish registry column expression."""
    cleaned = re.sub(r"'[^']*'", " ", expr)
    tokens: list[str] = []
    for m in re.finditer(r"\b[A-Za-z_][\w]*\b", cleaned):
        w = m.group(0)
        if w.upper() in _SQLISH_SKIP:
            continue
        tokens.append(w)
    return tokens


def _bool_or_tbd(value: Any, field: str, table: str) -> None:
    """Require ``value`` to be bool or the literal string ``TBD`` for ``table.field``."""
    if isinstance(value, bool):
        return
    if value == "TBD":
        return
    raise TypeError(
        f"{table}: {field} must be bool or the string 'TBD', got {value!r} ({type(value).__name__})"
    )


def _validate_table_spec_shape(table: str, spec: Any) -> Mapping[str, Any]:
    """Ensure ``tables.<table>`` is a mapping with exactly the expected keys."""
    if not isinstance(spec, Mapping):
        raise TypeError(f"tables.{table} must be a mapping, got {type(spec).__name__}")
    missing = [k for k in _TABLE_KEYS if k not in spec]
    if missing:
        raise KeyError(f"{table}: missing keys: {missing}")
    extra = [k for k in spec if k not in _TABLE_KEYS]
    if extra:
        raise KeyError(f"{table}: unknown keys (typo?): {extra}")
    return spec


def _validate_table_entry_core_fields(table: str, spec: Mapping[str, Any]) -> None:
    """Validate description, keys, time columns, partition list, and dedup/preprocess fields."""
    desc = spec["description"]
    if not isinstance(desc, str) or not desc.strip():
        raise ValueError(f"{table}: description must be a non-empty string")

    bk = spec["business_key"]
    if not isinstance(bk, list) or not bk or not all(isinstance(x, str) and x for x in bk):
        raise ValueError(f"{table}: business_key must be a non-empty list of non-empty strings")

    for col_key in ("event_time_col", "observed_at_col"):
        v = spec[col_key]
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{table}: {col_key} must be a non-empty string")

    utc = spec["update_time_col"]
    if utc is not None and (not isinstance(utc, str) or not utc.strip()):
        raise ValueError(f"{table}: update_time_col must be null or a non-empty string")

    pc = spec["partition_cols"]
    if not isinstance(pc, list) or not all(isinstance(x, str) and x for x in pc):
        raise ValueError(f"{table}: partition_cols must be a list of non-empty strings")

    dr = spec["dedup_rule_id"]
    if not isinstance(dr, str) or not dr.strip():
        raise ValueError(f"{table}: dedup_rule_id must be a non-empty string")

    pcon = spec["preprocessing_contract"]
    if not isinstance(pcon, list) or not pcon or not all(isinstance(x, str) and x.strip() for x in pcon):
        raise ValueError(f"{table}: preprocessing_contract must be a non-empty list of strings")


def _validate_table_entry_flags_and_metadata(table: str, spec: Mapping[str, Any]) -> None:
    """Validate bool/TBD flags, delay profile, late threshold string, and notes list."""
    _bool_or_tbd(spec["correction_expected"], "correction_expected", table)
    _bool_or_tbd(spec["late_arrival_expected"], "late_arrival_expected", table)

    edp = spec["expected_delay_profile"]
    if not isinstance(edp, str) or not edp.strip():
        raise ValueError(f"{table}: expected_delay_profile must be a non-empty string")

    lt = spec["late_threshold"]
    if not isinstance(lt, str) or not str(lt).strip():
        raise ValueError(f"{table}: late_threshold must be a string (use 'TBD' if unknown)")

    notes = spec["notes"]
    if not isinstance(notes, list) or not notes or not all(isinstance(x, str) and x.strip() for x in notes):
        raise ValueError(f"{table}: notes must be a non-empty list of non-empty strings")


def _validate_table_entry(table: str, spec: Any) -> None:
    """Validate structure and field types for one registry ``tables`` entry."""
    m = _validate_table_spec_shape(table, spec)
    _validate_table_entry_core_fields(table, m)
    _validate_table_entry_flags_and_metadata(table, m)


def _validate_columns_against_dict(
    tables: Mapping[str, Any],
    dict_cols: Mapping[str, set[str]],
    dictionary_path: Path,
) -> None:
    """Ensure registry column refs for each table exist in the markdown schema dictionary."""
    for table, spec in tables.items():
        if table not in dict_cols:
            raise KeyError(
                f"Registry table {table!r} has no matching section in schema dictionary "
                f"(expected heading like '## N. {table}' in {dictionary_path})"
            )
        allowed = dict_cols[table]
        cols_to_check: list[str] = []
        cols_to_check.extend(spec["business_key"])
        cols_to_check.extend(_identifiers_in_sqlish(spec["event_time_col"]))
        cols_to_check.extend(_identifiers_in_sqlish(spec["observed_at_col"]))
        if spec["update_time_col"]:
            cols_to_check.extend(_identifiers_in_sqlish(spec["update_time_col"]))
        cols_to_check.extend(spec["partition_cols"])
        missing = [c for c in cols_to_check if c not in allowed]
        if missing:
            raise ValueError(
                f"{table}: column(s) not listed in {dictionary_path} for this table: {missing}. "
                "Update the dictionary first, then fix the registry."
            )


def validate_registry(
    registry_path: Path,
    dictionary_path: Path,
    *,
    check_dictionary_columns: bool,
) -> None:
    """Validate ``time_semantics_registry`` shape, table entries, and optional dict columns."""
    data = _load_yaml(registry_path)
    missing_root = [k for k in _TOP_LEVEL_KEYS if k not in data]
    if missing_root:
        raise KeyError(f"Registry missing top-level keys: {missing_root}")

    tables = data["tables"]
    if not isinstance(tables, Mapping) or not tables:
        raise ValueError("tables must be a non-empty mapping")

    for name, spec in tables.items():
        if not isinstance(name, str) or not name.startswith("t_"):
            raise ValueError(f"Invalid table key: {name!r} (expected t_*)")
        _validate_table_entry(name, spec)

    if check_dictionary_columns:
        dict_cols = _parse_dictionary_columns(dictionary_path)
        _validate_columns_against_dict(tables, dict_cols, dictionary_path)


def main() -> int:
    """CLI entry: validate registry; return 0 on success, 1 with stderr message on failure."""
    args = _parse_args()
    try:
        validate_registry(
            args.registry.resolve(),
            args.dictionary.resolve(),
            check_dictionary_columns=not args.no_dictionary_columns,
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError, yaml.YAMLError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print("time_semantics_registry validation OK:", args.registry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
