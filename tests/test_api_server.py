"""tests/test_api_server.py
==========================
Unit tests for api_server (DB-only: static serving + four data APIs).

  GET /                  — serves main.html
  GET /main.html         — same
  GET /style.css         — serves style.css
  GET /script.js         — serves script.js
  GET /<path:filename>   — serves .js from frontend only (404 otherwise)
  GET /get_floor_status  — 200, returns layout or occupied
  GET /get_hc_history    — 200, returns list (or 500 on DB error)
  GET /get_validation    — 200, returns {"results": [...]}
  GET /get_alerts        — 200, returns {"alerts": [...]}

No model API (/health, /model_info, /score). All data from shared SQLite.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Import api_server safely — without adding trainer/ to sys.path.
# ---------------------------------------------------------------------------
import importlib as _il

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if "config" not in sys.modules:
    sys.modules["config"] = _il.import_module("trainer.config")

if "trainer.api_server" not in sys.modules:
    _il.import_module("trainer.api_server")

api_server = sys.modules["trainer.api_server"]  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers (kept for import by skipped model-API test modules round235/round242)
# ---------------------------------------------------------------------------

def _make_stub_artifacts(feature_list=None):
    """Stub for skipped tests that import this module."""
    return {"rated": None, "feature_list": feature_list or ["f1", "f2", "f3"], "model_version": "test-v0"}


def _score_payload(rows):
    """Stub for skipped tests that import this module."""
    return {"rows": rows}


# ---------------------------------------------------------------------------
# Test cases: static + four data APIs
# ---------------------------------------------------------------------------

class TestStaticRoutes(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()

    def test_index_returns_200(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)

    def test_main_html_returns_200(self):
        resp = self.client.get("/main.html")
        self.assertEqual(resp.status_code, 200)

    def test_style_css_returns_200(self):
        resp = self.client.get("/style.css")
        self.assertEqual(resp.status_code, 200)

    def test_script_js_returns_200(self):
        resp = self.client.get("/script.js")
        self.assertEqual(resp.status_code, 200)

    def test_frontend_module_non_js_returns_404(self):
        resp = self.client.get("/style.css")  # already served by route; use nonexistent .html
        # Actually /style.css is served by route. Use a path that hits frontend_module:
        resp = self.client.get("/nonexistent.html")
        self.assertEqual(resp.status_code, 404)

    def test_frontend_module_nonexistent_js_returns_404(self):
        resp = self.client.get("/definitely_not_there_12345.js")
        self.assertEqual(resp.status_code, 404)

    def test_frontend_module_path_traversal_returns_404(self):
        """Code Review §1 (P0): Paths containing '..' must return 404 to prevent path traversal."""
        for path in ("../config.py", "static/../../trainer/config.py", "foo/../bar.js", "a/../../b.js"):
            with self.subTest(path=path):
                resp = self.client.get("/" + path)
                self.assertEqual(resp.status_code, 404, f"Path {path!r} must return 404")
                # Ensure we do not leak content from outside FRONTEND_DIR (e.g. config)
                if resp.data:
                    self.assertNotIn(
                        b"DEFAULT_MODEL_DIR",
                        resp.data,
                        "Must not serve trainer config content via path traversal",
                    )


class TestGetFloorStatus(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()

    def test_returns_200(self):
        resp = self.client.get("/get_floor_status")
        self.assertEqual(resp.status_code, 200)

    def test_returns_json_with_occupied_or_layout(self):
        resp = self.client.get("/get_floor_status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsInstance(data, dict)
        # Primary path: layout from status_snapshots; fallback: occupied from CSV/sample
        self.assertTrue("layout" in data or "occupied" in data)


class TestGetHcHistory(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()

    def test_returns_200_or_500(self):
        resp = self.client.get("/get_hc_history")
        self.assertIn(resp.status_code, (200, 500))

    def test_returns_list_when_200(self):
        resp = self.client.get("/get_hc_history")
        if resp.status_code == 200:
            data = resp.get_json()
            self.assertIsInstance(data, list)


class TestGetValidation(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()

    def test_returns_200(self):
        resp = self.client.get("/get_validation")
        self.assertEqual(resp.status_code, 200)

    def test_returns_results_key(self):
        resp = self.client.get("/get_validation")
        data = resp.get_json()
        self.assertIn("results", data)
        self.assertIsInstance(data["results"], list)


class TestGetAlerts(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()

    def test_returns_200(self):
        resp = self.client.get("/get_alerts")
        self.assertEqual(resp.status_code, 200)

    def test_returns_alerts_key(self):
        resp = self.client.get("/get_alerts")
        data = resp.get_json()
        self.assertIn("alerts", data)
        self.assertIsInstance(data["alerts"], list)


class TestModelApiRemoved(unittest.TestCase):
    """After revert, /health, /model_info, POST /score must not exist (404)."""

    def setUp(self):
        self.client = api_server.app.test_client()

    def test_health_returns_404(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 404)

    def test_model_info_returns_404(self):
        resp = self.client.get("/model_info")
        self.assertEqual(resp.status_code, 404)

    def test_score_returns_404_or_405(self):
        # No POST /score route: Flask may return 404 (no route) or 405 (method not allowed)
        resp = self.client.post("/score", json={"rows": []})
        self.assertIn(resp.status_code, (404, 405))


if __name__ == "__main__":
    unittest.main()
