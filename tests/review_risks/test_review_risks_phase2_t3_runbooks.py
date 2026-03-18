"""
Phase 2 T3: Review/contract tests for runbooks and model_version format (Code Review T3 runbooks).

- get_model_version() return value matches documented format YYYYMMDD-HHMMSS-<git7|nogit>.
- Rollback runbook mentions MODEL_DIR (env/config) so operators know which directory to replace.
- Provenance query runbook mentions run name filter (runName / run_name) for MLflow search.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from trainer.training import trainer as trainer_mod

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLLBACK_RUNBOOK = _REPO_ROOT / "doc" / "phase2_model_rollback_runbook.md"
_QUERY_RUNBOOK = _REPO_ROOT / "doc" / "phase2_provenance_query_runbook.md"

# Documented model_version format: YYYYMMDD-HHMMSS-<git7 or nogit>
_MODEL_VERSION_PATTERN = re.compile(r"^\d{8}-\d{6}-([a-f0-9]{7}|nogit)$")


class TestGetModelVersionFormat(unittest.TestCase):
    """Code Review T3 §5: get_model_version() return matches documented format."""

    def test_get_model_version_matches_documented_format(self):
        """Return value must match YYYYMMDD-HHMMSS-<git7|nogit> (schema/runbook)."""
        value = trainer_mod.get_model_version()
        self.assertTrue(
            _MODEL_VERSION_PATTERN.match(value),
            f"get_model_version() returned {value!r}; expected format YYYYMMDD-HHMMSS-<git7|nogit> (Code Review T3 §5).",
        )


class TestRollbackRunbookMentionsModelDir(unittest.TestCase):
    """Code Review T3 §3: Rollback runbook must mention MODEL_DIR so operators know target directory."""

    def test_rollback_runbook_contains_model_dir(self):
        """Rollback runbook must mention MODEL_DIR (env or config) for rollback target."""
        self.assertTrue(_ROLLBACK_RUNBOOK.exists(), f"Runbook not found: {_ROLLBACK_RUNBOOK}")
        text = _ROLLBACK_RUNBOOK.read_text(encoding="utf-8")
        self.assertIn(
            "MODEL_DIR",
            text,
            "Rollback runbook must mention MODEL_DIR so operators know which directory to replace (Code Review T3 §3).",
        )


class TestProvenanceQueryRunbookMentionsRunNameFilter(unittest.TestCase):
    """Code Review T3 §1: Provenance query runbook must document run name filter for search_runs."""

    def test_query_runbook_contains_run_name_filter_hint(self):
        """Runbook must mention run name filter (runName or run_name) for MLflow search."""
        self.assertTrue(_QUERY_RUNBOOK.exists(), f"Runbook not found: {_QUERY_RUNBOOK}")
        text = _QUERY_RUNBOOK.read_text(encoding="utf-8")
        has_hint = "runName" in text or "run_name" in text or "Run Name" in text
        self.assertTrue(
            has_hint,
            "Provenance query runbook must mention run name filter (runName/run_name/Run Name) for search (Code Review T3 §1).",
        )
