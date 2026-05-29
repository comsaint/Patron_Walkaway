"""CLI entry: demo numbers until real IO is wired."""

from __future__ import annotations

import argparse

import numpy as np

from trainer_hightier.config import HighTierObjectiveConfig
from trainer_hightier.eval import report_alert_rate_at_precision_floor


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="High-tier segment: precision floor → alert rate (skeleton demo)."
    )
    p.add_argument(
        "--min-precision",
        type=float,
        default=None,
        help="Override HighTierObjectiveConfig.min_precision",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    cfg = HighTierObjectiveConfig()
    mp = float(cfg.min_precision if args.min_precision is None else args.min_precision)

    # Tiny synthetic demo; replace with Parquet/DuckDB segment load later.
    rng = np.random.default_rng(0)
    y_true = rng.integers(0, 2, size=200, dtype=np.int8)
    y_score = rng.normal(0.0, 1.0, size=200) + 0.6 * y_true.astype(np.float64)

    rep = report_alert_rate_at_precision_floor(y_true, y_score, min_precision=mp)
    print("[Step 1] HighTierObjectiveConfig (defaults):", cfg)
    print("[Step 2] Effective min_precision:", mp)
    print("[Step 3] Report:", rep)


if __name__ == "__main__":
    main()
