#!/usr/bin/env python3
"""Thin entrypoint for L0 ingest.

Canonical implementation: ``pipelines.layered_data_assets.cli.l0_ingest``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipelines.layered_data_assets.cli.l0_ingest import main

if __name__ == "__main__":
    raise SystemExit(main())
