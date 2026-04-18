"""資料契約與 loader（F2：標籤、censored、時間窗、時序切分、禁 shuffle）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .baseline_config import BaselineRunConfig


@dataclass(frozen=True)
class BaselineSplitSpec:
    """時序切分描述（禁止隨機 shuffle；細節見 SSOT §3.2）。"""

    protocol_name: str
    train_end_exclusive: Any
    valid_end_exclusive: Any
    test_end_exclusive: Any


def validate_binary_labels(frame: pd.DataFrame, label_col: str) -> None:
    """確認標籤欄僅含 0／1（可為整數或浮點 0.0／1.0）。

    Args:
        frame: 資料表。
        label_col: 標籤欄名。

    Raises:
        KeyError: 缺欄。
        ValueError: 含非二元值或 NaN。
    """
    if label_col not in frame.columns:
        raise KeyError(f"validate_binary_labels: 缺欄 {label_col!r}")
    s = pd.to_numeric(frame[label_col], errors="coerce")
    if s.isna().any():
        bad = int(s.isna().sum())
        raise ValueError(f"標籤欄 {label_col!r} 含 NaN: count={bad}")
    u = pd.unique(s)
    for v in u:
        if float(v) not in (0.0, 1.0):
            raise ValueError(f"標籤欄 {label_col!r} 必須為 0/1，收到唯一值: {u!r}")


def validate_label_and_censor_columns(frame: pd.DataFrame, label_col: str, censored_col: str) -> pd.DataFrame:
    """檢查必要欄位並排除 ``censored=True``（fail-fast）。

    Args:
        frame: 輸入資料表。
        label_col: 二元標籤欄位名。
        censored_col: 是否 censored 之布林欄位名。

    Returns:
        已排除 censored 列之副本。

    Raises:
        KeyError: 缺欄。
        ValueError: dtype 或內容不符合預期。
    """
    missing = [c for c in (label_col, censored_col) if c not in frame.columns]
    if missing:
        raise KeyError(f"缺少欄位: missing={missing!r}; columns={list(frame.columns)!r}")
    out = frame.loc[~frame[censored_col].astype(bool)].copy()
    if out.empty:
        raise ValueError("排除 censored 後資料為空。")
    validate_binary_labels(out, label_col)
    return out


def build_synthetic_smoke_frame(n_rows: int = 48) -> pd.DataFrame:
    """產生最小可評估表（含一筆 censored 供過濾測試）。

    Args:
        n_rows: 列數（至少 10）。

    Returns:
        含 ``bet_time``／``label``／``censored``／``smoke_score``、R1／R2／R3 合成欄位之表。
    """
    if n_rows < 10:
        raise ValueError(f"n_rows 過小: 收到 {n_rows}，期望 >= 10")
    rng = np.random.default_rng(0)
    times = pd.date_range("2024-01-01", periods=n_rows, freq="min", tz=None)
    labels = rng.integers(0, 2, size=n_rows).astype(np.int8)
    censored = np.zeros(n_rows, dtype=bool)
    censored[-1] = True
    # 與 label 略相關之分數，使 PR 曲線非退化
    base = labels.astype(float) * 0.55 + rng.random(n_rows) * 0.45
    # SSOT §4.1 R1：pace_drop／gap 類欄位（越高風險越高），供 Tier-0 smoke／單測
    pace_drop_ratio = rng.random(n_rows) * 0.5 + labels.astype(float) * 0.35
    pace_drop_ratio_w15m_w30m = rng.random(n_rows) * 0.5 + labels.astype(float) * 0.3
    prev_bet_gap_min = rng.exponential(1.5, size=n_rows) + labels.astype(float) * 4.0
    # R2：net 負值＝玩家虧（SSOT）；wager 為累積下注額 proxy
    loss_proxy_net = rng.normal(0.0, 120.0, size=n_rows) - labels.astype(float) * 180.0
    loss_proxy_wager = rng.uniform(20.0, 400.0, size=n_rows) + labels.astype(float) * 220.0
    # R3：theo／活躍日／session 計 ADT（SSOT §4.1 R3）
    theo_win_sum_30d = rng.uniform(50.0, 800.0, size=n_rows) + labels.astype(float) * 150.0
    theo_win_sum_180d = theo_win_sum_30d * rng.uniform(3.0, 6.0, size=n_rows)
    active_days_30d = rng.integers(3, 22, size=n_rows).astype(float)
    active_days_180d = rng.integers(20, 90, size=n_rows).astype(float)
    sessions_30d = rng.integers(2, 18, size=n_rows).astype(float)
    current_session_theo = rng.uniform(5.0, 120.0, size=n_rows) + labels.astype(float) * 60.0
    return pd.DataFrame(
        {
            "bet_time": times,
            "label": labels,
            "censored": censored,
            "smoke_score": base,
            "pace_drop_ratio": pace_drop_ratio,
            "pace_drop_ratio_w15m_w30m": pace_drop_ratio_w15m_w30m,
            "prev_bet_gap_min": prev_bet_gap_min,
            "loss_proxy_net": loss_proxy_net,
            "loss_proxy_wager": loss_proxy_wager,
            "theo_win_sum_30d": theo_win_sum_30d,
            "theo_win_sum_180d": theo_win_sum_180d,
            "active_days_30d": active_days_30d,
            "active_days_180d": active_days_180d,
            "sessions_30d": sessions_30d,
            "current_session_theo": current_session_theo,
        }
    )


def apply_time_window(
    frame: pd.DataFrame,
    time_col: str,
    window: dict[str, Any] | None,
) -> pd.DataFrame:
    """依 ``data_window.start``／``end`` 過濾（皆空則原樣返回）。

    Args:
        frame: 輸入表。
        time_col: 時間欄位。
        window: ``data_window`` mapping；可為 ``None``。

    Returns:
        過濾後副本。

    Raises:
        KeyError: 缺時間欄。
        ValueError: 起迄皆空但欄位含 NaT。
    """
    if window is None or (window.get("start") is None and window.get("end") is None):
        if time_col in frame.columns and frame[time_col].isna().any():
            raise ValueError(f"時間欄 {time_col!r} 含 NaT，請修正資料或縮窗。")
        return frame.copy()
    if time_col not in frame.columns:
        raise KeyError(f"apply_time_window: 缺時間欄 {time_col!r}")
    out = frame.copy()
    ts = pd.to_datetime(out[time_col], errors="coerce")
    if ts.isna().any():
        raise ValueError(f"時間欄 {time_col!r} 含無法解析之值")
    out[time_col] = ts
    start = window.get("start")
    end = window.get("end")
    if start is not None:
        out = out.loc[out[time_col] >= pd.Timestamp(start)]
    if end is not None:
        out = out.loc[out[time_col] < pd.Timestamp(end)]
    if out.empty:
        raise ValueError("時間窗過濾後資料為空。")
    return out


def temporal_train_valid_test_split(
    frame: pd.DataFrame,
    time_col: str,
    train_frac: float,
    valid_frac: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, BaselineSplitSpec]:
    """依時間排序後做前段／中段／後段切片（**不** shuffle）。

    Args:
        frame: 已過濾之表。
        time_col: 時間欄。
        train_frac: 訓練比例 ``(0,1)``。
        valid_frac: 在非訓練列中，驗證所占比例 ``(0,1)``；剩餘為 test。

    Returns:
        ``(train_df, valid_df, test_df, split_spec)``

    Raises:
        ValueError: 比例非法或列數不足。
    """
    if not (0.0 < train_frac < 1.0):
        raise ValueError(f"train_frac 必須在 (0,1): 收到 {train_frac!r}")
    if not (0.0 < valid_frac < 1.0):
        raise ValueError(f"valid_frac 必須在 (0,1): 收到 {valid_frac!r}")
    if time_col not in frame.columns:
        raise KeyError(f"temporal split: 缺欄 {time_col!r}")
    ordered = frame.sort_values(time_col, kind="mergesort").reset_index(drop=True)
    n = len(ordered)
    if n < 8:
        raise ValueError(f"切分需要足夠列數: 收到 n={n}")
    n_train = max(1, int(n * train_frac))
    rest = n - n_train
    if rest < 2:
        raise ValueError("訓練比例過大，導致 valid/test 不足。")
    n_valid = max(1, int(rest * valid_frac))
    n_test = rest - n_valid
    if n_test < 1:
        raise ValueError("valid_frac 過大，導致 test 為空。")
    train_df = ordered.iloc[:n_train].copy()
    valid_df = ordered.iloc[n_train : n_train + n_valid].copy()
    test_df = ordered.iloc[n_train + n_valid :].copy()
    spec = BaselineSplitSpec(
        protocol_name="temporal_slice_no_shuffle",
        train_end_exclusive=train_df[time_col].max(),
        valid_end_exclusive=valid_df[time_col].max(),
        test_end_exclusive=test_df[time_col].max(),
    )
    return train_df, valid_df, test_df, spec


def load_baseline_frame(config: BaselineRunConfig) -> pd.DataFrame:
    """依 ``data_source.kind`` 載入資料（fail-fast）。

    Args:
        config: 執行設定。

    Returns:
        原始表（尚未排除 censored）。

    Raises:
        FileNotFoundError: parquet 路徑不存在。
        ValueError: 不支援的 kind。
    """
    kind = config.data_source_kind
    if kind == "synthetic_smoke":
        return build_synthetic_smoke_frame()
    if kind == "parquet":
        rel = config.data_source_path
        if not rel:
            raise ValueError("data_source.kind=parquet 時必須提供 data_source.path_or_uri")
        path = Path(rel)
        if not path.is_file():
            cand = (config.config_path.parent / rel).resolve()
            path = cand
        if not path.is_file():
            raise FileNotFoundError(f"找不到 parquet: {rel!r}（亦嘗試 {path!r}）")
        return pd.read_parquet(path)
    raise ValueError(
        f"不支援的 data_source.kind={kind!r}；允許: synthetic_smoke, parquet"
    )
