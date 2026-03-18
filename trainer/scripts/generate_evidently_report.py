"""
Phase 2 T8: Manual/ad-hoc Evidently DQ/drift report generation.

- Reads reference and current datasets (CSV or Parquet).
- Generates a data drift report (HTML) using Evidently DataDriftPreset.
- Output directory default: out/evidently_reports (configurable via --output-dir).
- OOM risk: large inputs may cause OutOfMemory; use downsampled or aggregated data.
- If evidently is not installed, exits with a clear message (exit 1).

Run from repo root: python -m trainer.scripts.generate_evidently_report --reference <path> --current <path> [--output-dir out/evidently_reports]
See doc/phase2_evidently_usage.md for usage and OOM warning.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]


def _load_table(path: Path) -> pd.DataFrame:
    """Load CSV or Parquet into DataFrame by extension."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    suf = path.suffix.lower()
    if suf == ".csv":
        return pd.read_csv(path)
    if suf in (".parquet", ".pq"):
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported format: {path.suffix}. Use .csv or .parquet.")


def run_evidently_report(
    reference_path: Path,
    current_path: Path,
    output_dir: Path,
) -> int:
    """
    Generate Evidently data drift report (HTML) from reference and current data.
    Returns 0 on success, 1 on failure (e.g. evidently not installed, OOM).
    """
    try:
        from evidently import Report  # type: ignore[import-not-found]
        from evidently.presets import DataDriftPreset  # type: ignore[import-not-found]
    except ImportError:
        print(
            "evidently is not installed. Install it with: pip install evidently\n"
            "See doc/phase2_evidently_usage.md for usage.",
            file=sys.stderr,
        )
        return 1

    print(
        "OOM risk: large reference/current data may cause OutOfMemory. "
        "Use downsampled or aggregated inputs. See doc/phase2_evidently_usage.md."
    )

    reference_df = _load_table(reference_path)
    current_df = _load_table(current_path)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    report = Report(metrics=[DataDriftPreset()])
    result = report.run(reference_data=reference_df, current_data=current_df)

    out_html = output_dir / "data_drift_report.html"
    result.save_html(str(out_html))
    print(f"Report saved to {out_html}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Evidently data drift report (T8). Manual/ad-hoc only."
    )
    parser.add_argument(
        "--reference",
        required=True,
        type=Path,
        help="Path to reference dataset (CSV or Parquet).",
    )
    parser.add_argument(
        "--current",
        required=True,
        type=Path,
        help="Path to current dataset (CSV or Parquet).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("out/evidently_reports"),
        help="Output directory for report HTML (default: out/evidently_reports).",
    )
    args = parser.parse_args()

    try:
        return run_evidently_report(
            reference_path=args.reference,
            current_path=args.current,
            output_dir=args.output_dir,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
