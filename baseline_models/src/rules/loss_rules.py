"""R2：損失／下注 proxy；``net`` 與 ``wager`` 必須分開 metrics 列（SSOT §4.1 R2）。"""

from __future__ import annotations

from typing import Literal

import pandas as pd

LossProxy = Literal["net", "wager"]


def loss_rule_scores(
    frame: pd.DataFrame,
    proxy: LossProxy,
    *,
    net_column: str,
    wager_column: str,
) -> pd.Series:
    """依 proxy 從**不同欄**產生規則分數（禁止合併為單一 proxy 分數）。

    **net（玩家視角）**：欄位語意為「淨值／輸贏」，**負值＝玩家虧損**（SSOT §4.1 R2）。
    評估用分數採 ``-net``，使「虧越多 → 分數越大 → 越易判 walkaway」與 DEC-026 高分判正一致。

    **wager**：欄位為累積下注額等；分數＝欄位值（越大風險越高之單調假設，實務可改 YAML 換欄）。

    Args:
        frame: 已通過契約之表。
        proxy: ``net`` 或 ``wager``。
        net_column: ``proxy=net`` 時讀取之欄名。
        wager_column: ``proxy=wager`` 時讀取之欄名。

    Returns:
        與 ``frame`` 索引對齊之浮點分數。

    Raises:
        ValueError: proxy 非法、欄位含 NaN。
        KeyError: 缺欄。
    """
    if proxy == "net":
        col = net_column
    elif proxy == "wager":
        col = wager_column
    else:
        raise ValueError(f"proxy 必須為 net 或 wager，收到: {proxy!r}")
    if col not in frame.columns:
        raise KeyError(
            f"R2 proxy={proxy!r} 缺欄 {col!r}；現有欄位: {list(frame.columns)!r}"
        )
    raw = pd.to_numeric(frame[col], errors="coerce")
    if raw.isna().any():
        n_bad = int(raw.isna().sum())
        raise ValueError(f"R2 欄位 {col!r}（proxy={proxy}）含 NaN: count={n_bad}")
    if proxy == "net":
        return (-raw).astype("float64")
    return raw.astype("float64")
