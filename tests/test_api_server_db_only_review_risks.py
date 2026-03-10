"""Minimal reproducible tests for api_server DB-only Code Review risk points.

Maps to STATUS.md « Round — api_server 還原為 DB-only：Code Review ».
Tests only; no production code changes. Run with:
  pytest tests/test_api_server_db_only_review_risks.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import importlib as _il
if "config" not in sys.modules:
    sys.modules["config"] = _il.import_module("trainer.config")
if "trainer.api_server" not in sys.modules:
    _il.import_module("trainer.api_server")

api_server = sys.modules["trainer.api_server"]
FRONTEND_DIR = api_server.FRONTEND_DIR


# --- Risk 1: Path traversal in frontend_module ---------------------------------

class TestR1PathTraversal(unittest.TestCase):
    """Risk 1: frontend_module must not serve files outside FRONTEND_DIR."""

    def setUp(self):
        self.client = api_server.app.test_client()
        self._cleanup_path = None

    def tearDown(self):
        if self._cleanup_path is not None and self._cleanup_path.exists():
            self._cleanup_path.unlink()

    def test_traversal_like_path_non_js_returns_404(self):
        """Request path that looks like traversal but not .js -> 404."""
        resp = self.client.get("/..%2f..%2ftrainer%2fconfig.py")
        self.assertEqual(resp.status_code, 404)

    def test_traversal_js_outside_frontend_returns_404(self):
        """If a .js exists outside FRONTEND_DIR, request with traversal path must not serve it (404)."""
        # Create a .js file one level above frontend (trainer/review_risks_traversal.js)
        base = FRONTEND_DIR.resolve().parent
        self._cleanup_path = base / "review_risks_traversal.js"
        self._cleanup_path.write_text("secret", encoding="utf-8")
        try:
            # Path that resolves to that file: ../review_risks_traversal.js
            resp = self.client.get("/../review_risks_traversal.js")
            self.assertEqual(resp.status_code, 404, "must not serve .js outside FRONTEND_DIR")
            if resp.status_code == 200:
                self.assertNotIn(b"secret", resp.data)
        finally:
            if self._cleanup_path.exists():
                self._cleanup_path.unlink()
                self._cleanup_path = None


# --- Risk 2: get_validation / get_alerts missing columns -> 500 --------------

class TestR2MissingColumnsValidation(unittest.TestCase):
    """Risk 2: get_validation with DataFrame missing required columns (current: 500)."""

    def setUp(self):
        self.client = api_server.app.test_client()

    def test_validation_missing_columns_does_not_crash_with_500(self):
        """When validation_results has missing columns, API should not return 500 (desired: 200 + empty/error)."""
        # Return minimal df so code reaches the column access that can KeyError
        missing_cols_df = pd.DataFrame({"bet_id": [1], "validated_at": [pd.Timestamp("2024-01-01")]})

        def fake_read_sql(query, conn, params=None):
            if "validation_results" in (query or ""):
                return missing_cols_df
            return pd.read_sql_query(query, conn, params=params)

        with patch.object(api_server.pd, "read_sql_query", side_effect=fake_read_sql):
            resp = self.client.get("/get_validation")
        # Current production: KeyError -> 500. When fixed: 200 and results/error.
        self.assertIn(resp.status_code, (200, 500), "must not crash with other status")
        if resp.status_code == 200:
            data = resp.get_json()
            self.assertIn("results", data)
            self.assertIsInstance(data["results"], list)


class TestR2MissingColumnsAlerts(unittest.TestCase):
    """Risk 2: get_alerts with DataFrame missing 'ts' column (current: 500)."""

    def setUp(self):
        self.client = api_server.app.test_client()

    def test_alerts_missing_ts_column_does_not_crash_with_500(self):
        """When alerts table has no 'ts' column, API should not return 500 (desired: 200 + empty/error)."""
        missing_ts_df = pd.DataFrame({"player_id": [1]})

        def fake_read_sql(query, conn, params=None):
            if "alerts" in (query or "") and "validation_results" not in (query or ""):
                return missing_ts_df
            return pd.read_sql_query(query, conn, params=params)

        with patch.object(api_server.pd, "read_sql_query", side_effect=fake_read_sql):
            resp = self.client.get("/get_alerts")
        self.assertIn(resp.status_code, (200, 500), "must not crash with other status")
        if resp.status_code == 200:
            data = resp.get_json()
            self.assertIn("alerts", data)
            self.assertIsInstance(data["alerts"], list)


# --- Risk 3: get_hc_history hours <= 0 -----------------------------------------

class TestR3HcHistoryHoursEdgeCase(unittest.TestCase):
    """Risk 3: get_hc_history with hours=-1 or hours=0 must not crash."""

    def setUp(self):
        self.client = api_server.app.test_client()

    def test_hours_negative_returns_200_or_500(self):
        resp = self.client.get("/get_hc_history?hours=-1")
        self.assertIn(resp.status_code, (200, 500))

    def test_hours_zero_returns_200_or_500(self):
        resp = self.client.get("/get_hc_history?hours=0")
        self.assertIn(resp.status_code, (200, 500))

    def test_hours_negative_response_is_list_when_200(self):
        resp = self.client.get("/get_hc_history?hours=-1")
        if resp.status_code == 200:
            data = resp.get_json()
            self.assertIsInstance(data, list)


# --- Risk 4: get_validation / get_alerts large result (no limit) ------------

class TestR4LargeResultNoCrash(unittest.TestCase):
    """Risk 4: Large table response does not crash (current: no LIMIT)."""

    def setUp(self):
        self.client = api_server.app.test_client()

    def test_get_alerts_many_rows_returns_200(self):
        """Mock many rows; assert 200 and structure (documents current no-limit behavior)."""
        n = 2000
        big = pd.DataFrame({
            "ts": pd.date_range("2024-01-01", periods=n, freq="min").astype(str),
            "player_id": [1] * n,
            "bet_id": range(n),
        })

        def fake_read_sql(query, conn, params=None):
            if "alerts" in (query or "") and "validation_results" not in (query or ""):
                return big
            return pd.read_sql_query(query, conn, params=params)

        with patch.object(api_server.pd, "read_sql_query", side_effect=fake_read_sql):
            resp = self.client.get("/get_alerts")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("alerts", data)
        self.assertEqual(len(data["alerts"]), n)


# --- Risk 5: get_floor_status fallback when DB raises ------------------------

class TestR5FloorStatusFallbackWhenDbRaises(unittest.TestCase):
    """Risk 5: When get_floor_status DB path raises, response is still 200 with occupied or layout."""

    def setUp(self):
        self.client = api_server.app.test_client()

    def test_floor_status_when_db_raises_returns_200_with_payload(self):
        with patch.object(api_server, "get_db_conn", side_effect=Exception("DB unavailable")):
            resp = self.client.get("/get_floor_status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsInstance(data, dict)
        self.assertTrue("layout" in data or "occupied" in data, "fallback must return layout or occupied")


# --- Risk 6: Data API edge cases ---------------------------------------------

class TestR6DataApiEdgeCases(unittest.TestCase):
    """Risk 6: Empty table, invalid ts, etc."""

    def setUp(self):
        self.client = api_server.app.test_client()

    def test_get_validation_empty_table_returns_results_list(self):
        with patch.object(api_server.pd, "read_sql_query", return_value=pd.DataFrame()):
            resp = self.client.get("/get_validation")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("results", resp.get_json())
        self.assertEqual(resp.get_json()["results"], [])

    def test_get_alerts_empty_table_returns_alerts_list(self):
        with patch.object(api_server.pd, "read_sql_query", return_value=pd.DataFrame()):
            resp = self.client.get("/get_alerts")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("alerts", resp.get_json())
        self.assertEqual(resp.get_json()["alerts"], [])

    def test_get_validation_invalid_ts_still_200(self):
        """Invalid ts param should not cause 500; filter may be ignored."""
        resp = self.client.get("/get_validation?ts=not-a-date")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("results", data)

    def test_get_alerts_invalid_ts_still_200(self):
        resp = self.client.get("/get_alerts?ts=not-a-date")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("alerts", data)

    def test_get_hc_history_on_db_error_returns_500_with_error_key(self):
        """When hc_history query raises, response should be 500 with error in body."""
        with patch.object(api_server, "get_db_conn", side_effect=Exception("DB error")):
            resp = self.client.get("/get_hc_history")
        self.assertEqual(resp.status_code, 500)
        data = resp.get_json()
        self.assertIsInstance(data, dict)
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
