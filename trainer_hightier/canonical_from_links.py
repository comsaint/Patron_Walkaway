"""Canonical player mapping from DuckDB-produced links (+ FND-12 dummies).

Parity subset of legacy ``trainer.identity`` for offline high-tier DuckDB flows.
"""

from __future__ import annotations

import logging
from typing import Set

import pandas as pd

logger = logging.getLogger(__name__)


def _clean_casino_player_id(series: pd.Series) -> pd.Series:
    """Apply FND-03: whitespace trim; '', 'null' -> NA; return trimmed string for valid rows."""

    stripped = series.astype(str).str.strip()
    mask_invalid = stripped.str.lower().isin(["", "null"])
    valid_mask = series.notna() & ~mask_invalid
    return stripped.where(valid_mask, other=pd.NA)


def _apply_mn_resolution(
    links_df: pd.DataFrame,
    dummy_player_ids: Set,
) -> pd.DataFrame:
    """Resolve M:N player_id ↔ casino_player_id edges to final mapping."""

    if links_df.empty:
        return pd.DataFrame(columns=["player_id", "canonical_id"])

    df = links_df.copy()
    df["lud_dtm"] = pd.to_datetime(df["lud_dtm"], errors="coerce")

    card_counts = df.groupby("player_id")["casino_player_id"].nunique()
    swapped = card_counts[card_counts > 1]
    if not swapped.empty:
        logger.warning(
            "D2 Case 2 (card swap): %d player_id(s) mapped to multiple "
            "casino_player_ids — keeping most recent: %s",
            len(swapped),
            swapped.index.tolist()[:20],
        )

    resolved = (
        df.sort_values("lud_dtm", ascending=False, na_position="last")
        .drop_duplicates(subset=["player_id"], keep="first")
        [["player_id", "casino_player_id"]]
        .rename(columns={"casino_player_id": "canonical_id"})
    )
    resolved["canonical_id"] = resolved["canonical_id"].astype(str)
    resolved = resolved[~resolved["player_id"].isin(dummy_player_ids)]
    return resolved.reset_index(drop=True)


def build_canonical_mapping_from_links(
    links_df: pd.DataFrame,
    dummy_pids: Set,
) -> pd.DataFrame:
    """Return ``player_id`` / ``canonical_id`` from DuckDB links + dummy id set."""

    required = {"player_id", "casino_player_id", "lud_dtm"}
    missing = required - set(links_df.columns)
    if missing:
        raise ValueError(f"links_df is missing required columns: {sorted(missing)}")
    if links_df.empty:
        return pd.DataFrame(columns=["player_id", "canonical_id"])

    rated = links_df.loc[
        links_df["casino_player_id"].notna(), ["player_id", "casino_player_id", "lud_dtm"]
    ].copy()
    rated["casino_player_id"] = _clean_casino_player_id(rated["casino_player_id"])
    rated = rated[rated["casino_player_id"].notna()]
    return _apply_mn_resolution(rated, dummy_pids)
