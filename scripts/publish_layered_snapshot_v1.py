"""Shim: ``python -m pipelines.layered_data_assets.cli.publish_layered_snapshot_v1``."""
from __future__ import annotations

import sys

from pipelines.layered_data_assets.cli.publish_layered_snapshot_v1 import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
