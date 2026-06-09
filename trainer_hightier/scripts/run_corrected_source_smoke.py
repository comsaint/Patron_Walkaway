"""Post-rebuild integration smoke for corrected cleaned-bet source (INCIDENT guardrails)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
_ART = _REPO / "trainer_hightier" / "artifacts"


def _load_step06() -> Any:
    """Load Step 06 parity module for raw-source sanity."""
    step06_path = _REPO / "trainer_hightier" / "06_verify_training_serving_parity.py"
    spec = importlib.util.spec_from_file_location("step06_smoke", step06_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {step06_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _footer_rows(dataset_root: Path) -> int:
    """Row count from partitioned parquet footers."""
    from trainer_hightier.utils.bet_l0_preprocess import _partitioned_parquet_footer_row_count

    return int(_partitioned_parquet_footer_row_count(dataset_root))


def _training_set_summary(parquet_path: Path) -> dict[str, Any]:
    """Min/max gaming_day_event and row count for training_set.parquet."""
    p = parquet_path.resolve().as_posix().replace("'", "''")
    con = duckdb.connect(database=":memory:")
    try:
        row = con.execute(
            f"""
            SELECT
              COUNT(*)::BIGINT AS n_rows,
              MIN(CAST(gaming_day_event AS DATE)) AS min_day,
              MAX(CAST(gaming_day_event AS DATE)) AS max_day,
              COUNT(DISTINCT CAST(gaming_day_event AS DATE))::BIGINT AS n_distinct_days
            FROM read_parquet('{p}')
            """,
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise ValueError(f"empty summary for {parquet_path}")
    return {
        "n_rows": int(row[0]),
        "min_gaming_day_event": str(row[1]),
        "max_gaming_day_event": str(row[2]),
        "n_distinct_gaming_days": int(row[3]),
    }


def _june_2026_bet_probe(cleaned_root: Path) -> dict[str, Any]:
    """Incident probe month: row / distinct-bet coverage in June 2026 cleaned bets."""
    from trainer_hightier.utils.cleaned_bet_pool_read import cleaned_bet_dataset_glob_posix

    glo = cleaned_bet_dataset_glob_posix(cleaned_root).replace("'", "''")
    con = duckdb.connect(database=":memory:")
    try:
        row = con.execute(
            f"""
            SELECT
              COUNT(*)::BIGINT AS n_rows,
              COUNT(DISTINCT bet_id)::BIGINT AS n_distinct_bets
            FROM read_parquet('{glo}', hive_partitioning=false) AS _
            WHERE CAST(gaming_day_event AS DATE) >= DATE '2026-06-01'
              AND CAST(gaming_day_event AS DATE) < DATE '2026-07-01'
            """,
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise ValueError("june probe returned no row")
    return {
        "n_rows": int(row[0]),
        "n_distinct_bets": int(row[1]),
    }


def _run_raw_sanity_on_test_split() -> dict[str, Any]:
    """Live raw-source sanity on subsampled test split."""
    from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path
    from trainer_hightier.utils.partition_inventory import default_partition_snapshot_dir

    step06 = _load_step06()
    test_p = _ART / "training_data" / "splits" / "test.parquet"
    if not test_p.is_file():
        return {"verdict": "skipped", "issues": [f"missing test split: {test_p}"]}
    test = pd.read_parquet(test_p)
    col = step06.RAW_W1H_SANITY_COLUMN
    if col not in test.columns:
        return {"verdict": "skipped", "issues": [f"test split missing {col!r}"]}
    raw_dir = default_partition_snapshot_dir(repo_root=_REPO)
    if raw_dir is None:
        return {"verdict": "skipped", "issues": ["default partition snapshot dir missing"]}
    return step06.run_raw_source_w1h_sanity_check(
        test,
        raw_partition_dir=raw_dir,
        mapping_parquet=default_canonical_mapping_parquet_path(),
        max_rows=200,
    )


def _evaluate_checks(payload: dict[str, Any]) -> list[str]:
    """Return list of failed check messages (empty = all pass)."""
    fails: list[str] = []
    cleaned = payload["cleaned_row_counts"]
    base_rows = int(cleaned["cleaned__gmwds_t_bet_base"])
    seg_rows = int(cleaned["cleaned__gmwds_t_bet"])
    if base_rows < 1_000_000:
        fails.append(f"cleaned base row count suspiciously low: {base_rows}")
    if seg_rows < 1_000_000:
        fails.append(f"cleaned segment row count suspiciously low: {seg_rows}")
    ratio = seg_rows / max(base_rows, 1)
    if ratio < 0.05:
        fails.append(f"segment/base ratio {ratio:.4f} looks like incident row-loss (<5%)")

    june = payload["june_2026_probe"]
    if int(june["n_rows"]) < 1_000:
        fails.append(
            f"June 2026 cleaned rows {june['n_rows']} too low (incident-scale row loss)",
        )

    raw = payload["raw_source_w1h_sanity_live"]
    if raw.get("verdict") == "fail":
        fails.append(f"raw_source_w1h_sanity fail: {raw.get('issues')}")
    if raw.get("verdict") == "pass" and int(raw.get("n_rows_compared", 0)) == 0:
        fails.append("raw_source_w1h_sanity passed with n_rows_compared=0 (incident blind spot)")

    ts = payload["training_set"]
    min_day = date.fromisoformat(str(ts["min_gaming_day_event"]))
    if min_day > date(2026, 3, 1):
        fails.append(
            f"training_set min day {min_day} after 2026-03-01; possible erroneous horizon on assembly",
        )
    return fails


def main() -> int:
    """Run smoke checks and write JSON report under ``out/``."""
    out_dir = _REPO / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "corrected_source_integration_smoke.json"

    cleaned_base = _ART / "cleaned" / "cleaned__gmwds_t_bet_base"
    cleaned_seg = _ART / "cleaned" / "cleaned__gmwds_t_bet"
    training_p = _ART / "training_data" / "training_set.parquet"

    payload: dict[str, Any] = {
        "schema_version": "corrected_source_integration_smoke_v1",
        "cleaned_row_counts": {
            "cleaned__gmwds_t_bet_base": _footer_rows(cleaned_base),
            "cleaned__gmwds_t_bet": _footer_rows(cleaned_seg),
        },
        "training_set": _training_set_summary(training_p),
        "june_2026_probe": _june_2026_bet_probe(cleaned_seg),
        "raw_source_w1h_sanity_live": _run_raw_sanity_on_test_split(),
    }
    fails = _evaluate_checks(payload)
    payload["checks_failed"] = fails
    payload["verdict"] = "fail" if fails else "pass"

    report_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps(payload, indent=2, default=str))
    if fails:
        print("\nSMOKE FAIL:", file=sys.stderr)
        for msg in fails:
            print(f"  - {msg}", file=sys.stderr)
        return 1
    print("\nSMOKE PASS", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
