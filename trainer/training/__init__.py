# trainer.training — 項目 2.2：training 子包（trainer、time_fold、backtester、threshold_selection）
#
# 不在此檔頂層 `import` 子模組，以免與 backtester ↔ trainer 的循環相依衝突。
# `import trainer.training.threshold_selection` 的 bytecode 會對本包做 getattr("threshold_selection")，
# 故以 PEP 562 + importlib.import_module 延遲載入（與上層 walkaway_ml / trainer 之 __getattr__ 策略一致）。
from __future__ import annotations

import importlib
import sys
from typing import Any

_LAZY = frozenset({"trainer", "backtester", "time_fold", "threshold_selection"})


def __getattr__(name: str) -> Any:
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    t_key = f"trainer.training.{name}"
    w_key = f"walkaway_ml.training.{name}"
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
    return sorted(set(globals()) | set(_LAZY))
