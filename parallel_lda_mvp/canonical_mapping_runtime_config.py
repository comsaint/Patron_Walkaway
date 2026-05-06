"""MVP 運行參數：canonical mapping 更新節奏與晚到重算窗口。

預設值集中於此模組（之後可遷入 ``trainer/core``）。可選以環境變數覆寫。
"""
from __future__ import annotations

import os
from typing import Final

_ENV_REFRESH = "PARALLEL_LDA_MVP_MAPPING_REFRESH_INTERVAL_MIN"
_ENV_STALE = "PARALLEL_LDA_MVP_MAPPING_MAX_STALENESS_MIN"
_ENV_HARD_STALE = "PARALLEL_LDA_MVP_MAPPING_HARD_STALE_LIMIT_MIN"
_ENV_LATE_HOURS = "PARALLEL_LDA_MVP_LATE_ARRIVAL_RECOMPUTE_HOURS"

_DEFAULT_MAPPING_REFRESH_INTERVAL_MIN = 15
_DEFAULT_MAPPING_MAX_STALENESS_MIN = 30
# L1 與未來 L2 分界：超過此年齡仍允許服務讀舊 snapshot，但標 degrade_level=2（L2 行為待實作）
_DEFAULT_MAPPING_HARD_STALE_LIMIT_MIN = 240
_DEFAULT_LATE_ARRIVAL_RECOMPUTE_HOURS = 72


def _read_nonneg_int(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    v = int(raw)
    if v < 0:
        raise ValueError(f"{env_name} must be >= 0, got {v!r}")
    return v


MAPPING_REFRESH_INTERVAL_MIN: Final[int] = _read_nonneg_int(
    _ENV_REFRESH, _DEFAULT_MAPPING_REFRESH_INTERVAL_MIN
)
MAPPING_MAX_STALENESS_MIN: Final[int] = _read_nonneg_int(
    _ENV_STALE, _DEFAULT_MAPPING_MAX_STALENESS_MIN
)
MAPPING_HARD_STALE_LIMIT_MIN: Final[int] = _read_nonneg_int(
    _ENV_HARD_STALE, _DEFAULT_MAPPING_HARD_STALE_LIMIT_MIN
)
LATE_ARRIVAL_RECOMPUTE_HOURS: Final[int] = _read_nonneg_int(
    _ENV_LATE_HOURS, _DEFAULT_LATE_ARRIVAL_RECOMPUTE_HOURS
)


def late_arrival_window_days() -> int:
    """將 ``LATE_ARRIVAL_RECOMPUTE_HOURS`` 換算為日數（至少 1），供 month_bet_sha 政策使用。"""
    h = int(LATE_ARRIVAL_RECOMPUTE_HOURS)
    return max(1, (h + 23) // 24)
