# Marks trainer/ as a regular Python package so that
# `from trainer.time_fold import ...` works from the project root.
#
# 項目 2.3 相容層：re-export config / db_conn 讓既有 "from trainer.config import ..."
# 與 "from trainer import config" 仍可工作（config 實作在 trainer.core，頂層 trainer.config 為 re-export）。
# 安裝為 walkaway_ml 時須先設 trainer alias，再 import，否則 trainer.core 無法解析（Code Review P0）。
import sys

if __name__ == "walkaway_ml":
    sys.modules["trainer"] = sys.modules["walkaway_ml"]
from trainer import config  # noqa: F401  # re-export for "from trainer import config"
from trainer import db_conn  # noqa: F401  # re-export for "from trainer import db_conn"
