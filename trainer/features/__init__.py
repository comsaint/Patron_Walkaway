# trainer.features — 項目 2.2：features 子包（實作在 features.py）
from trainer.features.features import *  # noqa: F401, F403
from trainer.features.t_game_context import join_t_game_features_for_bets  # noqa: F401
# 底線名稱 "import *" 不匯出，測試與 etl 需用；顯式 re-export
from trainer.features.features import (  # noqa: F401
    _LOOKBACK_MAX_HOURS,
    _PROFILE_FEATURE_MIN_DAYS,
    _validate_feature_spec,
    _streak_lookback_numba,
    _run_boundary_lookback_numba,
)
