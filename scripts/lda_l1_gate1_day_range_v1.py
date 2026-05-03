#!/usr/bin/env python3
"""Thin entrypoint for the L1 day-range orchestrator.

Canonical implementation:
``pipelines.layered_data_assets.cli.lda_l1_gate1_day_range_v1``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipelines.layered_data_assets.cli.lda_l1_gate1_day_range_v1 import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
