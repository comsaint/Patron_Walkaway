"""Review risks MRE — Task 11 rolling KPI (`kpi_now_hk`) follow-ups (STATUS Code Review).

Tests-only: no production changes.

Maps to STATUS.md «Code Review — Task 11 實作» rows:
  #2 source order (metrics before SQLite save),
  #4 parallel / early-now undercount (two rows, now between timestamps).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import trainer.serving.validator as validator_mod

HK_TZ = ZoneInfo("Asia/Hong_Kong")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_VALIDATOR_PY = _REPO_ROOT / "trainer" / "serving" / "validator.py"


def _validate_once_source_block() -> str:
    text = _VALIDATOR_PY.read_text(encoding="utf-8")
    a = text.index("def validate_once")
    b = text.index("def run_validator_loop", a)
    return text[a:b]


def test_risk2_validate_once_append_metrics_before_save_validation_results() -> None:
    """#2: `recorded_at` / KPI insert must not be ordered after SQLite save of full frame."""
    chunk = _validate_once_source_block()
    idx_metrics = chunk.index("_append_validator_metrics(")
    idx_save = chunk.index("save_validation_results(", idx_metrics)
    assert idx_metrics < idx_save, "KPI metrics should be computed/written before save_validation_results"


def test_risk2_kpi_now_hk_before_append_validator_metrics_call() -> None:
    """Task 11 anchor must precede the metrics call (regression if refactored)."""
    chunk = _validate_once_source_block()
    idx_kpi = chunk.index("kpi_now_hk = datetime.now(HK_TZ)")
    idx_metrics = chunk.index("_append_validator_metrics(", idx_kpi)
    assert idx_kpi < idx_metrics


def test_risk4_now_hk_between_two_validated_at_timestamps_excludes_later_row() -> None:
    """#4: If KPI `now` is taken before the last row finishes, later row is excluded.

    Documents undercount when parallel validation is introduced without moving `kpi_now_hk`.
    """
    t0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=HK_TZ)
    t1 = t0 + timedelta(minutes=1)
    t2 = t0 + timedelta(minutes=3)
    df = pd.DataFrame(
        {
            "validated_at": [t1.isoformat(), t2.isoformat()],
            "reason": ["MATCH", "MATCH"],
        }
    )
    now_mid = t0 + timedelta(minutes=2)
    _, _, tot = validator_mod._rolling_precision_by_validated_at(
        df, now_hk=now_mid, window=timedelta(hours=1)
    )
    assert tot == 1

    _, _, tot_both = validator_mod._rolling_precision_by_validated_at(
        df, now_hk=t2, window=timedelta(hours=1)
    )
    assert tot_both == 2
