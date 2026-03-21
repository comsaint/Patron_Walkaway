"""
STATUS Code Review（2026-03-22）— `log_metrics_safe` 可選 `step` 風險點 → 最小可重現／契約測試。

對應 `.cursor/plans/STATUS.md`「Code Review：`log_metrics_safe` 可選 `step` 變更」§1–§5。
- §2／§4／§5 已由 production／文件落地；本檔為契約與 §1 現狀鎖定 MRE。
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trainer.core import mlflow_utils

REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKTESTER_SRC = REPO_ROOT / "trainer" / "training" / "backtester.py"
_DOCS_STEP_MONOTONIC = (
    REPO_ROOT / "doc" / "phase2_p0_p1_implementation_plan.md",
    REPO_ROOT / "doc" / "phase2_provenance_schema.md",
)


def _patch_dummy_mlflow():
    """Inject fake mlflow module with log_metrics mock."""
    dummy = types.SimpleNamespace()
    mock_lm = MagicMock()
    dummy.log_metrics = mock_lm
    return dummy, mock_lm


# ---------------------------------------------------------------------------
# STATUS §1 — step 執行時型別：鎖定「現狀轉發」行為（bool／float／0／numpy.integer）
# ---------------------------------------------------------------------------


def test_review_step_mre_bool_true_forwards_step_kwarg_to_mlflow():
    """§1：現狀下 `step=True` 會進入 `step is not None` 並轉發給 `mlflow.log_metrics`（bool 為 int 子類之風險鎖定）。"""
    dummy, mock_lm = _patch_dummy_mlflow()
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch.dict(sys.modules, {"mlflow": dummy}):
            mlflow_utils.log_metrics_safe({"a": 1.0}, step=True)

    mock_lm.assert_called_once()
    assert mock_lm.call_args.kwargs.get("step") is True


def test_review_step_mre_float_forwards_step_kwarg_to_mlflow():
    """§1：現狀下 `step=1.5` 會原樣轉發（型別註解為 int 但執行未驗證之風險鎖定）。"""
    dummy, mock_lm = _patch_dummy_mlflow()
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch.dict(sys.modules, {"mlflow": dummy}):
            mlflow_utils.log_metrics_safe({"a": 1.0}, step=1.5)

    mock_lm.assert_called_once()
    assert mock_lm.call_args.kwargs.get("step") == 1.5


def test_review_step_mre_int_zero_forwards_step_zero():
    """§1：合法邊界 `step=0` 應仍帶 `step=0`（與「僅 None 省略 step」契約一致）。"""
    dummy, mock_lm = _patch_dummy_mlflow()
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch.dict(sys.modules, {"mlflow": dummy}):
            mlflow_utils.log_metrics_safe({"a": 1.0}, step=0)

    mock_lm.assert_called_once()
    assert mock_lm.call_args.kwargs.get("step") == 0


def test_review_step_mre_numpy_integer_forwards_to_mlflow():
    """§1：可選 — `numpy.integer` 非 `bool`、非內建 `int` 時現狀仍轉發（與 operator.index 正規化建議對照）。"""
    np = pytest.importorskip("numpy")
    dummy, mock_lm = _patch_dummy_mlflow()
    step_val = np.int64(7)
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch.dict(sys.modules, {"mlflow": dummy}):
            mlflow_utils.log_metrics_safe({"a": 1.0}, step=step_val)

    mock_lm.assert_called_once()
    assert mock_lm.call_args.kwargs.get("step") == step_val


# ---------------------------------------------------------------------------
# STATUS §3 — 全鍵過濾後 early return，不得呼叫 log_metrics（含帶 step）
# ---------------------------------------------------------------------------


def test_review_step_mre_all_nonfinite_with_step_does_not_call_log_metrics():
    """§3：僅 NaN/inf 無有限值時，即使傳 `step` 也不應觸發 `mlflow.log_metrics`。"""
    dummy, mock_lm = _patch_dummy_mlflow()
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch.dict(sys.modules, {"mlflow": dummy}):
            mlflow_utils.log_metrics_safe(
                {"nan": float("nan"), "inf": float("inf")}, step=99
            )

    assert mock_lm.call_count == 0


# ---------------------------------------------------------------------------
# STATUS §2 — 舊 client 拒絕 `step=` 時應有 fallback
# ---------------------------------------------------------------------------


def test_review_step_mre_typeerror_on_step_then_fallback_without_step():
    """§2：先 `log_metrics(..., step=1)` 失敗後再以無 `step` 成功。"""
    dummy, mock_lm = _patch_dummy_mlflow()
    mock_lm.side_effect = [
        TypeError("unexpected keyword argument 'step'"),
        None,
    ]
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch.dict(sys.modules, {"mlflow": dummy}):
            mlflow_utils.log_metrics_safe({"x": 1.0}, step=1)

    assert mock_lm.call_count == 2
    assert mock_lm.call_args_list[0].kwargs.get("step") == 1
    assert mock_lm.call_args_list[1].kwargs.get("step", "missing") in (None, "missing")


# ---------------------------------------------------------------------------
# STATUS §4 — backtester ImportError fallback 應吞 `step=`
# ---------------------------------------------------------------------------


def _backtester_import_error_block_src() -> str:
    lines = _BACKTESTER_SRC.read_text(encoding="utf-8").splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == "except ImportError:":
            start = i + 1
            break
    assert start is not None, "backtester.py: except ImportError not found"
    end = start
    while end < len(lines):
        if lines[end].startswith("HK_TZ ") or lines[end].startswith("HK_TZ="):
            break
        end += 1
    return "\n".join(lines[start:end])


def test_review_backtester_fallback_log_metrics_accepts_var_kwargs_contract():
    """§4：契約 — `except ImportError` 內之 `def log_metrics_safe` 應含 `**kwargs` 或 `**_kwargs`（避免 future `step=` 炸 backtest）。"""
    body = _backtester_import_error_block_src()
    assert "def log_metrics_safe" in body
    sig_line = next(
        ln for ln in body.splitlines() if "def log_metrics_safe" in ln
    )
    assert "**" in sig_line, f"fallback signature should absorb kwargs: {sig_line!r}"


# ---------------------------------------------------------------------------
# STATUS §5 — 文件應提示 caller 對 step 單調性責任
# ---------------------------------------------------------------------------


def test_review_doc_mentions_step_monotonic_guidance_for_callers():
    """§5：至少一份 SSOT doc 含單調／monotonic／遞減 等告誡字樣之一。"""
    pattern = re.compile(
        r"單調|monotonic|非遞減|遞減|non-?decreasing",
        re.IGNORECASE,
    )
    found = False
    for path in _DOCS_STEP_MONOTONIC:
        if not path.is_file():
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            found = True
            break
    assert found, "Expected step monotonic caller guidance in phase2 docs"

