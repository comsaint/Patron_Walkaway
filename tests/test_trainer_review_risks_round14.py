"""tests/test_trainer_review_risks_round14.py
================================================
Guardrail tests for Round 14 review findings (R22-R27) in `trainer/trainer.py`.

This round is tests-only (no production changes), so known-bug guardrails are
marked with `unittest.expectedFailure` and will be flipped to normal asserts
once the implementation is fixed.
"""

from __future__ import annotations

import ast
import pathlib
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "training" / "trainer.py"
_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _get_func_node(name: str) -> ast.FunctionDef:
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in trainer.py")


class TestTrainerReviewRisksRound14(unittest.TestCase):
    def test_r22_clickhouse_pull_includes_history_buffer_for_track_human(self):
        """R22: bets pull should include pre-window history buffer for Track-B states."""
        load_func = _get_func_node("load_clickhouse_data")
        load_src = ast.get_source_segment(_SRC, load_func) or ""

        # Guardrail rule: bets query should not start exactly from %(start)s.
        # We expect something like %(start)s - INTERVAL ... for history context.
        self.assertIn(
            "payout_complete_dtm >= %(start)s - INTERVAL",
            load_src,
            msg="load_clickhouse_data bets window should include historical buffer before window_start.",
        )

    def test_r23_apply_dq_localizes_or_converts_payout_timezone(self):
        """R23: apply_dq must handle tz-aware boundaries without naive/aware TypeError."""
        dq_func = _get_func_node("apply_dq")
        dq_src = ast.get_source_segment(_SRC, dq_func) or ""

        # Minimal lint-like rule: payout_complete_dtm handling should include
        # timezone normalization via tz_localize or tz_convert.
        has_tz_fix = ("tz_localize(" in dq_src) or ("tz_convert(" in dq_src)
        self.assertTrue(
            has_tz_fix,
            msg="apply_dq should normalize payout_complete_dtm timezone before boundary comparison.",
        )

    def test_r24_split_assignment_avoids_to_period_on_tz_aware_series(self):
        """R24: split assignment should avoid dt.to_period on payout_complete_dtm."""
        run_func = _get_func_node("run_pipeline")
        run_src = ast.get_source_segment(_SRC, run_func) or ""

        # Guardrail: avoid `.dt.to_period("M")` directly on payout timestamp.
        self.assertNotIn(
            ".dt.to_period(\"M\")",
            run_src,
            msg="run_pipeline should avoid direct to_period on tz-aware payout_complete_dtm.",
        )

    def test_r25_canonical_mapping_cutoff_not_global_end(self):
        """R25: canonical mapping cutoff should be training-window end, not global end."""
        run_func = _get_func_node("run_pipeline")

        calls = [
            n
            for n in ast.walk(run_func)
            if isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Name) and n.func.id == "build_canonical_mapping")
                or (isinstance(n.func, ast.Attribute) and n.func.attr == "build_canonical_mapping")
            )
        ]
        self.assertTrue(calls, "run_pipeline should call build_canonical_mapping")

        found_train_end_cutoff = False
        for call in calls:
            for kw in call.keywords:
                if kw.arg == "cutoff_dtm":
                    # Guardrail: disallow direct cutoff_dtm=end.
                    if isinstance(kw.value, ast.Name) and kw.value.id != "end":
                        found_train_end_cutoff = True
                    # Accept expressions not literally `end`.
                    if not (isinstance(kw.value, ast.Name) and kw.value.id == "end"):
                        found_train_end_cutoff = True
        self.assertTrue(
            found_train_end_cutoff,
            msg="build_canonical_mapping cutoff_dtm should be derived from train split end, not global end.",
        )

    def test_r26_local_parquet_uses_pushdown_filters(self):
        """R26: load_local_parquet should use read_parquet filters for pushdown."""
        local_func = _get_func_node("load_local_parquet")

        read_calls = [
            n
            for n in ast.walk(local_func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "read_parquet"
        ]
        self.assertGreaterEqual(len(read_calls), 1, "load_local_parquet should call pd.read_parquet")

        has_filters = any(any(kw.arg == "filters" for kw in call.keywords) for call in read_calls)
        self.assertTrue(
            has_filters,
            msg="pd.read_parquet should use filters=... to avoid full table scans per chunk.",
        )

    def test_r27_process_chunk_fills_missing_canonical_id_from_player_id(self):
        """R27: process_chunk should fill canonical_id fallback after left merge."""
        proc_func = _get_func_node("process_chunk")

        has_fillna_fallback = False
        for node in ast.walk(proc_func):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Subscript):
                continue
            tgt = node.targets[0]
            if not (
                isinstance(tgt.value, ast.Name)
                and tgt.value.id == "bets"
                and isinstance(tgt.slice, ast.Constant)
                and tgt.slice.value == "canonical_id"
            ):
                continue

            # Match: bets["canonical_id"] = bets["canonical_id"].fillna(...)
            val = node.value
            if (
                isinstance(val, ast.Call)
                and isinstance(val.func, ast.Attribute)
                and val.func.attr == "fillna"
                and isinstance(val.func.value, ast.Subscript)
                and isinstance(val.func.value.value, ast.Name)
                and val.func.value.value.id == "bets"
                and isinstance(val.func.value.slice, ast.Constant)
                and val.func.value.slice.value == "canonical_id"
            ):
                arg_src = ast.get_source_segment(_SRC, val.args[0]) if val.args else ""
                if arg_src and "player_id" in arg_src:
                    has_fillna_fallback = True
                    break

        self.assertTrue(
            has_fillna_fallback,
            msg="process_chunk should fallback canonical_id to player_id when mapping miss occurs.",
        )


if __name__ == "__main__":
    unittest.main()

