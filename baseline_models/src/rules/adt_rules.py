"""R3：ADT／理論貢獻估算（SSOT §4.1 R3）；估算式摘要供寫入 ``run_state.json``。"""

from __future__ import annotations

import json
from typing import Any, FrozenSet, Literal, Mapping

import numpy as np
import pandas as pd

AdtVariant = Literal["adt30", "adt180", "theo_per_session"]

R3_ADT_VARIANTS: FrozenSet[str] = frozenset({"adt30", "adt180", "theo_per_session"})


def r3_formula_specs() -> dict[str, str]:
    """R3 估算式文字摘要（供 ``run_state`` 稽核；語意對齊 SSOT §4.1 R3）。"""
    return {
        "adt30": (
            "ADT_30d = theo_win_sum_30d / max(active_days_30d, 1)；"
            "若無欄位 active_days_30d 則 ADT_30d = theo_win_sum_30d / 30。"
        ),
        "adt180": (
            "ADT_180d = theo_win_sum_180d / max(active_days_180d, 1)；"
            "若無欄位 active_days_180d 則 ADT_180d = theo_win_sum_180d / 180。"
        ),
        "theo_per_session": (
            "TheoPerSession_30d = theo_win_sum_30d / max(sessions_30d, 1)；"
            "若無欄位 sessions_30d 則分母為 1。"
        ),
        "score": (
            "ratio = current_session_theo / max(ADT_est * tau, epsilon)；"
            "tau 為敏感度係數（SSOT §4.1 R3 建議掃描如 0.8,1.0,1.2,1.5,2.0）；"
            "tau=1 時等同原式 current_session_theo / max(ADT_est, epsilon)。"
        ),
    }


def _require_numeric_column(frame: pd.DataFrame, col: str) -> pd.Series:
    """讀取數值欄；缺欄或含 NaN 則 fail-fast。"""
    if col not in frame.columns:
        raise KeyError(f"R3 缺欄 {col!r}；現有欄位: {list(frame.columns)!r}")
    s = pd.to_numeric(frame[col], errors="coerce")
    if s.isna().any():
        n_bad = int(s.isna().sum())
        raise ValueError(f"R3 欄位 {col!r} 含 NaN: count={n_bad}")
    return s.astype("float64")


def _adt_est_adt30(frame: pd.DataFrame) -> pd.Series:
    """30 日 ADT 估算（SSOT §4.1 R3）。"""
    theo = _require_numeric_column(frame, "theo_win_sum_30d")
    if "active_days_30d" in frame.columns:
        ad = pd.to_numeric(frame["active_days_30d"], errors="coerce").fillna(0.0)
        return theo / np.maximum(ad.to_numpy(dtype=float), 1.0)
    return theo / 30.0


def _adt_est_adt180(frame: pd.DataFrame) -> pd.Series:
    """180 日 ADT 估算。"""
    theo = _require_numeric_column(frame, "theo_win_sum_180d")
    if "active_days_180d" in frame.columns:
        ad = pd.to_numeric(frame["active_days_180d"], errors="coerce").fillna(0.0)
        return theo / np.maximum(ad.to_numpy(dtype=float), 1.0)
    return theo / 180.0


def _adt_est_theo_per_session(frame: pd.DataFrame) -> pd.Series:
    """每 session 理論貢獻（30d）。"""
    theo = _require_numeric_column(frame, "theo_win_sum_30d")
    if "sessions_30d" in frame.columns:
        sess = pd.to_numeric(frame["sessions_30d"], errors="coerce").fillna(0.0)
        return theo / np.maximum(sess.to_numpy(dtype=float), 1.0)
    return theo.copy()


def _adt_est_for_variant(frame: pd.DataFrame, variant: AdtVariant) -> pd.Series:
    """依變體回傳 ADT 類分母估算序列。"""
    if variant == "adt30":
        return _adt_est_adt30(frame)
    if variant == "adt180":
        return _adt_est_adt180(frame)
    if variant == "theo_per_session":
        return _adt_est_theo_per_session(frame)
    raise ValueError(f"未知 R3 variant: {variant!r}")


def adt_rule_scores(
    frame: pd.DataFrame,
    variant: AdtVariant,
    *,
    current_session_theo_column: str = "current_session_theo",
    ratio_epsilon: float = 1e-9,
    tau: float = 1.0,
) -> pd.Series:
    """產生 R3 規則分數 ``current_session_theo / max(ADT_est * tau, eps)``（越大風險越高）。

    Args:
        frame: 已通過契約之表。
        variant: ``adt30``／``adt180``／``theo_per_session``（對應 SSOT ``proxy_type``）。
        current_session_theo_column: 本場／當期 theo 欄名。
        ratio_epsilon: 避免除以零。
        tau: ADT 分母敏感度係數（>0）；``1.0`` 即 SSOT 基線 ``cur/ADT_est``。

    Returns:
        與 ``frame`` 對齊之分數。

    Raises:
        ValueError: variant 非法、欄位含 NaN、``tau`` 非有限或 <=0。
        KeyError: 缺必要欄位。
    """
    if variant not in R3_ADT_VARIANTS:
        raise ValueError(f"R3 variant 必須為 {sorted(R3_ADT_VARIANTS)!r}，收到: {variant!r}")
    t = float(tau)
    if not np.isfinite(t) or t <= 0.0:
        raise ValueError(f"adt_rule_scores: tau 須為有限正數，收到: {tau!r}")
    cur = _require_numeric_column(frame, current_session_theo_column)
    adt_est = _adt_est_for_variant(frame, variant)
    denom = np.maximum(adt_est.to_numpy(dtype=float) * t, float(ratio_epsilon))
    ratio = cur.to_numpy(dtype=float) / denom
    return pd.Series(ratio, index=frame.index, dtype="float64")


def r3_model_type_for_metrics(variant: str, tau: float) -> str:
    """R3 metrics 列用之 ``model_type``（含 variant 與 tau，便於網格區分）。"""
    return f"R3_adt:{variant}:tau={json.dumps(float(tau))}"


def r3_run_state_block(
    *,
    enabled: bool,
    variants: list[str],
    current_session_theo_column: str,
    tau_grid: list[float],
) -> Mapping[str, Any]:
    """組 ``run_state.json`` 內 ``tier0_r3`` 區塊（含估算式文字）。"""
    return {
        "enabled": enabled,
        "variants_evaluated": list(variants),
        "tau_grid": list(tau_grid),
        "current_session_theo_column": current_session_theo_column,
        "formulas": r3_formula_specs(),
    }
