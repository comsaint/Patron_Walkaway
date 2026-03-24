# Marks trainer/ as a regular Python package so that
# `from trainer.time_fold import ...` works from the project root.
#
# 項目 2.3：安裝為 walkaway_ml 時須先設 trainer alias，再 import，否則 trainer.core/fetch 無法解析（Code Review P0）。
#
# config / db_conn 為實體子模組，但 `import trainer.config` 的 bytecode 會對父包做 getattr("config")。
# __getattr__ 內若寫 `import trainer.config` 會再次觸發同一 getattr，與 trainer/walkaway_ml 互別後形成遞迴。
# 必須改以 importlib.import_module 載入；並讓 trainer.<sub> 與 walkaway_ml.<sub> 共用同一 sys.modules 物件，
# 否則 trainer.training.threshold_selection 與 walkaway_ml.training.threshold_selection 會變成兩份模組，patch 失效。
# 並 re-export 子模組供 "from walkaway_ml import trainer" 等（tests/round 119, 123, 127, 140, 150, 160, 171, 174, 175, 213, 221, 256, 376, 389, serving_code_review）。
from __future__ import annotations

import importlib
import sys
from typing import Any

if __name__ == "walkaway_ml":
    sys.modules["trainer"] = sys.modules["walkaway_ml"]


_LAZY_TOP = frozenset({"config", "db_conn", "training", "serving", "etl", "scripts"})


def __getattr__(name: str) -> Any:
    if name not in _LAZY_TOP:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    t_key = f"trainer.{name}"
    w_key = f"walkaway_ml.{name}"
    if t_key in sys.modules:
        mod = sys.modules[t_key]
    elif w_key in sys.modules:
        mod = sys.modules[w_key]
    else:
        mod = importlib.import_module(t_key)
    sys.modules[t_key] = mod
    sys.modules[w_key] = mod
    return mod


def __dir__() -> list[str]:
    return sorted(
        set(globals()) | {"config", "db_conn", "training", "serving", "etl", "scripts"}
    )


if __name__ == "walkaway_ml":
    import trainer.trainer  # noqa: F401
    import trainer.backtester  # noqa: F401
    import trainer.scorer  # noqa: F401
    import trainer.validator  # noqa: F401
    import trainer.status_server  # noqa: F401
    import trainer.api_server  # noqa: F401
    import trainer.features.features  # noqa: F401
    import trainer.etl.etl_player_profile  # noqa: F401
    import trainer.identity  # noqa: F401
    import trainer.core  # noqa: F401
    _g = globals()
    _g["trainer"] = sys.modules["trainer.trainer"]
    _g["backtester"] = sys.modules["trainer.backtester"]
    _g["scorer"] = sys.modules["trainer.scorer"]
    _g["validator"] = sys.modules["trainer.validator"]
    _g["status_server"] = sys.modules["trainer.status_server"]
    _g["api_server"] = sys.modules["trainer.api_server"]
    _g["features"] = sys.modules["trainer.features.features"]
    _g["etl_player_profile"] = sys.modules["trainer.etl.etl_player_profile"]
    _g["identity"] = sys.modules["trainer.identity"]
    _g["core"] = sys.modules["trainer.core"]
