"""W2 SSOT: run contract fields shared by trainer artifacts, backtest, and scorer.

``read_bundle_run_contract_block`` returns the same top-level keys written to
``backtest_metrics.json`` and merged into scorer ``load_dual_artifacts`` output:
``selection_mode``, ``selection_mode_source``, ``production_neg_pos_ratio``.

``selection_mode`` prefers a non-empty ``selection_mode`` in the bundle's
``training_metrics.v2.json`` (when present and readable), else
``training_metrics.json``; otherwise :data:`trainer.core.config.SELECTION_MODE`.

``production_neg_pos_ratio`` defaults to :data:`trainer.core.config.PRODUCTION_NEG_POS_RATIO`.
Callers that load config via a different module (e.g. backtester ``_cfg``) may pass
``production_neg_pos_ratio=...`` so the contract matches ``compute_micro_metrics``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from trainer.core import config
from trainer.core.training_metrics_bundle import load_training_metrics_for_contract

_MISSING = object()


def read_bundle_run_contract_block(
    bundle_root: Optional[Path],
    *,
    production_neg_pos_ratio: Any = _MISSING,
) -> dict[str, Any]:
    """Return ``selection_mode``, ``selection_mode_source``, ``production_neg_pos_ratio``."""
    sel = str(getattr(config, "SELECTION_MODE", "legacy") or "legacy").strip() or "legacy"
    src = "config"
    if production_neg_pos_ratio is _MISSING:
        pn: Optional[float] = getattr(config, "PRODUCTION_NEG_POS_RATIO", None)
    else:
        pn = production_neg_pos_ratio  # type: ignore[assignment]

    if bundle_root is not None:
        tm, src_hint = load_training_metrics_for_contract(Path(bundle_root))
        if isinstance(tm, dict):
            raw_mode = tm.get("selection_mode")
            if raw_mode is not None:
                cand = str(raw_mode).strip()
                if cand:
                    sel = cand
                    src = src_hint

    return {
        "selection_mode": sel,
        "selection_mode_source": src,
        "production_neg_pos_ratio": pn,
    }
