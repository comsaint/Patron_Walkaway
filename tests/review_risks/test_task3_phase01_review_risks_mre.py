"""
Task 3 Phase 0/1 review risks -> minimal reproducible tests/contracts.

Scope:
- package/deploy/main.py
- trainer/serving/validator.py

Tests only; no production code changes.
"""

from __future__ import annotations

import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_MAIN = _REPO_ROOT / "package" / "deploy" / "main.py"
_VALIDATOR = _REPO_ROOT / "trainer" / "serving" / "validator.py"


def _deploy_text() -> str:
    return _DEPLOY_MAIN.read_text(encoding="utf-8")


def _validator_text() -> str:
    return _VALIDATOR.read_text(encoding="utf-8")


class TestRisk1ApiTimingUsesWallClock(unittest.TestCase):
    """Risk #1: API stage timing currently uses datetime.now().timestamp()."""

    def test_alerts_and_validation_use_datetime_timestamp(self) -> None:
        text = _deploy_text()
        self.assertIn("t_query = datetime.now().timestamp()", text)
        self.assertIn("t_transform = datetime.now().timestamp()", text)
        self.assertIn('stage_seconds["api_query_alerts"] = datetime.now().timestamp() - t_query', text)
        self.assertIn(
            'stage_seconds["api_query_validation"] = datetime.now().timestamp() - t_query',
            text,
        )


class TestRisk2ApiPerfLogLevelDebug(unittest.TestCase):
    """Contract: API perf summary uses DEBUG so default INFO consoles stay quiet."""

    def test_api_perf_logs_at_debug(self) -> None:
        text = _deploy_text()
        self.assertIn('debug("[api][perf] top_hotspots: %s"', text)
        self.assertNotIn('info("[api][perf] top_hotspots: %s"', text)


class TestRisk3ApiTimingSharedStateNoLock(unittest.TestCase):
    """Risk #3: shared _API_STAGE_TIMINGS has no synchronization lock."""

    def test_no_lock_around_api_timing_store(self) -> None:
        text = _deploy_text()
        self.assertIn("_API_STAGE_TIMINGS", text)
        self.assertNotIn("threading.Lock(", text)
        self.assertNotIn("_API_STAGE_TIMINGS_LOCK", text)


class TestRisk4ParseKnownArgsSilentUnknown(unittest.TestCase):
    """Risk #4: unknown CLI args are ignored (warning only)."""

    def test_parse_known_args_and_warning_present(self) -> None:
        text = _deploy_text()
        self.assertIn("parse_known_args()", text)
        self.assertIn("Ignoring unrecognized argv", text)


class TestRisk5ValidatorPhase1SessionFetchContract(unittest.TestCase):
    """Risk #5: ensure Phase-1 contract is explicit and protected by test."""

    def test_validate_once_does_not_call_fetch_sessions(self) -> None:
        text = _validator_text()
        start = text.find("def validate_once(")
        self.assertNotEqual(start, -1)
        end = text.find("\ndef ", start + 1)
        block = text[start:end if end != -1 else len(text)]
        self.assertNotIn("fetch_sessions_by_canonical_id(", block)
        self.assertIn("session_cache_disabled", block)

    def test_validate_alert_row_signature_still_keeps_session_cache_param(self) -> None:
        text = _validator_text()
        self.assertIn("def validate_alert_row(", text)
        self.assertIn("session_cache: Dict[str, List[Dict]]", text)


class TestRisk6ValidatorPerfUsesPandasQuantile(unittest.TestCase):
    """Risk #6: validator perf summary currently uses pandas quantile."""

    def test_validator_perf_uses_series_quantile(self) -> None:
        text = _validator_text()
        start = text.find("def _emit_validator_perf_summary(")
        self.assertNotEqual(start, -1)
        end = text.find("\ndef ", start + 1)
        block = text[start:end if end != -1 else len(text)]
        self.assertIn('arr = pd.Series(hist, dtype="float64")', block)
        self.assertIn("arr.quantile(0.5)", block)
        self.assertIn("arr.quantile(0.95)", block)


if __name__ == "__main__":
    unittest.main()

