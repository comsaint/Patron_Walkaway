"""Unit tests for B+ Step 8 sampling helpers and minimal rated eval parquet loader."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trainer.training.trainer import (
    _load_rated_eval_split_from_parquet,
    _step8_resolve_sample_strategy,
    _step8_sample_in_memory_train,
    _write_skipped_optuna_manifest_for_libsvm,
)


class TestStep8ResolveSampleStrategy(unittest.TestCase):
    """``_step8_resolve_sample_strategy`` normalizes config strings."""

    def test_valid_strategies(self) -> None:
        self.assertEqual(_step8_resolve_sample_strategy("head"), "head")
        self.assertEqual(_step8_resolve_sample_strategy("TAIL"), "tail")
        self.assertEqual(_step8_resolve_sample_strategy(" head_tail "), "head_tail")

    def test_invalid_falls_back_to_head(self) -> None:
        self.assertEqual(_step8_resolve_sample_strategy("middle"), "head")
        self.assertEqual(_step8_resolve_sample_strategy(None), "head")


class TestStep8SampleInMemoryTrain(unittest.TestCase):
    """``_step8_sample_in_memory_train`` respects head / tail / head_tail."""

    def setUp(self) -> None:
        n = 20
        self.train = pd.DataFrame(
            {
                "label": (np.arange(n) % 3 == 0).astype(int),
                "f0": np.arange(n, dtype=float),
                "payout_complete_dtm": pd.date_range("2024-01-01", periods=n, freq="h"),
            }
        )

    def test_head_uses_first_rows(self) -> None:
        out = _step8_sample_in_memory_train(
            self.train, strategy="head", sample_n=5, default_cap=10
        )
        self.assertEqual(len(out), 5)
        self.assertEqual(int(out["f0"].iloc[0]), 0)

    def test_tail_uses_last_rows(self) -> None:
        out = _step8_sample_in_memory_train(
            self.train, strategy="tail", sample_n=5, default_cap=10
        )
        self.assertEqual(len(out), 5)
        self.assertEqual(int(out["f0"].iloc[-1]), 19)

    def test_head_tail_combines_ends(self) -> None:
        out = _step8_sample_in_memory_train(
            self.train, strategy="head_tail", sample_n=10, default_cap=10
        )
        self.assertGreaterEqual(len(out), 2)
        self.assertIn(0, set(out["f0"].astype(int).tolist()))


class TestLoadRatedEvalSplitFromParquet(unittest.TestCase):
    """Rated eval loader must keep ``payout_complete_dtm`` when present."""

    def test_loads_payout_and_filters_rated(self) -> None:
        df = pd.DataFrame(
            {
                "f0": [1.0, 2.0, 3.0],
                "label": [0, 1, 0],
                "is_rated": [True, False, True],
                "payout_complete_dtm": pd.to_datetime(
                    ["2024-01-01", "2024-01-02", "2024-01-03"]
                ),
            }
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "valid.parquet"
            pq.write_table(pa.Table.from_pandas(df, preserve_index=False), p)
            got = _load_rated_eval_split_from_parquet(p, ["f0"])
        self.assertEqual(len(got), 2)
        self.assertIn("payout_complete_dtm", got.columns)
        self.assertIn("f0", got.columns)


class TestWriteSkippedOptunaManifestForLibsvm(unittest.TestCase):
    """Skip manifest reasons must map to distinct objective_mode strings."""

    def test_gate_blocked_reason(self) -> None:
        sink: list[dict] = []
        _write_skipped_optuna_manifest_for_libsvm(
            sink, run_optuna=True, skipped_reason="libsvm_optuna_gate_blocked"
        )
        self.assertEqual(len(sink), 1)
        self.assertEqual(
            sink[0].get("optuna_hpo_objective_mode"),
            "skipped_libsvm_optuna_gate_blocked",
        )
        self.assertEqual(sink[0].get("optuna_hpo_skipped_reason"), "libsvm_optuna_gate_blocked")


if __name__ == "__main__":
    unittest.main()
