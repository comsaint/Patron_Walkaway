"""tests/test_review_risks_round110.py
======================================
Minimal reproducible guardrail tests for Review Round 24 findings (R113-R115).

Tests-only: no production code changes.
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from trainer import trainer as trainer_mod

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "training" / "trainer.py"
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_TRAINER_TREE = ast.parse(_TRAINER_SRC)


def _get_func_src(tree: ast.AST, src: str, name: str) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


class TestR113NoDataEndOvershoot(unittest.TestCase):
    """R113: local data end adjustment must not overshoot to next-day midnight."""

    def test_run_pipeline_local_data_end_avoids_overshoot(self):
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "run_pipeline")
        self.assertGreater(len(src), 0, "run_pipeline not found")
        self.assertNotRegex(
            src,
            r"data_end\s*\+\s*timedelta\s*\(\s*days\s*=\s*1\s*\)",
            "R113: run_pipeline should not set end to data_end + 1 day; "
            "this can create an artificial empty tail and contaminate H1 labels",
        )


class TestR114TimezoneAwareMetadataDate(unittest.TestCase):
    """R114: _parse_obj_to_date should convert aware datetime to HK date first."""

    def test_parse_obj_to_date_respects_timezone(self):
        v = datetime(2026, 2, 13, 22, 0, 0, tzinfo=timezone.utc)
        expected = v.astimezone(trainer_mod.HK_TZ).date()
        got = trainer_mod._parse_obj_to_date(v)
        self.assertEqual(
            got,
            expected,
            "R114: timezone-aware parquet stats datetime must be converted to HK_TZ "
            "before taking date()",
        )


class TestR115PartialMetadataFallback(unittest.TestCase):
    """R115: _detect_local_data_end should gracefully handle one-side metadata miss."""

    def test_detect_local_data_end_handles_partial_metadata(self):
        with patch(
            "trainer.trainer._parquet_date_range",
            side_effect=[None, (date(2026, 1, 1), date(2026, 1, 31))],
        ):
            got = trainer_mod._detect_local_data_end()
        self.assertEqual(
            got,
            date(2026, 1, 31),
            "R115: when one table metadata is missing, _detect_local_data_end should "
            "still return the available max date (graceful fallback)",
        )


if __name__ == "__main__":
    unittest.main()
