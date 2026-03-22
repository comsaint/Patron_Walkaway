# Marks trainer/ as a regular Python package so that
# `from trainer.time_fold import ...` works from the project root.
#
# 項目 2.3 相容層：lazy re-export config / db_conn（避免 import trainer 時連帶拉重鏈）。
# 安裝為 walkaway_ml 時須先設 trainer alias，再 import，否則 trainer.core 無法解析（Code Review P0）。
# 並 re-export 子模組供 "from walkaway_ml import trainer" 等（tests/round 119, 123, 127, 140, 150, 160, 171, 174, 175, 213, 221, 256, 376, 389, serving_code_review）。
from __future__ import annotations

import sys
from typing import Any

if __name__ == "walkaway_ml":
    sys.modules["trainer"] = sys.modules["walkaway_ml"]


def __getattr__(name: str) -> Any:
    if name == "config":
        import trainer.config as cfg

        return cfg
    if name == "db_conn":
        import trainer.db_conn as dbc

        return dbc
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | {"config", "db_conn"})


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
