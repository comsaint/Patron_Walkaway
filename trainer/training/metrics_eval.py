"""trainer/training/metrics_eval.py
======================================
Pure metric-evaluation helpers extracted from
``trainer/training/trainer.py`` (Issue #12 PR-12.4).

Scope
-----
This module owns small, pure helpers that the trainer's metric pipelines
consume. Keeping them in a dedicated module gives backtester/scorer/validator
a stable import target without dragging in the heavier orchestration code.

Currently exported helpers
~~~~~~~~~~~~~~~~~~~~~~~~~~

* ``precision_prod_adjusted``: Bayes-rescaling of test precision into the
  assumed production neg/pos ratio (DEC-026 / R1300 closed form).
* ``warn_if_invalid_production_neg_pos_ratio``: single-shot validation log
  emitted when ``PRODUCTION_NEG_POS_RATIO`` is non-finite or non-positive.

Both functions are zero-side-effect pure (other than the warn helper's log
emission). The legacy underscore aliases ``_precision_prod_adjusted`` and
``_warn_if_invalid_production_neg_pos_ratio`` are exported as re-bindings so
older call sites in ``trainer.training.trainer`` keep working.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)


def precision_prod_adjusted(
    prec: Optional[float],
    *,
    production_neg_pos_ratio: Optional[float],
    test_neg_pos_ratio: Optional[float],
) -> Optional[float]:
    """Rescale raw precision for assumed production neg/pos ratio (test_precision_prod_adjusted formula).

    Returns None when inputs are missing, non-finite, out of range, or when the closed form
    would yield a non-finite or out-of-[0,1] value (JSON-safe contract).
    """
    if prec is None:
        return None
    p = float(prec)
    if not math.isfinite(p) or p <= 0.0:
        return None
    if p > 1.0 + 1e-9:
        return None
    if p > 1.0:
        p = 1.0
    if production_neg_pos_ratio is None or test_neg_pos_ratio is None:
        return None
    pn = float(production_neg_pos_ratio)
    tn = float(test_neg_pos_ratio)
    if not math.isfinite(pn) or not math.isfinite(tn) or pn <= 0.0 or tn <= 0.0:
        return None
    scaling = pn / tn
    if not math.isfinite(scaling):
        return None
    inv_p = 1.0 / p
    if not math.isfinite(inv_p):
        return None
    term = (inv_p - 1.0) * scaling
    if not math.isfinite(term):
        return None
    denom = 1.0 + term
    if not math.isfinite(denom) or denom <= 0.0:
        return None
    adj = 1.0 / denom
    if not math.isfinite(adj):
        return None
    if adj < -1e-9 or adj > 1.0 + 1e-9:
        return None
    if adj < 0.0:
        return 0.0
    if adj > 1.0:
        return 1.0
    return float(adj)


def warn_if_invalid_production_neg_pos_ratio(ratio: Optional[float]) -> None:
    """Log one warning when production neg/pos ratio cannot be used for prod_adjusted fields."""
    if ratio is None:
        return
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        logger.warning(
            "PRODUCTION_NEG_POS_RATIO=%r is invalid (must be finite and > 0); "
            "all prod_adjusted test precision fields (including precision@recall *_prod_adjusted) will be None.",
            ratio,
        )
        return
    if not math.isfinite(r) or r <= 0.0:
        logger.warning(
            "PRODUCTION_NEG_POS_RATIO=%r is invalid (must be finite and > 0); "
            "all prod_adjusted test precision fields (including precision@recall *_prod_adjusted) will be None.",
            ratio,
        )


# Legacy underscore aliases — historic call sites import these by name from
# ``trainer.training.trainer``. Keep both names bound to the same callable so
# refactor remains zero-behavior-change.
_precision_prod_adjusted = precision_prod_adjusted
_warn_if_invalid_production_neg_pos_ratio = warn_if_invalid_production_neg_pos_ratio
