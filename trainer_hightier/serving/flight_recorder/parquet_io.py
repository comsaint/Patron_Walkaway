"""Schema-aware Parquet serialization for flight recorder artifacts.

Column typing follows ``schema/GDP_GMWDS_Raw_Schema_Dictionary.md`` for raw
``t_bet`` / ``t_session`` / ``t_game`` captures and defensive coercion for
derived scorer/validator frames (mixed numeric + categorical features).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# t_bet (GDP_GMWDS_Raw_Schema_Dictionary §4)
_TBET_STRING_COLS: frozenset[str] = frozenset(
    {
        "bet_type",
        "type_of_bet",
        "bet_uuid",
        "status",
        "position_code",
        "position_label",
        "chips_paid",
        "chips_wagered",
        "chipsvalue_by_chipset",
        "chipset_label",
        "chips_tip",
        "bet_cards",
        "short_bet_name_en",
        "short_bet_name_zh",
        "bet_payout_type",
        "__op",
        "__deleted",
    }
)
_TBET_INT64_COLS: frozenset[str] = frozenset(
    {"bet_id", "game_id", "session_id", "player_id", "__ts_ms"}
)
_TBET_INT32_COLS: frozenset[str] = frozenset(
    {
        "is_back_bet",
        "table_id",
        "position_idx",
        "is_settled",
        "is_lump_sum_payout",
        "mixed_stack",
        "auto_resolve_stack",
        "bonus_game_offered",
        "is_jackpot",
    }
)
_TBET_DECIMAL_COLS: frozenset[str] = frozenset(
    {
        "base_ha",
        "bonus",
        "casino_loss_from_nn",
        "casino_win",
        "commission",
        "max_wager",
        "payout_ha",
        "payout_odds",
        "std_dev",
        "theo_win",
        "theo_win_cash",
        "true_odds",
        "wager",
        "wager_nn",
        "tip_amount",
        "increment_wager",
        "bet_cards_sum",
        "adjusted_theo_win",
        "payout_value",
    }
)
_TBET_TIMESTAMP_COLS: frozenset[str] = frozenset(
    {"payout_complete_dtm", "__etl_insert_Dtm", "bet_reconciled_at"}
)

# t_session (§3) — columns we may capture in future time-machine extracts
_TSESSION_STRING_COLS: frozenset[str] = frozenset(
    {
        "casino_player_id",
        "clockin_event_id",
        "clockout_event_id",
        "casino_open_rating_id",
        "casino_close_rating_id",
        "position_label",
        "clockin_event_username",
        "irc_number",
        "player_name",
        "table_name",
        "status",
        "rating_status",
        "verified_status",
        "game_type",
        "game_variant",
        "group_code",
        "rep_code",
        "seat_label",
        "chipset_labels",
        "shoe_id",
        "pit_name",
        "gaming_area",
        "walk_in",
        "walk_with",
        "color_hsl_code",
        "verification_info",
        "updated_position_label",
        "__op",
        "__deleted",
    }
)

# Validator / alert derived columns (not raw CH tables)
_TRACE_STRING_COLS: frozenset[str] = frozenset(
    {
        "bet_id",
        "canonical_id",
        "casino_player_id",
        "model_version",
        "reason",
        "gap_start",
        "validated_at",
        "alert_ts",
        "bet_ts",
        "feature_id",
        "feature_value",
        "source_layer",
        "null_reason",
        "cycle",
    }
)
_TRACE_BOOL_COLS: frozenset[str] = frozenset(
    {
        "result",
        "is_null",
        "model_features_missing",
        "feast_mid_missing",
        "feast_slow_missing",
        "short_term_missing",
    }
)


def _serialize_cell(val: Any) -> str | None:
    """Serialize one scalar to a nullable string."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    if pd.isna(val):
        return None
    if isinstance(val, (bool, np.bool_)):
        return str(bool(val))
    if isinstance(val, (int, np.integer)) and not isinstance(val, (bool, np.bool_)):
        return str(int(val))
    if isinstance(val, (float, np.floating)):
        return str(float(val))
    return str(val)


def _coerce_object_series(series: pd.Series) -> pd.Series:
    """Coerce object column to a single Parquet-safe dtype."""
    non_null = series.dropna()
    if non_null.empty:
        return series.astype("string")
    numeric = pd.to_numeric(non_null, errors="coerce")
    if numeric.notna().all():
        return pd.to_numeric(series, errors="coerce").astype("float64")
    return series.map(_serialize_cell).astype("string")


def _coerce_series(name: str, series: pd.Series) -> pd.Series:
    """Coerce one column using schema hints and defensive fallbacks."""
    if name in _TRACE_BOOL_COLS:
        return series.astype("boolean")
    if name in _TRACE_STRING_COLS or name in _TBET_STRING_COLS or name in _TSESSION_STRING_COLS:
        if pd.api.types.is_object_dtype(series) or pd.api.types.is_bool_dtype(series):
            return series.map(_serialize_cell).astype("string")
        return series.astype("string")
    if name in _TBET_INT64_COLS or name in {"player_id"}:
        return pd.to_numeric(series, errors="coerce").astype("Int64")
    if name in _TBET_INT32_COLS:
        return pd.to_numeric(series, errors="coerce").astype("Int32")
    if name in _TBET_DECIMAL_COLS or name in {"score", "gap_minutes", "margin"}:
        return pd.to_numeric(series, errors="coerce").astype("float64")
    if name in _TBET_TIMESTAMP_COLS:
        return pd.to_datetime(series, errors="coerce", utc=True)
    if name == "gaming_day":
        return pd.to_datetime(series, errors="coerce").dt.date.astype("string")
    if isinstance(series.dtype, pd.CategoricalDtype):
        return series.astype("string")
    if pd.api.types.is_object_dtype(series):
        return _coerce_object_series(series)
    return series


def prepare_dataframe_for_parquet(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with Parquet-safe dtypes (schema-aware + defensive)."""
    if frame.empty:
        return frame.copy()
    out = frame.copy()
    for col in out.columns:
        out[col] = _coerce_series(str(col), out[col])
    return out


def write_parquet_safe(path: Path, frame: pd.DataFrame) -> None:
    """Write *frame* to Parquet after schema-aware dtype coercion."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if frame.empty:
        pd.DataFrame().to_parquet(path, index=False)
        return
    safe = prepare_dataframe_for_parquet(frame)
    safe.to_parquet(path, index=False)
