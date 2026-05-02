"""Snapshot-scoped deterministic ``run_id`` (implementation plan §4.1 + SSOT §6)."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

_RUN_ID_BODY_HEX = 32


def _coerce_int_player_or_bet(value: Any) -> int:
    """Coerce DuckDB / pandas numeric types to ``int`` for hashing."""
    if isinstance(value, bool):
        raise TypeError(f"unexpected bool for numeric id: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, float):
        return int(value)
    return int(str(value))


def run_start_ts_canonical(value: Any) -> str:
    """Normalize event timestamp to a stable ISO-like string for ``run_id`` hashing.

    Naïve datetimes are formatted without offset; matches typical ``t_bet`` exports.
    """
    if value is None:
        raise ValueError("run_start_ts must not be None")
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz=None).replace(tzinfo=None)
        # Always include microseconds (6 digits) so hashes match DuckDB ``strftime(..., '%f')``.
        return dt.isoformat(sep="T", timespec="microseconds")
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day).isoformat(sep="T", timespec="microseconds")
    # DuckDB may pass pandas.Timestamp
    ts = getattr(value, "to_pydatetime", lambda: value)()
    if isinstance(ts, datetime):
        return run_start_ts_canonical(ts)
    raise TypeError(f"unsupported run_start_ts type: {type(value).__name__}")


def derive_run_id(
    *,
    player_id: Any,
    run_start_ts: Any,
    first_bet_id: Any,
    run_definition_version: str,
    source_namespace: str,
) -> str:
    """Return ``run_<32hex>`` from SHA-256 of canonical JSON (§4.1 includes ``first_bet_id``)."""
    payload = {
        "first_bet_id": str(_coerce_int_player_or_bet(first_bet_id)),
        "player_id": _coerce_int_player_or_bet(player_id),
        "run_definition_version": str(run_definition_version).strip(),
        "run_start_ts": run_start_ts_canonical(run_start_ts),
        "source_namespace": str(source_namespace).strip(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_RUN_ID_BODY_HEX]
    return f"run_{digest}"
