"""tests/test_profile_schema_hash.py
====================================
Guardrail tests for the profile schema fingerprint mechanism.

The player_profile_daily cache (local Parquet) can become stale if:
  1. PROFILE_FEATURE_COLS changes in features.py
  2. PROFILE_VERSION is bumped in etl_player_profile.py
  3. _SESSION_COLS changes in etl_player_profile.py

These tests verify that:
  a) compute_profile_schema_hash() actually changes when those inputs change.
  b) ensure_player_profile_daily_ready() detects a hash mismatch and clears
     the stale parquet + checkpoint before triggering a rebuild.
  c) The schema hash sidecar is written alongside the parquet after each
     successful _persist_local_parquet call.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestComputeProfileSchemaHash(unittest.TestCase):
    """Unit tests for compute_profile_schema_hash()."""

    def _call(self) -> str:
        from trainer.etl_player_profile import compute_profile_schema_hash
        return compute_profile_schema_hash()

    def test_returns_non_empty_hex_string(self):
        h = self._call()
        self.assertIsInstance(h, str)
        self.assertTrue(len(h) > 0)
        # MD5 hex digest is 32 chars
        self.assertEqual(len(h), 32)
        # All hex characters
        self.assertRegex(h, r"^[0-9a-f]+$")

    def test_deterministic(self):
        """Same environment → same hash on repeated calls."""
        h1 = self._call()
        h2 = self._call()
        self.assertEqual(h1, h2)

    def test_changes_when_profile_version_changes(self):
        """Bumping PROFILE_VERSION must change the fingerprint."""
        import trainer.etl_player_profile as etl
        original_version = etl.PROFILE_VERSION
        try:
            h_before = self._call()
            etl.PROFILE_VERSION = "v99.99-test"
            h_after = self._call()
            self.assertNotEqual(
                h_before, h_after,
                "Hash should change when PROFILE_VERSION changes",
            )
        finally:
            etl.PROFILE_VERSION = original_version

    def test_changes_when_profile_feature_cols_changes(self):
        """Adding/removing a column from PROFILE_FEATURE_COLS must change the fingerprint."""
        import trainer.features as feat
        original_cols = list(feat.PROFILE_FEATURE_COLS)
        try:
            h_before = self._call()
            feat.PROFILE_FEATURE_COLS = original_cols + ["__test_sentinel_col__"]
            h_after = self._call()
            self.assertNotEqual(
                h_before, h_after,
                "Hash should change when PROFILE_FEATURE_COLS changes",
            )
        finally:
            feat.PROFILE_FEATURE_COLS = original_cols

    def test_changes_when_session_cols_changes(self):
        """Adding/removing a column from _SESSION_COLS must change the fingerprint."""
        import trainer.etl_player_profile as etl
        original_cols = list(etl._SESSION_COLS)
        try:
            h_before = self._call()
            etl._SESSION_COLS = original_cols + ["__test_sentinel_session_col__"]
            h_after = self._call()
            self.assertNotEqual(
                h_before, h_after,
                "Hash should change when _SESSION_COLS changes",
            )
        finally:
            etl._SESSION_COLS = original_cols


class TestWriteLocalParquetWritesSidecar(unittest.TestCase):
    """Verify that _persist_local_parquet writes the .schema_hash sidecar."""

    def test_sidecar_written_alongside_parquet(self):
        """After a successful write, the sidecar file should exist and match current hash."""
        import hashlib
        import pandas as pd
        import trainer.etl_player_profile as etl

        df = pd.DataFrame({
            "canonical_id": ["C1"],
            "snapshot_date": ["2025-01-01"],
            "snapshot_dtm": [pd.Timestamp("2025-01-01")],
            "profile_version": ["v1.0"],
            "sessions_30d": [5],
        })

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # Patch module-level paths to point to our temp dir
            with (
                patch.object(etl, "LOCAL_PARQUET_DIR", tmp_path),
                patch.object(etl, "LOCAL_PROFILE_PARQUET", tmp_path / "player_profile_daily.parquet"),
                patch.object(etl, "LOCAL_PROFILE_SCHEMA_HASH", tmp_path / "player_profile_daily.schema_hash"),
            ):
                etl._persist_local_parquet(df)

            parquet_path = tmp_path / "player_profile_daily.parquet"
            hash_path = tmp_path / "player_profile_daily.schema_hash"

            self.assertTrue(parquet_path.exists(), "Parquet file should be written")
            self.assertTrue(hash_path.exists(), "Schema hash sidecar should be written")

            written_hash = hash_path.read_text(encoding="utf-8").strip()
            # R106: sidecar stores full hash (base + _pop_tag). No whitelist → _full.
            # R300: sidecar also encodes max_lookback_days; default is 365 → _mlb=365.
            # DEC-019 R601: sidecar also encodes sched_tag; default for
            # _persist_local_parquet is "_daily".
            base_hash = etl.compute_profile_schema_hash(
                session_parquet=tmp_path / "gmwds_t_session.parquet"
            )
            expected_hash = hashlib.md5((base_hash + "_full" + "_mlb=365" + "_daily").encode()).hexdigest()
            self.assertEqual(
                written_hash, expected_hash,
                "Sidecar should contain the current schema hash (with population tag + horizon tag + sched_tag)",
            )


class TestEnsureProfileReadySchemaMismatch(unittest.TestCase):
    """Verify ensure_player_profile_daily_ready() invalidates cache on hash mismatch."""

    def _run_ensure(self, tmp_dir: Path, stored_hash: str | None, parquet_exists: bool = True):
        """Helper: run ensure_player_profile_daily_ready with controlled sidecar state."""
        from datetime import datetime, timezone
        from trainer.trainer import ensure_player_profile_daily_ready

        profile_parquet = tmp_dir / "player_profile_daily.parquet"
        schema_hash_file = tmp_dir / "player_profile_daily.schema_hash"
        checkpoint_file = tmp_dir / "player_profile_etl_checkpoint.json"

        if parquet_exists:
            profile_parquet.write_bytes(b"fake parquet content")
        if stored_hash is not None:
            schema_hash_file.write_text(stored_hash, encoding="utf-8")
        checkpoint_file.write_text('{"last_success_date": "2025-01-01"}', encoding="utf-8")

        # Create a fake session parquet ONLY if no real one was placed there by the
        # caller (test_matching_hash writes a real parquet first — we must not
        # overwrite it, otherwise the stored_hash and current_hash would diverge).
        _sess = tmp_dir / "gmwds_t_session.parquet"
        if not _sess.exists():
            _sess.write_bytes(b"fake session parquet")

        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 3, 1, tzinfo=timezone.utc)

        import trainer.etl_player_profile as etl
        import trainer.trainer as trainer_mod

        # Patch only the specific modules that need redirecting — no global Path.exists patch.
        # etl.LOCAL_PARQUET_DIR must also be patched so compute_profile_schema_hash()
        # (called inside ensure) reads the session parquet from tmp_dir, not the real
        # workspace data directory (R101 hermeticity fix).
        with (
            patch.object(trainer_mod, "LOCAL_PARQUET_DIR", tmp_dir),
            patch.object(trainer_mod, "LOCAL_PROFILE_SCHEMA_HASH", schema_hash_file),
            patch.object(etl, "LOCAL_PARQUET_DIR", tmp_dir),
            # Prevent actual subprocess calls after cache is cleared
            patch.object(trainer_mod, "_parquet_date_range", return_value=None),
            patch("subprocess.run") as mock_subproc,
        ):
            mock_subproc.return_value = MagicMock(returncode=0, stderr="", stdout="")
            ensure_player_profile_daily_ready(start, end, use_local_parquet=True)

        return profile_parquet, schema_hash_file, checkpoint_file

    def test_stale_hash_removes_parquet_and_checkpoint(self):
        """When stored hash != current hash, profile parquet and checkpoint are deleted."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            profile_parquet, _, checkpoint_file = self._run_ensure(
                tmp_path,
                stored_hash="0000000000000000000000000000dead",  # wrong hash
                parquet_exists=True,
            )
            self.assertFalse(
                profile_parquet.exists(),
                "Stale parquet should have been deleted on hash mismatch",
            )
            self.assertFalse(
                checkpoint_file.exists(),
                "Stale ETL checkpoint should have been deleted on hash mismatch",
            )

    def test_missing_sidecar_treated_as_stale(self):
        """No sidecar file (legacy cache) is treated as stale and triggers rebuild."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            profile_parquet, _, _ = self._run_ensure(
                tmp_path,
                stored_hash=None,  # no sidecar written
                parquet_exists=True,
            )
            self.assertFalse(
                profile_parquet.exists(),
                "Parquet without sidecar should be treated as stale and deleted",
            )

    def test_matching_hash_does_not_delete_parquet(self):
        """When stored hash == current hash, profile parquet is preserved."""
        import hashlib
        import pandas as pd
        from trainer.etl_player_profile import compute_profile_schema_hash
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # R101: create a real (minimal) session parquet in tmp_dir so that
            # compute_profile_schema_hash(session_parquet=...) is hermetic —
            # both stored_hash and the in-process current_hash read the same file.
            sess_df = pd.DataFrame({
                "session_id": [1],
                "player_id": [100],
                "session_start_dtm": pd.to_datetime(["2024-01-01"]),
            })
            sess_path = tmp_path / "gmwds_t_session.parquet"
            sess_df.to_parquet(sess_path, index=False)
            # R106: ensure_player_profile_daily_ready adds _pop_tag to hash;
            # when canonical_id_whitelist=None, _pop_tag="_full".
            # R200: also adds _horizon_tag = f"_mlb={max_lookback_days}";
            # _run_ensure calls ensure_player_profile_daily_ready() without
            # max_lookback_days, so the default 365 is used → _mlb=365.
            base_hash = compute_profile_schema_hash(session_parquet=sess_path)
            # DEC-019 R601: _run_ensure calls ensure_player_profile_daily_ready with
            # default use_month_end_snapshots=True, fast_mode=False → _sched_tag="_month_end".
            # stored_hash must match this formula exactly.
            stored_hash = hashlib.md5((base_hash + "_full" + "_mlb=365" + "_month_end").encode()).hexdigest()
            profile_parquet, _, _ = self._run_ensure(
                tmp_path,
                stored_hash=stored_hash,
                parquet_exists=True,
            )
            self.assertTrue(
                profile_parquet.exists(),
                "Parquet with matching schema hash should NOT be deleted",
            )


class TestSessionMinDateInHash(unittest.TestCase):
    """Guard tests for the session_min_date drift signal in compute_profile_schema_hash().

    Scenario: developer starts with a 3-month local session parquet for quick testing,
    then replaces it with a 1-year file.  The 365d rolling features computed from the
    3-month cache are *truncated* (incorrect); the cache must be invalidated automatically.
    """

    def _make_session_parquet(self, tmp_dir: Path, min_date: str, max_date: str) -> Path:
        """Write a minimal session parquet whose row-group statistics span [min_date, max_date]."""
        import pandas as pd

        path = tmp_dir / "gmwds_t_session.parquet"
        df = pd.DataFrame(
            {
                "session_id": [1, 2],
                "player_id": [100, 101],
                "session_start_dtm": pd.to_datetime([min_date, max_date]),
            }
        )
        df.to_parquet(path, index=False)
        return path

    def test_hash_changes_when_session_min_date_shifts_earlier(self):
        """Hash must differ when raw session data is replaced with a longer history."""
        from trainer.etl_player_profile import compute_profile_schema_hash

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            sess_3m = self._make_session_parquet(tmp_path, min_date="2024-10-01", max_date="2024-12-31")
            hash_3m = compute_profile_schema_hash(session_parquet=sess_3m)

            # Overwrite with 1-year file — min_date is now 1 year earlier
            sess_1y = self._make_session_parquet(tmp_path, min_date="2024-01-01", max_date="2024-12-31")
            hash_1y = compute_profile_schema_hash(session_parquet=sess_1y)

            self.assertNotEqual(
                hash_3m,
                hash_1y,
                "Hash should change when session parquet min_date shifts earlier "
                "(3-month cache has truncated 365d features that must be recomputed)",
            )

    def test_hash_stable_when_only_max_date_extends(self):
        """Adding new sessions at the end (max_date later) should NOT change the hash.

        New tail data creates *new* snapshot rows (handled by the date-range check),
        but does not corrupt any *existing* 365d windows — no rebuild required.
        """
        from trainer.etl_player_profile import compute_profile_schema_hash

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            sess_old = self._make_session_parquet(tmp_path, min_date="2024-01-01", max_date="2024-09-30")
            hash_old = compute_profile_schema_hash(session_parquet=sess_old)

            sess_new = self._make_session_parquet(tmp_path, min_date="2024-01-01", max_date="2024-12-31")
            hash_new = compute_profile_schema_hash(session_parquet=sess_new)

            self.assertEqual(
                hash_old,
                hash_new,
                "Hash should NOT change when only max_date extends — "
                "existing 365d windows are unaffected; only new snapshots need adding.",
            )

    def test_hash_is_none_safe_when_session_parquet_absent(self):
        """compute_profile_schema_hash should not raise if session parquet is missing."""
        from trainer.etl_player_profile import compute_profile_schema_hash

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no_such_file.parquet"
            h = compute_profile_schema_hash(session_parquet=missing)
            self.assertIsInstance(h, str)
            self.assertEqual(len(h), 32, "Should still return a valid MD5 hex string")

    def test_read_session_min_date_returns_iso_string(self):
        """_read_session_min_date should return an ISO date string for a valid parquet."""
        from trainer.etl_player_profile import _read_session_min_date

        with tempfile.TemporaryDirectory() as tmp:
            sess = self._make_session_parquet(
                Path(tmp), min_date="2024-03-15", max_date="2024-06-30"
            )
            result = _read_session_min_date(sess)
            self.assertIsNotNone(result, "_read_session_min_date should return a date string")
            self.assertEqual(result, "2024-03-15", "Should return the min session_start_dtm as ISO")

    def test_read_session_min_date_returns_none_for_missing_file(self):
        """_read_session_min_date should return None gracefully for absent files."""
        from trainer.etl_player_profile import _read_session_min_date

        result = _read_session_min_date(Path("/nonexistent/path/file.parquet"))
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
