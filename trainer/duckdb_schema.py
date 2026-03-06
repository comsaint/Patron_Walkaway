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
    """Return a copy of bets_df with monetary columns as float64 for DuckDB.

    Use this before duckdb.register("bets", df) or before passing the frame
    to DuckDB so that window expressions (SUM(wager), AVG(payout_odds), etc.)
    are computed as DOUBLE and never cast to DECIMAL(9,4) or DECIMAL(10,4),
    which would fail for values like 100000 or -1900000.

    Aligned to schema/schema.txt: all t_bet Decimal(19,4) columns are cast
    to float64; columns missing from the DataFrame are skipped.
    """
    import pandas as pd

    out = bets_df.copy()
    for col in T_BET_DECIMAL_19_4_COLUMNS:
        if col not in out.columns:
            continue
        if out[col].dtype == object or str(out[col].dtype).startswith("decimal"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        if out[col].dtype != "float64":
            out[col] = out[col].astype("float64")
    return out
