"""E1：precision@recall（DEC-026）、PR-AUC、alerts（SSOT §7 canonical 鍵名）。"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from sklearn.metrics import average_precision_score

from .dec026_imports import (
    THRESHOLD_FBETA,
    dec026_pr_alert_arrays,
    dec026_sanitize_per_hour_params,
    pick_threshold_dec026,
    pick_threshold_dec026_from_pr_arrays,
)

# 與 ``trainer/training/backtester.py`` 之 ``_TARGET_RECALLS``（DEC-026）對齊
DEC026_REPORT_RECALLS: tuple[float, ...] = (0.001, 0.01, 0.1, 0.5)


def precision_at_recall_operating_point(
    y_true: np.ndarray,
    y_score: np.ndarray,
    target_recall: float = 0.01,
    *,
    min_alert_count: int = 1,
    min_alerts_per_hour: float | None = None,
    window_hours: float | None = None,
) -> dict[str, float | None]:
    """於 PR 曲線上取固定 recall 操作點（與 trainer DEC-026 選阈一致）。

    Args:
        y_true: 二元真值 ``0/1``，形狀 ``(n,)``。
        y_score: 分數，形狀 ``(n,)``；越大越易判為正。
        target_recall: recall 下限（主指標 ``0.01``）。
        min_alert_count: 最小 alert 筆數約束。
        min_alerts_per_hour: 可選，搭配 ``window_hours``。
        window_hours: 可選評估窗（小時）。

    Returns:
        含 ``precision_at_recall_0.01``、``threshold_at_recall_0.01``、``internal_recall`` 等。

    Raises:
        ValueError: 長度不一致。
    """
    if y_true.shape != y_score.shape:
        raise ValueError(
            f"y_true 與 y_score 長度不一致: {y_true.shape!r} vs {y_score.shape!r}"
        )
    pick = pick_threshold_dec026(
        y_true,
        y_score,
        recall_floor=float(target_recall),
        min_alert_count=int(min_alert_count),
        min_alerts_per_hour=min_alerts_per_hour,
        window_hours=window_hours,
        fbeta_beta=float(THRESHOLD_FBETA),
    )
    thr = float(pick.threshold) if not pick.is_fallback else None
    prec = float(pick.precision) if not pick.is_fallback else None
    out: dict[str, float | None] = {
        "precision_at_recall_0.01": prec,
        "threshold_at_recall_0.01": thr,
        "internal_recall_at_pick": float(pick.recall) if not pick.is_fallback else None,
    }
    return out


def dec026_multi_operating_point_columns(
    y_true: np.ndarray,
    y_score: np.ndarray,
    *,
    min_alert_count: int,
    min_alerts_per_hour: float | None,
    window_hours: float | None,
    fbeta_beta: float,
) -> dict[str, float | int | None]:
    """對 DEC-026 報告 recall 集合重複選阈（與 trainer ``training_metrics`` 鍵名對齊）。"""
    prep = dec026_pr_alert_arrays(y_true, y_score)
    out: dict[str, float | int | None] = {}
    if prep is None:
        for r in DEC026_REPORT_RECALLS:
            out[f"test_precision_at_recall_{r}"] = None
            out[f"threshold_at_recall_{r}"] = None
            out[f"n_alerts_at_recall_{r}"] = None
            out[f"alerts_per_minute_at_recall_{r}"] = None
        return out
    pr_p, pr_r, pr_th, ac, _n = prep
    wh_eff, mah_eff = dec026_sanitize_per_hour_params(window_hours, min_alerts_per_hour)
    window_minutes = (float(wh_eff) * 60.0) if (wh_eff is not None and wh_eff > 0) else None
    y_s = np.asarray(y_score, dtype=float)
    min_ac = max(1, int(min_alert_count))
    for r in DEC026_REPORT_RECALLS:
        pick = pick_threshold_dec026_from_pr_arrays(
            pr_p,
            pr_r,
            pr_th,
            ac,
            recall_floor=float(r),
            min_alert_count=min_ac,
            min_alerts_per_hour=mah_eff,
            window_hours=wh_eff,
            fbeta_beta=float(fbeta_beta),
        )
        if pick.is_fallback:
            out[f"test_precision_at_recall_{r}"] = None
            out[f"threshold_at_recall_{r}"] = None
            out[f"n_alerts_at_recall_{r}"] = None
            out[f"alerts_per_minute_at_recall_{r}"] = None
            continue
        thr = float(pick.threshold)
        n_al = int(np.sum(y_s >= thr))
        apm: float | None
        if window_minutes and window_minutes > 0:
            apm = float(n_al) / float(window_minutes)
        else:
            apm = None
        out[f"test_precision_at_recall_{r}"] = float(pick.precision)
        out[f"threshold_at_recall_{r}"] = thr
        out[f"n_alerts_at_recall_{r}"] = n_al
        out[f"alerts_per_minute_at_recall_{r}"] = apm
    return out


def pr_auc(y_true: np.ndarray, y_score: np.ndarray) -> float | None:
    """PR 曲線下面積（對外鍵名 ``pr_auc``）；單一類別時回傳 ``None``。"""
    y_t = np.asarray(y_true, dtype=float)
    y_s = np.asarray(y_score, dtype=float)
    if y_t.shape != y_s.shape or y_t.size == 0:
        return None
    n_pos = int(np.sum(y_t == 1))
    if n_pos == 0 or n_pos == len(y_t):
        return None
    if np.any(np.isnan(y_s)) or np.any(np.isnan(y_t)):
        return None
    return float(average_precision_score(y_t, y_s))


def alerts_and_rate(y_score: np.ndarray, threshold: float | None) -> tuple[int, float | None]:
    """在固定阈下計算 alerts 數與占比。

    Args:
        y_score: 分數陣列。
        threshold: 判警阈；``None`` 時回傳 ``(0, None)``。

    Returns:
        ``(alerts_count, alerts_rate)``。
    """
    n = int(y_score.shape[0])
    if threshold is None or n == 0:
        return 0, None
    try:
        thr = float(threshold)
    except (TypeError, ValueError):
        return 0, None
    m = np.asarray(y_score, dtype=float) >= thr
    a = int(np.sum(m))
    return a, float(a) / float(n)


def canonical_metrics_row_stub(extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """產生最小 JSON 可序列化列，供 smoke 驗證 schema（數值為 null 占位）。

    Args:
        extra: 合併至輸出之列額外欄位。

    Returns:
        含 SSOT §7 常見必填鍵之字典（值待實際評估填入）。
    """
    row: dict[str, Any] = {
        "experiment_id": None,
        "baseline_family": None,
        "model_type": None,
        "proxy_type": None,
        "data_window": None,
        "split_protocol": None,
        "feature_set_version": None,
        "label_contract_version": None,
        "precision_at_recall_0.01": None,
        "threshold_at_recall_0.01": None,
        "pr_auc": None,
        "alerts": None,
        "alerts_rate": None,
        "runtime_sec": None,
        "peak_memory_est_mb": None,
        "decision": None,
        "notes": None,
    }
    if extra:
        row.update(dict(extra))
    return row


def build_eval_metrics_row(
    *,
    experiment_id: str,
    baseline_family: str,
    model_type: str,
    proxy_type: str | None,
    split_protocol: str,
    feature_set_version: str | None,
    label_contract_version: str | None,
    data_window: Mapping[str, Any] | None,
    y_true: np.ndarray,
    y_score: np.ndarray,
    min_alert_count: int,
    recall_floor: float | None,
    min_alerts_per_hour: float | None,
    window_hours: float | None,
    runtime_sec: float,
    peak_memory_est_mb: float | None,
    decision: str,
    notes: str,
) -> dict[str, Any]:
    """組裝單筆 SSOT §7 對外列（DEC-026 + canonical 指標鍵）。"""
    pat = precision_at_recall_operating_point(
        y_true,
        y_score,
        target_recall=float(recall_floor if recall_floor is not None else 0.01),
        min_alert_count=min_alert_count,
        min_alerts_per_hour=min_alerts_per_hour,
        window_hours=window_hours,
    )
    thr = pat.get("threshold_at_recall_0.01")
    auc = pr_auc(y_true, y_score)
    alerts, rate = alerts_and_rate(y_score, thr)
    multi = dec026_multi_operating_point_columns(
        y_true,
        y_score,
        min_alert_count=min_alert_count,
        min_alerts_per_hour=min_alerts_per_hour,
        window_hours=window_hours,
        fbeta_beta=float(THRESHOLD_FBETA),
    )
    row = canonical_metrics_row_stub(
        {
            "experiment_id": experiment_id,
            "baseline_family": baseline_family,
            "model_type": model_type,
            "proxy_type": proxy_type,
            "data_window": dict(data_window) if data_window else None,
            "split_protocol": split_protocol,
            "feature_set_version": feature_set_version,
            "label_contract_version": label_contract_version,
            "precision_at_recall_0.01": pat.get("precision_at_recall_0.01"),
            "threshold_at_recall_0.01": thr,
            "pr_auc": auc,
            "alerts": alerts,
            "alerts_rate": rate,
            "runtime_sec": float(runtime_sec),
            "peak_memory_est_mb": peak_memory_est_mb,
            "decision": decision,
            "notes": notes,
        }
    )
    row.update(multi)
    return row


def build_smoke_metrics_row(
    *,
    experiment_id: str,
    split_protocol: str,
    feature_set_version: str | None,
    label_contract_version: str | None,
    data_window: Mapping[str, Any] | None,
    y_true: np.ndarray,
    y_score: np.ndarray,
    min_alert_count: int,
    recall_floor: float | None,
    min_alerts_per_hour: float | None,
    window_hours: float | None,
    runtime_sec: float,
    peak_memory_est_mb: float | None,
) -> dict[str, Any]:
    """組裝 Phase A smoke 列（非 R1／R2 正式規則）。"""
    return build_eval_metrics_row(
        experiment_id=experiment_id,
        baseline_family="rule",
        model_type="smoke_score_order",
        proxy_type=None,
        split_protocol=split_protocol,
        feature_set_version=feature_set_version,
        label_contract_version=label_contract_version,
        data_window=data_window,
        y_true=y_true,
        y_score=y_score,
        min_alert_count=min_alert_count,
        recall_floor=recall_floor,
        min_alerts_per_hour=min_alerts_per_hour,
        window_hours=window_hours,
        runtime_sec=runtime_sec,
        peak_memory_est_mb=peak_memory_est_mb,
        decision="iterate",
        notes="Phase A smoke：僅驗證產物 schema 與 DEC-026 接線；非正式 baseline。",
    )
