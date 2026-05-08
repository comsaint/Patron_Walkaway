"""Trip-layer materializer: LDA bridge columns on bet rows (WS4).

Materializes optional ``lda_*`` passthrough columns into ``float32`` for a
stable on-disk / cache contract. Trip semantics still originate from the
bridge Parquet; this module does not recompute run/trip aggregates.
"""

from __future__ import annotations

import logging
from typing import Set

import numpy as np
import pandas as pd

from trainer.features.features import _LDA_OPTIONAL_PASSTHROUGH

logger = logging.getLogger(__name__)


def expected_lda_trip_column_names() -> Set[str]:
    """Column names that may participate in trip-level passthrough today."""
    return set(_LDA_OPTIONAL_PASSTHROUGH)


def materialize_trip_layer_features(
    bets_df: pd.DataFrame,
    *,
    fail_closed: bool = False,
) -> pd.DataFrame:
    """Ensure LDA trip passthrough columns exist with ``float32`` dtype.

    Parameters
    ----------
    bets_df:
        Bet-level frame (possibly with ``lda_*`` passthrough columns).
    fail_closed:
        When True, raise ``ValueError`` if any expected ``lda_*`` column is missing.
        When False, missing columns are created as 0.0 (legacy lenient behavior).
    """
    expected = expected_lda_trip_column_names()
    missing = sorted(c for c in expected if c not in bets_df.columns)
    if missing:
        if fail_closed:
            raise ValueError(
                "trip_materializer fail_closed: missing lda passthrough columns: "
                f"{missing}"
            )
        logger.debug(
            "trip_materializer: lda passthrough columns absent (optional), zero-filling: %s",
            missing,
        )
    for col in expected:
        if col not in bets_df.columns:
            bets_df[col] = np.float32(0.0)
        else:
            bets_df[col] = (
                pd.to_numeric(bets_df[col], errors="coerce").fillna(0.0).astype("float32")
            )
    return bets_df
