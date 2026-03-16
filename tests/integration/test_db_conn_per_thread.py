"""Minimal reproducible tests for Code Review: ClickHouse 做法 1（per-thread client）.

Maps STATUS.md « Code Review：ClickHouse 做法 1 » risk points §1–§3, §6–§7 to tests.
Production code is not modified. All tests use mocks; no real ClickHouse required.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch


def _import_core_db_conn():
    import trainer.core.db_conn as m
    return m


# ---------------------------------------------------------------------------
# §6: Per-thread isolation — different threads get different client instances
# ---------------------------------------------------------------------------


class TestPerThreadClientIsolation(unittest.TestCase):
    """§6: Different threads must receive different client instances."""

    def setUp(self):
        self.db = _import_core_db_conn()
        self.db.get_clickhouse_client.cache_clear()

    def tearDown(self):
        self.db.get_clickhouse_client.cache_clear()

    def test_per_thread_different_client_instances(self):
        """Two threads each call get_clickhouse_client(); returned instances must differ."""
        mock_connect = MagicMock()
        mock_connect.get_client.side_effect = lambda *a, **k: MagicMock()

        with patch.object(self.db, "clickhouse_connect", mock_connect):
            results = []

            def collect_in_thread():
                c = self.db.get_clickhouse_client()
                results.append(c)

            t1 = threading.Thread(target=collect_in_thread)
            t2 = threading.Thread(target=collect_in_thread)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertEqual(len(results), 2)
            self.assertIsNot(results[0], results[1], "each thread must get a different client instance")


# ---------------------------------------------------------------------------
# §7: cache_clear() affects only current thread
# ---------------------------------------------------------------------------


class TestCacheClearOnlyCurrentThread(unittest.TestCase):
    """§7: cache_clear() must clear only the calling thread's client; other threads unchanged."""

    def setUp(self):
        self.db = _import_core_db_conn()
        self.db.get_clickhouse_client.cache_clear()

    def tearDown(self):
        self.db.get_clickhouse_client.cache_clear()

    def test_cache_clear_affects_only_current_thread(self):
        """Thread A keeps client after B clears; B gets new client after B's clear."""
        mock_connect = MagicMock()
        mock_connect.get_client.side_effect = lambda *a, **k: MagicMock()

        with patch.object(self.db, "clickhouse_connect", mock_connect):
            out = []

            def thread_a():
                c_a = self.db.get_clickhouse_client()
                out.append(("a1", c_a))
                ev_b_cleared.wait()
                c_a_again = self.db.get_clickhouse_client()
                out.append(("a2", c_a_again))

            def thread_b():
                c_b = self.db.get_clickhouse_client()
                out.append(("b1", c_b))
                self.db.get_clickhouse_client.cache_clear()
                ev_b_cleared.set()
                c_b_again = self.db.get_clickhouse_client()
                out.append(("b2", c_b_again))

            ev_b_cleared = threading.Event()
            ta = threading.Thread(target=thread_a)
            tb = threading.Thread(target=thread_b)
            ta.start()
            tb.start()
            ta.join()
            tb.join()

            by_key = dict(out)
            self.assertIs(by_key["a2"], by_key["a1"], "thread A must keep same client after B clears")
            self.assertIsNot(by_key["b2"], by_key["b1"], "thread B must get new client after its own clear")


# ---------------------------------------------------------------------------
# §3: get_client() failure does not cache; retry succeeds
# ---------------------------------------------------------------------------


class TestGetClientFailureDoesNotPolluteCache(unittest.TestCase):
    """§3: When get_client() raises, _thread_local is not set; next call retries and caches."""

    def setUp(self):
        self.db = _import_core_db_conn()
        self.db.get_clickhouse_client.cache_clear()

    def tearDown(self):
        self.db.get_clickhouse_client.cache_clear()

    def test_get_client_failure_then_retry_returns_same_cached_client(self):
        """First get_clickhouse_client() raises; second and third return same (cached) client."""
        mock_client = MagicMock()
        mock_connect = MagicMock()
        mock_connect.get_client.side_effect = [RuntimeError("network"), mock_client]

        with patch.object(self.db, "clickhouse_connect", mock_connect):
            with self.assertRaises(RuntimeError) as ctx:
                self.db.get_clickhouse_client()
            self.assertIn("network", str(ctx.exception))

            c2 = self.db.get_clickhouse_client()
            c3 = self.db.get_clickhouse_client()
            self.assertIs(c2, mock_client)
            self.assertIs(c3, mock_client)
            self.assertIs(c2, c3)


# ---------------------------------------------------------------------------
# §2: After cache_clear(), same thread gets new client (config change scenario)
# ---------------------------------------------------------------------------


class TestAfterCacheClearSameThreadGetsNewClient(unittest.TestCase):
    """§2: After cache_clear(), next get_clickhouse_client() in same thread returns new instance."""

    def setUp(self):
        self.db = _import_core_db_conn()
        self.db.get_clickhouse_client.cache_clear()

    def tearDown(self):
        self.db.get_clickhouse_client.cache_clear()

    def test_after_cache_clear_same_thread_gets_new_client(self):
        """Same thread: get client c1, cache_clear(), get client c2; c2 must be a new object."""
        mock_connect = MagicMock()
        mock_connect.get_client.side_effect = lambda *a, **k: MagicMock()

        with patch.object(self.db, "clickhouse_connect", mock_connect):
            c1 = self.db.get_clickhouse_client()
            self.db.get_clickhouse_client.cache_clear()
            c2 = self.db.get_clickhouse_client()
            self.assertIsNot(c1, c2, "after cache_clear() same thread must get new client instance")


# ---------------------------------------------------------------------------
# §1 (optional): Fork/child can call cache_clear() without error
# ---------------------------------------------------------------------------


class TestForkChildCanCallCacheClear(unittest.TestCase):
    """§1 optional: In a fresh process (e.g. forked child), cache_clear() can be called without error."""

    def test_child_process_can_import_and_call_cache_clear(self):
        """Subprocess: import trainer.core.db_conn and call cache_clear(); must exit 0."""
        code = """
import trainer.core.db_conn as m
m.get_clickhouse_client.cache_clear()
"""
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=None,
        )
        self.assertEqual(proc.returncode, 0, f"child must exit 0; stderr: {proc.stderr!r}")
