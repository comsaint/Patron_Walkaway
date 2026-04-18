"""Phase D：可選載入 LightGBM（同窗）`training_metrics.json` 摘要。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


def resolve_repo_relative_path(config_path: Path, relative: str) -> Path:
    """將相對路徑解析為絕對路徑（相對於倉庫根：``…/baseline_models/config/<file>`` 之上兩層）。"""
    root = config_path.resolve().parents[2]
    return (root / relative).resolve()


def load_training_metrics_reference(
    metrics_path: Path,
    section: str,
) -> dict[str, Any]:
    """讀取 trainer 產出之 ``training_metrics.json`` 中一區塊，供 E2 同窗表。

    Args:
        metrics_path: JSON 檔絕對路徑。
        section: 頂層鍵（常見 ``rated`` 或 ``model_default``）。

    Returns:
        含 ``status`` 與摘要用欄位；失敗時 ``status``=``error`` 並附 ``reason``。
    """
    if not metrics_path.is_file():
        return {
            "status": "missing_file",
            "reason": f"檔案不存在: {metrics_path!r}",
            "path": str(metrics_path),
            "section": section,
        }
    try:
        data = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return {
            "status": "error",
            "reason": f"讀取或解析失敗: {e!r}",
            "path": str(metrics_path),
            "section": section,
        }
    sec = data.get(section)
    if not isinstance(sec, Mapping):
        return {
            "status": "error",
            "reason": f"頂層無 mapping 區段 {section!r}",
            "path": str(metrics_path),
            "section": section,
        }
    return {
        "status": "loaded",
        "path": str(metrics_path),
        "section": section,
        "test_ap": sec.get("test_ap"),
        "test_precision_at_recall_0.01": sec.get("test_precision_at_recall_0.01"),
        "test_precision_at_recall_0.01_prod_adjusted": sec.get(
            "test_precision_at_recall_0.01_prod_adjusted"
        ),
        "threshold_at_recall_0.01": sec.get("threshold_at_recall_0.01"),
        "test_samples": sec.get("test_samples"),
        "test_positives": sec.get("test_positives"),
    }


def reference_precision_at_recall_0_01_for_peer(ref: Mapping[str, Any]) -> float | None:
    """同窗 P@R=1%：優先採生產先驗／負採樣修正鍵，缺則退回 test 上 raw 操作點 precision。

    Args:
        ref: :func:`load_training_metrics_reference` 成功時之 mapping（status=loaded）。

    Returns:
        可供與 baseline ``precision_at_recall_0.01`` 比較之浮點數，或 ``None``。
    """
    k_adj = "test_precision_at_recall_0.01_prod_adjusted"
    v_adj = ref.get(k_adj)
    if isinstance(v_adj, (int, float)):
        return float(v_adj)
    v_raw = ref.get("test_precision_at_recall_0.01")
    if isinstance(v_raw, (int, float)):
        return float(v_raw)
    return None


def peer_delta_pp(baseline_pat: float | None, ref_pat: float | None) -> float | None:
    """回傳 baseline − ref 之 percentage points；任一為 ``None`` 則 ``None``。"""
    if baseline_pat is None or ref_pat is None:
        return None
    return float(baseline_pat) - float(ref_pat)


def build_reference_lightgbm_snapshot(
    *,
    enabled: bool,
    config_path: Path,
    training_metrics_path: str | None,
    metrics_section: str,
) -> dict[str, Any]:
    """組 ``run_state.reference_lightgbm`` 區塊（E2／SSOT §8 同窗對照來源）。"""
    if not enabled:
        return {"enabled": False}
    if not training_metrics_path or not str(training_metrics_path).strip():
        return {
            "enabled": True,
            "status": "skipped",
            "reason": "reference_model.enabled 為 true 但未設定 training_metrics_path",
        }
    abs_p = resolve_repo_relative_path(config_path, str(training_metrics_path).strip())
    snap = load_training_metrics_reference(abs_p, metrics_section)
    snap["enabled"] = True
    return snap


def markdown_lightgbm_peer_table(
    metrics_rows: list[Mapping[str, Any]],
    ref: Mapping[str, Any],
) -> str:
    """E2：LightGBM（或同窗）``training_metrics`` 之 P@R=1% 對照 Markdown。"""
    if ref.get("status") != "loaded":
        return ""
    ref_pat = reference_precision_at_recall_0_01_for_peer(ref)
    ref_ap = ref.get("test_ap")
    raw_pat = ref.get("test_precision_at_recall_0.01")
    adj_pat = ref.get("test_precision_at_recall_0.01_prod_adjusted")
    peer_source = (
        "`test_precision_at_recall_0.01_prod_adjusted`"
        if isinstance(adj_pat, (int, float))
        else "`test_precision_at_recall_0.01`（無 prod_adjusted 鍵）"
    )
    lines = [
        "\n## Phase D — LightGBM 同窗對照（參考 metrics 檔）\n\n",
        f"- **參考檔**：`{ref.get('path')}`（section=`{ref.get('section')}`）\n",
        f"- **參考 test_ap**：{ref_ap!s}；**參考 P@R=1%（同窗用）**：{ref_pat!s}（來源：{peer_source}；"
        f"raw `test_precision_at_recall_0.01`={raw_pat!s}）\n",
        "- **註**：參考檔與本次 `data_window`／切分未必一致時，下表 Δ 僅供並列檢視，不作同窗硬比結論。\n\n",
        "| model_type | baseline P@R=1% | Δ vs 參考 (pp) |\n",
        "|:---|---:|---:|\n",
    ]
    for row in metrics_rows:
        mt = str(row.get("model_type") or "")
        pat = row.get("precision_at_recall_0.01")
        if isinstance(pat, (int, float)):
            pat_s = f"{float(pat):.4f}"
            dpp = peer_delta_pp(float(pat), ref_pat)
            d_cell = f"{dpp:+.4f}" if dpp is not None else "—"
        else:
            pat_s = "—"
            d_cell = "—"
        lines.append(f"| `{mt}` | {pat_s} | {d_cell} |\n")
    return "".join(lines)
