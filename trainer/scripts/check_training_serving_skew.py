"""
Phase 2 T9: One-shot / manual training-serving skew check.

- Loads serving-side and training-side feature tables (CSV or Parquet).
- Merges on a common key column (default: id), compares feature columns.
- Outputs: list of columns with any mismatch, summary table; optional markdown file.

Run from repo root: python -m trainer.scripts.check_training_serving_skew --serving <path> --training <path> [--id-column id] [--output out/skew_check_report.md]
See doc/phase2_skew_check_runbook.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]


def _load_table(path: Path) -> pd.DataFrame:
    """Load CSV or Parquet by extension."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    suf = path.suffix.lower()
    if suf == ".csv":
        return pd.read_csv(path)
    if suf in (".parquet", ".pq"):
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported format: {path.suffix}. Use .csv or .parquet.")


def run_skew_check(
    serving_path: Path,
    training_path: Path,
    id_column: str = "id",
    output_path: Path | None = None,
) -> int:
    """
    Compare serving vs training feature tables on common key; report inconsistent columns.
    Returns 0 on success, 1 on failure.
    """
    serving_df = _load_table(serving_path)
    training_df = _load_table(training_path)

    if id_column not in serving_df.columns or id_column not in training_df.columns:
        print(f"Error: id column {id_column!r} missing in serving or training table.", file=sys.stderr)
        return 1

    # Merge on key (inner join: only rows present in both)
    merged = serving_df.merge(
        training_df,
        on=id_column,
        how="inner",
        suffixes=("_serving", "_training"),
    )
    if merged.empty:
        print("No common keys between serving and training; nothing to compare.", file=sys.stderr)
        return 1

    # After merge with suffixes, we have col_serving and col_training for each common column
    serving_cols = set(serving_df.columns) - {id_column}
    training_cols = set(training_df.columns) - {id_column}
    common_cols = serving_cols & training_cols

    inconsistent = []
    for col in sorted(common_cols):
        left_name = f"{col}_serving" if f"{col}_serving" in merged.columns else col
        right_name = f"{col}_training" if f"{col}_training" in merged.columns else col
        if left_name not in merged.columns or right_name not in merged.columns:
            continue
        left = merged[left_name]
        right = merged[right_name]
        try:
            diff = left.ne(right) & ~(left.isna() & right.isna())
        except Exception:
            diff = left != right
        n_diff = int(diff.sum())
        if n_diff > 0:
            inconsistent.append((col, n_diff))

    lines = [
        "# Training–Serving Skew Check Summary",
        "",
        f"Common keys: {len(merged)}",
        f"Columns compared: {len(common_cols)}",
        "",
        "## Inconsistent columns (serving != training)",
        "",
    ]
    if not inconsistent:
        lines.append("None.")
    else:
        lines.append("| Column | Mismatch count |")
        lines.append("|--------|----------------|")
        for col, count in inconsistent:
            lines.append(f"| {col} | {count} |")

    text = "\n".join(lines)
    print(text)

    if output_path is not None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        print(f"\nReport written to {output_path}", file=sys.stderr)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare serving vs training feature tables for skew (T9). One-shot/manual."
    )
    parser.add_argument("--serving", required=True, type=Path, help="Path to serving-side features (CSV or Parquet).")
    parser.add_argument("--training", required=True, type=Path, help="Path to training-side features (CSV or Parquet).")
    parser.add_argument("--id-column", default="id", help="Common key column name (default: id).")
    parser.add_argument("--output", type=Path, default=None, help="Optional output markdown path.")
    args = parser.parse_args()

    try:
        return run_skew_check(
            serving_path=args.serving,
            training_path=args.training,
            id_column=args.id_column,
            output_path=args.output,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
