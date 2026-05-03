#!/usr/bin/env python3
"""Thin entrypoint for L1 ``run_bet_map`` materialization.

Canonical implementation: ``pipelines.layered_data_assets.cli.materialize_run_bet_map_v1``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipelines.layered_data_assets.cli.materialize_run_bet_map_v1 import main

if __name__ == "__main__":
    raise SystemExit(main())
