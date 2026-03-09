"""Round 238 Code Review — 步驟 5（統一 503 body）風險點轉成測試。

STATUS.md Round 238 Code Review: convert reviewer risk points to minimal
reproducible tests only. No production code changes.

Reference: PLAN § api_server 對齊 model_api_protocol 步驟 5, STATUS Round 238 Review.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if "config" not in sys.modules:
    import importlib as _il
    sys.modules["config"] = _il.import_module("trainer.config")
if "trainer.api_server" not in sys.modules:
    import importlib as _il
    _il.import_module("trainer.api_server")

import trainer.api_server as api_server  # noqa: E402


class TestR238_1_ModelInfo503BodyContract(unittest.TestCase):
    """Review #1: GET /model_info 無 artifact 時 503 body 必須為 protocol §5 契約."""

    def test_model_info_503_error_must_be_model_not_ready(self):
        """When _get_artifacts returns None, GET /model_info must return 503 with error \"model not ready\"."""
        with patch.object(api_server, "_get_artifacts", return_value=None):
            resp = api_server.app.test_client().get("/model_info")
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertIn("error", data)
        self.assertEqual(data["error"], "model not ready")


if __name__ == "__main__":
    unittest.main()
