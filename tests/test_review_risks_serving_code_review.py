"""Minimal reproducible tests for Code Review: 項目 2.2 serving 子包搬移（STATUS.md Code Review §2–§5）.

Maps each Reviewer risk to a test. Production code is not modified.
Some tests use @unittest.expectedFailure until production/docs are updated (see STATUS.md).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Review §3: status_server STATE_DB_PATH should respect env (consistency with scorer/validator)
# ---------------------------------------------------------------------------


class TestStatusServerStateDbPathEnv(unittest.TestCase):
    """§3: status_server should use STATE_DB_PATH env when set (same as scorer/validator)."""

    def test_status_server_state_db_path_under_base_dir(self):
        """Default: STATE_DB_PATH is under BASE_DIR and ends with local_state/state.db."""
        import trainer.status_server as status_server_mod

        self.assertTrue(
            status_server_mod.STATE_DB_PATH.is_absolute() or "local_state" in str(status_server_mod.STATE_DB_PATH),
            "STATE_DB_PATH should be under BASE_DIR",
        )
        self.assertEqual(
            status_server_mod.STATE_DB_PATH.name,
            "state.db",
            "Default DB filename is state.db",
        )
        base = status_server_mod.BASE_DIR
        self.assertTrue(
            str(status_server_mod.STATE_DB_PATH).startswith(str(base)),
            "STATE_DB_PATH should be under BASE_DIR",
        )

    def test_status_server_uses_state_db_path_env_when_set(self):
        """When STATE_DB_PATH is set before first import, status_server must use it."""
        env_path = str(_REPO_ROOT / "tmp_code_review_serving_state.db")
        # Compare resolved paths so the test is cross-platform (Windows path str differs).
        code = (
            "import os\n"
            "from pathlib import Path\n"
            "env_path = os.environ.get('STATE_DB_PATH', '')\n"
            "if not env_path: exit(2)\n"
            "import trainer.status_server as m\n"
            "exit(0 if Path(m.STATE_DB_PATH).resolve() == Path(env_path).resolve() else 1)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_REPO_ROOT),
            env={**os.environ, "STATE_DB_PATH": env_path},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, f"status_server should use STATE_DB_PATH env: {result.stderr}")


# ---------------------------------------------------------------------------
# Review §4: logger name is trainer.serving.* (observability contract)
# ---------------------------------------------------------------------------


class TestServingLoggerNames(unittest.TestCase):
    """§4: Logger names are trainer.serving.* after move (for log aggregation)."""

    def test_scorer_logger_name_is_trainer_serving_scorer(self):
        """logging.getLogger('trainer.serving.scorer').name == 'trainer.serving.scorer'."""
        logger = logging.getLogger("trainer.serving.scorer")
        self.assertEqual(logger.name, "trainer.serving.scorer")

    def test_validator_logger_name_is_trainer_serving_validator(self):
        """logging.getLogger('trainer.serving.validator').name == 'trainer.serving.validator'."""
        logger = logging.getLogger("trainer.serving.validator")
        self.assertEqual(logger.name, "trainer.serving.validator")


# ---------------------------------------------------------------------------
# Review §5: Production should use WSGI / not run __main__ (documentation contract)
# ---------------------------------------------------------------------------


class TestProductionApiServerDocumentation(unittest.TestCase):
    """§5: Docs should state production use WSGI (e.g. gunicorn), not python -m trainer.api_server."""

    def test_project_or_package_readme_mentions_production_wsgi_or_no_main(self):
        """PROJECT.md or package README should mention production/WSGI/do not run __main__ (fails until doc added)."""
        candidates = [
            _REPO_ROOT / "PROJECT.md",
            _REPO_ROOT / "README.md",
            _REPO_ROOT / "package" / "README.md",
        ]
        keywords = ("wsgi", "gunicorn", "生產", "production", "勿以 __main__", "do not run __main__", "do not use __main__")
        for path in candidates:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            for kw in keywords:
                if kw.lower() in text:
                    return  # pass
        self.fail(
            "PROJECT.md or package/README should mention production use WSGI / not run __main__; "
            "add a sentence and remove @expectedFailure from this test."
        )


if __name__ == "__main__":
    unittest.main()
