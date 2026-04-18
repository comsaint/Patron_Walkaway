"""自 trainer／walkaway_ml 匯入 DEC-026 選阈與 PR 陣列工具（同倉庫雙安裝名）。"""

from __future__ import annotations

from typing import Any, Callable, Tuple


def _load_dec026_bundle() -> Tuple[Any, Callable[..., Any], float]:
    """回傳 ``(threshold_selection_module, pick_threshold_dec026, THRESHOLD_FBETA)``。"""
    try:
        import walkaway_ml.training.threshold_selection as ts
        from walkaway_ml.core.config import THRESHOLD_FBETA as fb

        return ts, ts.pick_threshold_dec026, float(fb)
    except ImportError:
        pass
    try:
        import trainer.training.threshold_selection as ts
        from trainer.core.config import THRESHOLD_FBETA as fb

        return ts, ts.pick_threshold_dec026, float(fb)
    except ImportError as e:
        raise ImportError(
            "需要可匯入 walkaway_ml 或 trainer（建議在倉庫根執行 `pip install -e .`）"
            "以使用 DEC-026 與 `THRESHOLD_FBETA`。"
        ) from e


_ts_mod, pick_threshold_dec026, THRESHOLD_FBETA = _load_dec026_bundle()
dec026_pr_alert_arrays = _ts_mod.dec026_pr_alert_arrays
pick_threshold_dec026_from_pr_arrays = _ts_mod.pick_threshold_dec026_from_pr_arrays
dec026_sanitize_per_hour_params = _ts_mod.dec026_sanitize_per_hour_params

select_threshold_dec026 = pick_threshold_dec026
