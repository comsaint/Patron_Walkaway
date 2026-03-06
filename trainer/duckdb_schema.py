"""DuckDB schema and helpers for t_bet, aligned to schema/schema.txt.

Use this when registering a bets DataFrame with DuckDB for Track LLM (or any
feature SQL) to avoid "Casting value ... to type DECIMAL(9,4) failed: value is
out of range".  schema.txt defines all monetary columns as Decimal(19,4);
DuckDB must use DECIMAL(19,4) or DOUBLE for those columns.

- DECIMAL(9,4)  max ~99,999.9999  → fails for e.g. wager 100000
- DECIMAL(10,4) max ~999,999.9999 → fails for e.g. casino_win -1900000
- DECIMAL(19,4) or DOUBLE          → safe for source data and aggregates
"""

from __future__ import annotations

from typing import Set

# Monetary columns in t_bet per schema/schema.txt (Decimal(19,4)).
# Cast these to float64 before con.register("bets", df) so DuckDB sees DOUBLE
# and does not infer narrow DECIMAL in window expressions (SUM/AVG/etc).
T_BET_DECIMAL_19_4_COLUMNS: Set[str] = {
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


def prepare_bets_for_duckdb(bets_df):
    """Cast monetary columns of *bets_df* to float64 in-place and return it.

    Mutates the caller's DataFrame directly to avoid an extra full copy; the
    caller in ``compute_track_llm_features`` already works on a local view so
    mutation is safe.

    Use this before duckdb.register("bets", df) so that window expressions
    (SUM(wager), AVG(payout_odds), etc.) are computed as DOUBLE and never
    cast to DECIMAL(9,4) or DECIMAL(10,4), which would fail for values like
    100000 or -1900000.

    Decimal detection uses ``"decimal" in str(dtype).lower()`` to remain
    backend-agnostic (covers pyarrow Decimal128, pandas ArrowDtype, etc.).
    """
    import pandas as pd

    for col in T_BET_DECIMAL_19_4_COLUMNS:
        if col not in bets_df.columns:
            continue
        if bets_df[col].dtype == object or "decimal" in str(bets_df[col].dtype).lower():
            bets_df[col] = pd.to_numeric(bets_df[col], errors="coerce")
        if bets_df[col].dtype != "float64":
            bets_df[col] = bets_df[col].astype("float64")
    return bets_df
