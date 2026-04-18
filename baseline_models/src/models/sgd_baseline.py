"""M2：SGDClassifier 基線（SSOT §4.2：loss=log_loss、class_weight=balanced）。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import SGDClassifier


def fit_sgd_baseline(
    train_x: pd.DataFrame,
    train_y: pd.Series,
    *,
    max_iter: int = 1000,
    random_state: int = 42,
    **kwargs: Any,
) -> SGDClassifier:
    """訓練 SGD logistic 基線（僅時序訓練切片；二元分類）。

    Args:
        train_x: 訓練特徵（全為數值欄）。
        train_y: 訓練標籤 ``0/1``。
        max_iter: 訓練資料最大 passes（對應 ``SGDClassifier.max_iter``）。
        random_state: 隨機種子（權重初始化與洗牌）。
        **kwargs: 覆寫傳入 ``SGDClassifier`` 之額外參數（預設為 SSOT 建議）。

    Returns:
        已擬合之 ``SGDClassifier``（``loss=log_loss`` 時具 ``predict_proba``）。

    Raises:
        ValueError: 訓練集僅單一類別或列數為 0。
    """
    if len(train_x) == 0:
        raise ValueError("fit_sgd_baseline: train_x 為空。")
    y = np.asarray(train_y).astype(int)
    if len(np.unique(y)) < 2:
        raise ValueError(
            f"fit_sgd_baseline: 訓練集須含兩類別；收到 unique={np.unique(y)!r}"
        )
    params: dict[str, Any] = {
        "loss": "log_loss",
        "class_weight": "balanced",
        "max_iter": int(max_iter),
        "random_state": int(random_state),
        "n_jobs": 1,
        "penalty": "l2",
        "tol": 1e-3,
    }
    params.update(kwargs)
    est = SGDClassifier(**params)
    est.fit(train_x.to_numpy(dtype=np.float64, copy=False), y)
    return est


def predict_proba_positive(model: SGDClassifier, x: pd.DataFrame) -> np.ndarray:
    """回傳正類（標籤 ``1``）之預測機率。

    Args:
        model: 已擬合之二元 ``SGDClassifier``（``loss=log_loss``）。
        x: 與訓練欄序一致之特徵表。

    Returns:
        形狀 ``(n_samples,)`` 之正類機率。

    Raises:
        ValueError: 模型類別數非 2，或類別中不含 ``1``。
    """
    classes = list(model.classes_)
    if len(classes) != 2:
        raise ValueError(f"predict_proba_positive: 預期二元模型，classes={classes!r}")
    if 1 not in classes:
        raise ValueError(f"predict_proba_positive: 預期正類標籤 1，classes={classes!r}")
    pos_idx = int(classes.index(1))
    proba = model.predict_proba(x.to_numpy(dtype=np.float64, copy=False))[:, pos_idx]
    return np.asarray(proba, dtype=np.float64)
