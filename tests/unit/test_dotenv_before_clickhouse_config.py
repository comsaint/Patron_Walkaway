from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_MODS_TO_RELOAD = (
    "trainer.core.config",
    "trainer.config",
    "trainer.core._config_clickhouse_sources",
)


def _pop_config_modules() -> None:
    for name in _MODS_TO_RELOAD:
        sys.modules.pop(name, None)


def _restore_env_and_config(saved: dict[str, str | None]) -> None:
    for key, val in saved.items():
        if val is not None:
            os.environ[key] = val
        elif key in os.environ:
            del os.environ[key]
    _pop_config_modules()
    import trainer.core.config  # noqa: F401 — restore real config for other tests


class TestDotenvBeforeClickhouseConfig(unittest.TestCase):
    """``credential/.env`` must load before ClickHouse shard binds ``CH_*``."""

    def test_credential_dotenv_applies_to_ch_user_pass(self) -> None:
        saved: dict[str, str | None] = {}
        for key in ("CH_USER", "CH_PASS"):
            saved[key] = os.environ.pop(key, None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cred_dir = root / "credential"
                cred_dir.mkdir(parents=True, exist_ok=True)
                (cred_dir / ".env").write_text(
                    "CH_USER=from_cred_dotenv_user\nCH_PASS=from_cred_dotenv_pass\n",
                    encoding="utf-8",
                )

                _pop_config_modules()

                import trainer.core._dotenv_bootstrap as ddb

                with patch.object(ddb, "_REPO_ROOT", root):
                    import trainer.core.config as core_config

                self.assertEqual(core_config.CH_USER, "from_cred_dotenv_user")
                self.assertEqual(core_config.CH_PASS, "from_cred_dotenv_pass")
        finally:
            _restore_env_and_config(saved)

    def test_existing_ch_user_not_overridden_by_credential_dotenv(self) -> None:
        """``load_dotenv(..., override=False)``: process env wins over credential file."""
        saved: dict[str, str | None] = {}
        for key in ("CH_USER", "CH_PASS"):
            saved[key] = os.environ.pop(key, None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cred_dir = root / "credential"
                cred_dir.mkdir(parents=True, exist_ok=True)
                (cred_dir / ".env").write_text(
                    "CH_USER=from_cred_dotenv_user\nCH_PASS=from_cred_dotenv_pass\n",
                    encoding="utf-8",
                )

                _pop_config_modules()

                import trainer.core._dotenv_bootstrap as ddb

                os.environ["CH_USER"] = "from_process_env"
                os.environ["CH_PASS"] = "from_process_pass"
                with patch.object(ddb, "_REPO_ROOT", root):
                    import trainer.core.config as core_config

                self.assertEqual(core_config.CH_USER, "from_process_env")
                self.assertEqual(core_config.CH_PASS, "from_process_pass")
        finally:
            _restore_env_and_config(saved)


if __name__ == "__main__":
    unittest.main()
