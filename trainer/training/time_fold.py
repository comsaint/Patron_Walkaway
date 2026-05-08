"""trainer/time_fold.py
====================
Centralised time-window definitions for the Patron Walkaway pipeline.

All ETL queries, feature cutoffs, label computation, and model
cross-validation must obtain their time boundaries exclusively from
this module (SSOT §4.3).  Calculating boundaries elsewhere risks
off-by-one errors or leakage.

Boundary contract
-----------------
  core window   : [window_start, window_end)
      Observations whose payout_complete_dtm falls here are eligible
      for training / validation / test.

  extended zone : [window_end, extended_end)
      Pull this extra data from ClickHouse (or Parquet) solely to
      observe future bets for C1 label computation.
      ** Never include these observations in the training set. **

GitHub #16 (L2 主路徑)
----------------------
訓練主路徑僅使用 ``get_single_window_chunk``（單一 run/trip 視窗；無 monthly chunk 入口）。
``get_monthly_chunks`` 仍保留於本模組供舊測試／輔助腳本使用，**不再**由 trainer CLI 觸發。
列級 train/valid/test 仍由 Step 7 完成；L2 manifest 契約見 ``l2_trainer_contracts``。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List

# R2: support both `import time_fold` (run from trainer/) and
# `import trainer.time_fold` (run from project root / tests/).
try:
    from config import (  # type: ignore[import]
        LABEL_LOOKAHEAD_MIN,
        TRAIN_SPLIT_FRAC,
        VALID_SPLIT_FRAC,
    )
except ModuleNotFoundError:
    from trainer.config import (  # type: ignore[import]
        LABEL_LOOKAHEAD_MIN,
        TRAIN_SPLIT_FRAC,
        VALID_SPLIT_FRAC,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _month_start(dt: datetime) -> datetime:
    """Return midnight on the 1st of dt's month, preserving tzinfo."""
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start(dt: datetime) -> datetime:
    """Return midnight on the 1st of the month after dt (stdlib only)."""
    if dt.month == 12:
        return dt.replace(
            year=dt.year + 1, month=1, day=1,
            hour=0, minute=0, second=0, microsecond=0,
        )
    return dt.replace(
        month=dt.month + 1, day=1,
        hour=0, minute=0, second=0, microsecond=0,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_monthly_chunks(start: datetime, end: datetime) -> List[Dict]:
    """Partition [start, end) into month-aligned chunks.

    Each returned dict contains:

    ``window_start``  – inclusive start of the core training window
    ``window_end``    – exclusive end of the core window
                        (= start of next calendar month, capped at ``end``)
    ``extended_end``  – end of the C1 label look-ahead zone
                        = ``window_end`` + max(LABEL_LOOKAHEAD_MIN, 1 day)

    The extended zone provides the extra context needed to decide whether a
    terminal bet is a censored observation (H1).  Samples from this zone
    must **never** enter the training set.

    Parameters
    ----------
    start : datetime
        Inclusive start of the full training / evaluation period.
    end : datetime
        Exclusive end of the full training / evaluation period.

    Returns
    -------
    List of chunk dicts, ordered chronologically.  Empty list if
    ``start >= end``.

    Raises
    ------
    ValueError
        If exactly one of ``start`` / ``end`` is tz-aware (R4).
    """
    # R4: guard against mixed tz-aware / tz-naive — the comparison `start >= end`
    # would otherwise raise a cryptic TypeError.
    if (start.tzinfo is None) != (end.tzinfo is None):
        raise ValueError(
            "start and end must both be tz-aware or both be tz-naive; "
            f"got start.tzinfo={start.tzinfo!r}, end.tzinfo={end.tzinfo!r}"
        )

    if start >= end:
        return []

    chunks: List[Dict] = []

    # The first chunk begins at `start` (which may be mid-month).
    # Subsequent chunks start at calendar-month boundaries.
    window_start = start
    current_month = _month_start(start)

    while window_start < end:
        next_ms = _next_month_start(current_month)
        window_end = min(next_ms, end)

        # C1 extended pull: at least X+Y minutes, recommended 1 day (SSOT §7.2)
        label_lookahead = timedelta(minutes=LABEL_LOOKAHEAD_MIN)
        one_day = timedelta(days=1)
        extended_end = window_end + max(label_lookahead, one_day)

        chunks.append(
            {
                "window_start": window_start,
                "window_end": window_end,
                "extended_end": extended_end,
            }
        )

        window_start = window_end
        current_month = next_ms

    return chunks


def get_single_window_chunk(start: datetime, end: datetime) -> List[Dict]:
    """Return a single chunk covering ``[start, end)`` (no month partitioning).

    Uses the same ``extended_end`` rule as :func:`get_monthly_chunks` for C1
    label lookahead. When ``start >= end``, returns an empty list.

    Parameters
    ----------
    start, end
        Same contract as :func:`get_monthly_chunks`.

    Returns
    -------
    list
        Either one dict ``{window_start, window_end, extended_end}`` or ``[]``.
    """
    if (start.tzinfo is None) != (end.tzinfo is None):
        raise ValueError(
            "start and end must both be tz-aware or both be tz-naive; "
            f"got start.tzinfo={start.tzinfo!r}, end.tzinfo={end.tzinfo!r}"
        )
    if start >= end:
        return []
    label_lookahead = timedelta(minutes=LABEL_LOOKAHEAD_MIN)
    one_day = timedelta(days=1)
    extended_end = end + max(label_lookahead, one_day)
    return [
        {
            "window_start": start,
            "window_end": end,
            "extended_end": extended_end,
        }
    ]


def partition_windows_for_train_end_cutoff(
    windows: List[Dict],
    train_frac: float = TRAIN_SPLIT_FRAC,
    valid_frac: float = VALID_SPLIT_FRAC,
) -> Dict[str, List[Dict]]:
    """Partition an ordered list of time-window dicts for **train_end cutoff** only.

    Used after ``get_single_window_chunk`` / ``get_monthly_chunks``: assigns each
    window dict to a bucket by **count** (not row volume). The trainer uses the
    ``train`` bucket's latest ``window_end`` as ``train_end`` for canonical mapping
    (B1); **row-level** train/valid/test is computed later in Step 7.

    When there are too few windows to honour the requested fractions while
    keeping at least one window per non-empty bucket, the function degrades:

    * n >= 3  → each bucket gets at least 1 window (R3)
    * n == 2  → train 1, valid 1, test 0
    * n == 1  → train 1, valid 0, test 0

    Parameters
    ----------
    windows
        Ordered list of ``{window_start, window_end, extended_end}`` dicts.
    train_frac, valid_frac
        Fraction of **windows** for train / valid buckets (defaults from config).

    Returns
    -------
    dict
        Keys ``train_windows``, ``valid_windows``, ``test_windows`` — each a list
        of the same dict objects passed in *windows*.

    Raises
    ------
    ValueError
        If fractions are out of range or their sum >= 1 (R5).
    """
    # R5: validate fractions eagerly so callers get a clear error, not silent
    # empty splits or negative n_test.
    if not (0 < train_frac < 1 and 0 < valid_frac < 1 and train_frac + valid_frac < 1):
        raise ValueError(
            f"Invalid fractions: train_frac={train_frac}, valid_frac={valid_frac}. "
            "Both must be in (0, 1) and train_frac + valid_frac must be < 1."
        )

    empty: Dict[str, List[Dict]] = {
        "train_windows": [],
        "valid_windows": [],
        "test_windows": [],
    }
    n = len(windows)
    if n == 0:
        return empty

    if n == 1:
        return {"train_windows": windows[:1], "valid_windows": [], "test_windows": []}

    if n == 2:
        return {"train_windows": windows[:1], "valid_windows": windows[1:], "test_windows": []}

    # n >= 3: honour fractions while guaranteeing >=1 window per bucket (R3).
    n_train = max(1, round(n * train_frac))
    n_valid = max(1, round(n * valid_frac))
    n_test = n - n_train - n_valid

    while n_test < 1 and n_train > 1:
        n_train -= 1
        n_test += 1

    return {
        "train_windows": windows[:n_train],
        "valid_windows": windows[n_train : n_train + n_valid],
        "test_windows": windows[n_train + n_valid :],
    }
