"""Task 7 R4 profile_hash chunk-scope review risks -> executable MRE guards.

Scope:
- Tests only; no production code edits.
- Encode STATUS.md R4 review as minimal reproducible checks on current behavior.
"""

from __future__ import annotations

import inspect
import unittest

import pandas as pd

import trainer.trainer as trainer_mod


class TestTask7R4ProfileHashReviewRisksMRE(unittest.TestCase):
    def test_risk1_nat_only_vs_future_only_share_p0_fingerprint(self) -> None:
        """MRE: rows with NaT snapshot_dtm excluded like future rows -> identical p0 hash."""
        we = pd.Timestamp("2026-02-01 00:00:00")
        nat_only = pd.DataFrame(
            {
                "canonical_id": ["a"],
                "snapshot_dtm": [pd.NaT],
                "f1": [1.0],
            }
        )
        future_only = pd.DataFrame(
            {
                "canonical_id": ["b"],
                "snapshot_dtm": [pd.Timestamp("2026-03-01")],
                "f1": [2.0],
            }
        )
        self.assertEqual(
            trainer_mod._profile_hash_chunk_scoped(nat_only, we),
            trainer_mod._profile_hash_chunk_scoped(future_only, we),
        )

    def test_risk1_p0_path_ignores_row_count_when_all_times_unusable(self) -> None:
        """MRE: many invalid snapshot rows vs one — same p0 digest (volume invisible)."""
        we = pd.Timestamp("2026-02-01 00:00:00")
        one_nat = pd.DataFrame(
            {
                "canonical_id": ["x"],
                "snapshot_dtm": [pd.NaT],
                "f1": [0.0],
            }
        )
        many_nat = pd.DataFrame(
            {
                "canonical_id": [f"p{i}" for i in range(50)],
                "snapshot_dtm": [pd.NaT] * 50,
                "f1": [float(i) for i in range(50)],
            }
        )
        self.assertEqual(
            trainer_mod._profile_hash_chunk_scoped(one_nat, we),
            trainer_mod._profile_hash_chunk_scoped(many_nat, we),
        )

    def test_risk2_profile_scope_no_size_guard_in_source(self) -> None:
        """MRE: no branch to skip full-subset hashing for very large profile slices."""
        src = inspect.getsource(trainer_mod._profile_hash_chunk_scoped)
        self.assertNotRegex(
            src,
            r"MAX_|max_rows|chunk_size|len\(sub\)\s*>",
            "Expected no explicit large-slice guard in profile chunk hash (review debt).",
        )

    def test_risk3_missing_snapshot_dtm_uses_legacy_len_cols_branch(self) -> None:
        """MRE: absence of snapshot_dtm triggers run-level len+cols fingerprint."""
        src = inspect.getsource(trainer_mod._profile_hash_chunk_scoped)
        self.assertIn('"snapshot_dtm" not in profile_df.columns', src)
        self.assertRegex(src, r"len\(profile_df\).*_profile_cols_key|_profile_cols_key.*len\(profile_df\)")

    def test_risk4_process_chunk_passes_window_end_to_profile_hash(self) -> None:
        """MRE: cache key uses window_end, not extended_end, for profile scope."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertRegex(
            src,
            r"_profile_hash_chunk_scoped\(\s*profile_df\s*,\s*window_end\s*\)",
        )
        self.assertNotRegex(
            src,
            r"_profile_hash_chunk_scoped\(\s*profile_df\s*,\s*extended_end\s*\)",
        )

    def test_risk5_profile_hash_is_six_hex_when_not_none(self) -> None:
        """MRE: non-'none' profile component is md5[:6] (short collision window)."""
        we = pd.Timestamp("2026-02-01 00:00:00")
        df = pd.DataFrame(
            {
                "canonical_id": ["a"],
                "snapshot_dtm": [pd.Timestamp("2026-01-15")],
                "x": [1.0],
            }
        )
        h = trainer_mod._profile_hash_chunk_scoped(df, we)
        self.assertNotEqual(h, "none")
        self.assertRegex(h, r"^[0-9a-f]{6}$")

        legacy = pd.DataFrame({"canonical_id": ["z"], "f1": [1.0]})
        h2 = trainer_mod._profile_hash_chunk_scoped(legacy, we)
        self.assertRegex(h2, r"^[0-9a-f]{6}$")


if __name__ == "__main__":
    unittest.main()
