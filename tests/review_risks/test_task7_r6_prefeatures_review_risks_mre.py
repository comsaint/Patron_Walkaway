"""Task 7 R6 prefeatures two-stage cache review risks -> executable MRE guards.

Encode STATUS.md § Code Review — Task 7 R6 findings as tests on **current** behavior.
Scope: tests only; no production code edits.
"""

from __future__ import annotations

import inspect
import re
import unittest

import pandas as pd

import trainer.trainer as trainer_mod


class TestTask7R6PrefeaturesReviewRisksMRE(unittest.TestCase):
    # --- 1) canonical_map not in fingerprint ---

    def test_risk1_chunk_cache_components_has_no_canonical_map_field(self) -> None:
        """MRE: pipeline components dict does not carry canonical_map identity."""
        chunk = {
            "window_start": pd.Timestamp("2026-01-01 00:00:00"),
            "window_end": pd.Timestamp("2026-02-01 00:00:00"),
        }
        bets = pd.DataFrame({"bet_id": [1], "player_id": ["p1"]})
        comp = trainer_mod._chunk_cache_components(
            chunk,
            bets,
            profile_hash="none",
            feature_spec_hash="x",
            neg_sample_frac=1.0,
        )
        self.assertNotIn("canonical_map", comp)

    def test_risk1_prefeatures_components_inherit_no_canonical_digest(self) -> None:
        """MRE: prefeatures key material still omits canonical_map (inherits from base)."""
        base = {
            "window_start": "a",
            "window_end": "b",
            "data_hash": "c",
            "cfg_hash": "d",
            "profile_hash": "e",
            "feature_spec_hash": "spec",
            "neg_sample_frac": 0.5,
        }
        pc = trainer_mod._prefeatures_cache_components(base)
        self.assertNotIn("canonical_map", pc)

    # --- 2) orphan parquet without sidecar -> safe miss (empty sidecar) ---

    def test_risk2_empty_sidecar_never_matches_real_prefingerprint(self) -> None:
        """MRE: missing/empty sidecar yields sk that cannot equal computed _pref_key."""
        base = {
            "window_start": "2026-01-01T00:00:00",
            "window_end": "2026-02-01T00:00:00",
            "data_hash": "11111111",
            "cfg_hash": "aaaaaa",
            "profile_hash": "none",
            "feature_spec_hash": "myspec",
            "neg_sample_frac": 0.5,
        }
        pref = trainer_mod._prefeatures_cache_components(base)
        real_fp = trainer_mod._fingerprint_from_chunk_cache_components(pref)
        sk, _sc = trainer_mod._read_chunk_cache_sidecar("")
        self.assertNotEqual(sk, real_fp)

    # --- 3) hit path uses whole-frame read_parquet ---

    def test_risk3_prefeatures_hit_loads_via_read_parquet(self) -> None:
        """MRE: R6 hit path loads prefeatures with read_parquet (full table)."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn("prefeatures cache hit", src)
        self.assertRegex(
            src,
            r"read_parquet\(\s*_pref_path\s*\)",
            "Expected prefeatures hit to use pd.read_parquet(_pref_path) whole-frame load.",
        )

    # --- 4) two parquet writes on non-skip path (prefeatures + final chunk) ---

    def test_risk4_process_chunk_has_multiple_to_parquet_calls(self) -> None:
        """MRE: R6 on path writes prefeatures parquet and final chunk parquet (disk 2× class)."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertGreaterEqual(src.count("to_parquet"), 2)

    # --- 5) prefeatures hit has no parquet metadata integrity probe ---

    def test_risk5_prefeatures_hit_branch_skips_metadata_probe(self) -> None:
        """MRE: prefeatures cache hit returns without read_metadata / corruption probe."""
        src = inspect.getsource(trainer_mod.process_chunk)
        m = re.search(
            r"prefeatures cache hit.*?Track LLM",
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "prefeatures hit block not found before Track LLM section")
        assert m is not None
        fragment = m.group(0)
        self.assertNotRegex(
            fragment,
            r"read_metadata|ParquetFile|pq\.ParquetFile|cache corrupt",
            "Expected prefeatures hit to skip metadata/corruption probe.",
        )

    def test_risk5_read_parquet_on_hit_not_wrapped_in_try_except_for_integrity(self) -> None:
        """MRE: no dedicated try/except around prefeatures read (truncation surfaces later)."""
        src = inspect.getsource(trainer_mod.process_chunk)
        # Narrow: line with read_parquet(_pref_path) is not in an except handler block (heuristic).
        self.assertNotRegex(
            src,
            r"except\s+Exception\s*:\s*[^\n]*\n[^\n]*read_parquet\(\s*_pref_path",
            "If this matches, review prefeatures read error handling.",
        )

    # --- 6) no atomic rename for prefeatures write (debt) ---

    def test_risk6_prefeatures_write_no_os_replace_in_process_chunk(self) -> None:
        """MRE: current process_chunk does not use os.replace for atomic parquet swap."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertNotIn("os.replace", src)

    # --- 7) log strings distinguish prefeatures vs final cache stale ---

    def test_risk7_distinct_stale_log_substrings_prefeatures_vs_final(self) -> None:
        """MRE: log lines use different wording for prefeatures vs final chunk stale."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn("prefeatures cache stale", src)
        self.assertRegex(src, r"cache stale \(key mismatch, miss_reason=")

    def test_risk7_prefeatures_miss_uses_chunk_cache_miss_reasons(self) -> None:
        """MRE: prefeatures stale path still delegates to _chunk_cache_miss_reasons (same tags)."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn("_chunk_cache_miss_reasons", src)
        self.assertIn("prefeatures cache stale", src)


if __name__ == "__main__":
    unittest.main()
