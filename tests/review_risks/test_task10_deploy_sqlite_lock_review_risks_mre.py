"""Task 10 (deploy SQLite lock / deferred validator) reviewer risks -> MRE tests.

Tests-only: encodes STATUS.md Code Review #1–#7 without modifying production.
Behavior oracle ``reference_parse_deploy_validator_start_wait_timeout`` must stay
in sync with ``package/deploy/main.py::_deploy_validator_start_wait_timeout``.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from trainer.serving import scorer as sc

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DEPLOY_MAIN = REPO_ROOT / "package" / "deploy" / "main.py"
DEPLOY_DIST_MAIN = REPO_ROOT / "deploy_dist" / "main.py"


def _read_deploy_main(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _func_block(src: str, func_name: str) -> str:
    pattern = rf"^def {re.escape(func_name)}\("
    m = re.search(pattern, src, re.MULTILINE)
    if not m:
        return ""
    start = m.start()
    nxt = re.search(r"\n\ndef [A-Za-z_]\w*\(", src[start + 1 :])
    end = (start + 1 + nxt.start()) if nxt else len(src)
    return src[start:end]


def reference_parse_deploy_validator_start_wait_timeout(raw: str | None) -> float | None:
    """Mirror ``_deploy_validator_start_wait_timeout`` (no os.environ).

    Sync with ``package/deploy/main.py`` when that function changes.
    """
    _log = logging.getLogger("tests.task10.deploy_timeout_ref")
    if raw is None:
        return 600.0
    s = raw.strip()
    if not s or s.lower() in ("0", "none", "inf", "infinite"):
        return None
    try:
        v = float(s)
    except ValueError:
        _log.warning(
            "[deploy] Invalid DEPLOY_VALIDATOR_START_TIMEOUT_SECONDS=%r; using 600s",
            raw,
        )
        return 600.0
    if math.isnan(v):
        _log.warning(
            "[deploy] DEPLOY_VALIDATOR_START_TIMEOUT_SECONDS is NaN (%r); using 600s",
            raw,
        )
        return 600.0
    if math.isinf(v):
        return None
    if v == 0.0:
        return None
    if v < 0:
        return None
    return v


class TestReferenceParseDeployValidatorTimeout(unittest.TestCase):
    """Review #2 / #3 / #7: env parsing table (oracle must match production)."""

    def test_none_and_trim_and_special_strings(self) -> None:
        self.assertEqual(reference_parse_deploy_validator_start_wait_timeout(None), 600.0)
        self.assertIsNone(reference_parse_deploy_validator_start_wait_timeout(""))
        self.assertIsNone(reference_parse_deploy_validator_start_wait_timeout("   "))
        self.assertIsNone(reference_parse_deploy_validator_start_wait_timeout("0"))
        self.assertIsNone(reference_parse_deploy_validator_start_wait_timeout("NONE"))
        self.assertIsNone(reference_parse_deploy_validator_start_wait_timeout("Inf"))
        self.assertIsNone(reference_parse_deploy_validator_start_wait_timeout("infinite"))

    def test_risk2_zero_point_zero_means_infinite_wait_not_immediate_timeout(self) -> None:
        """#2: ``0.0`` must not mean ``wait(timeout=0)`` (validator starts instantly)."""
        self.assertIsNone(reference_parse_deploy_validator_start_wait_timeout("0.0"))
        self.assertIsNone(reference_parse_deploy_validator_start_wait_timeout(" 0.00 "))

    def test_risk3_nan_and_inf_numeric(self) -> None:
        self.assertEqual(reference_parse_deploy_validator_start_wait_timeout("nan"), 600.0)
        self.assertIsNone(reference_parse_deploy_validator_start_wait_timeout("inf"))
        self.assertIsNone(reference_parse_deploy_validator_start_wait_timeout("-inf"))

    def test_invalid_string_fallback_600(self) -> None:
        self.assertEqual(reference_parse_deploy_validator_start_wait_timeout("not-a-float"), 600.0)

    def test_negative_means_infinite(self) -> None:
        self.assertIsNone(reference_parse_deploy_validator_start_wait_timeout("-1"))
        self.assertIsNone(reference_parse_deploy_validator_start_wait_timeout(" -0.001 "))

    def test_positive_finite(self) -> None:
        self.assertEqual(reference_parse_deploy_validator_start_wait_timeout("600"), 600.0)
        self.assertEqual(reference_parse_deploy_validator_start_wait_timeout(" 30.5 "), 30.5)


class TestDeployMainSourceContract(unittest.TestCase):
    """Static contracts so parsing / mitigations do not regress without test updates."""

    def setUp(self) -> None:
        self._pkg = _read_deploy_main(PACKAGE_DEPLOY_MAIN)

    def test_risk2_contains_zero_point_zero_guard(self) -> None:
        self.assertIn("v == 0.0", self._pkg)

    def test_risk3_contains_isnan_isinf(self) -> None:
        self.assertIn("math.isnan(v)", self._pkg)
        self.assertIn("math.isinf(v)", self._pkg)

    def test_import_math_for_timeout_parse(self) -> None:
        # Early import block should include math (used by timeout parse).
        head = self._pkg[:800]
        self.assertRegex(head, r"(?m)^import math$")

    def test_validator_deferred_uses_event_wait(self) -> None:
        self.assertIn("def _run_validator_deferred(", self._pkg)
        self.assertIn("first_cycle_done.wait", self._pkg)

    def test_risk5_get_db_conn_has_no_busy_timeout_yet(self) -> None:
        """#5: documents that Flask path still lacks PRAGMA busy_timeout (fast-follow)."""
        block = _func_block(self._pkg, "get_db_conn")
        self.assertIn("def get_db_conn", block)
        self.assertNotIn("busy_timeout", block.lower())

    def test_risk4_no_low_timeout_warning_string(self) -> None:
        """#4: current prod does not warn on sub-5s timeout (behavior unchanged)."""
        self.assertNotIn("low timeout", self._pkg.lower())
        self.assertNotIn("below recommended", self._pkg.lower())


class TestPackageAndDeployDistTimeoutFnInSync(unittest.TestCase):
    """#7: deploy_dist must not drift from package/deploy for timeout parsing."""

    def test_timeout_function_blocks_match(self) -> None:
        pkg = _read_deploy_main(PACKAGE_DEPLOY_MAIN)
        dist = _read_deploy_main(DEPLOY_DIST_MAIN)
        a = _func_block(pkg, "_deploy_validator_start_wait_timeout")
        b = _func_block(dist, "_deploy_validator_start_wait_timeout")
        self.assertTrue(a.strip(), "package/deploy/main.py: missing timeout fn")
        self.assertTrue(b.strip(), "deploy_dist/main.py: missing timeout fn")
        self.assertEqual(
            a,
            b,
            "package/deploy/main.py and deploy_dist/main.py "
            "_deploy_validator_start_wait_timeout must match",
        )


@pytest.fixture
def tmp_state_db(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setattr(sc, "STATE_DB_PATH", db)
    return db


class TestRisk1FirstCycleEventNotSetIfLoadFailsBeforeLoop(unittest.TestCase):
    """#1: Event is only set inside the loop; pre-loop failure leaves it unset."""

    def test_load_dual_artifacts_raises_before_while(self) -> None:
        ev = threading.Event()
        with (
            patch.object(sc, "_check_numba_runtime_once"),
            patch.object(sc, "load_dual_artifacts", side_effect=RuntimeError("pre-loop")),
            patch.object(sc, "score_once"),
            patch.object(sc, "load_alert_history", return_value=set()),
        ):
            with self.assertRaises(RuntimeError):
                sc.run_scorer_loop(
                    interval_seconds=1,
                    lookback_hours=1,
                    once=True,
                    first_cycle_done=ev,
                )
        self.assertFalse(
            ev.is_set(),
            "MRE: first_cycle_done must stay clear when failing before first score_once",
        )


def test_risk1_regression_when_load_succeeds_event_still_set(tmp_state_db):
    """Control: normal path still sets the event (existing unit behavior)."""
    ev = threading.Event()
    fake_art = {
        "model_version": "t",
        "rated": None,
        "feature_list": [],
        "reason_code_map": {},
        "feature_spec": None,
    }
    with (
        patch.object(sc, "_check_numba_runtime_once"),
        patch.object(sc, "load_dual_artifacts", return_value=fake_art),
        patch.object(sc, "score_once"),
        patch.object(sc, "load_alert_history", return_value=set()),
    ):
        sc.run_scorer_loop(
            interval_seconds=1,
            lookback_hours=1,
            once=True,
            first_cycle_done=ev,
        )
    assert ev.is_set()
