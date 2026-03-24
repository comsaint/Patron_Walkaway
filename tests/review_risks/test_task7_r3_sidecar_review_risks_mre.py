"""Task 7 R3 sidecar review risks -> executable MRE guards.

Scope:
- Tests only; no production code edits.
- Encode STATUS.md review findings as minimal reproducible checks on current behavior.
"""

from __future__ import annotations

import inspect
import json
import unittest

import trainer.trainer as trainer_mod


class TestTask7R3SidecarReviewRisksMRE(unittest.TestCase):
    def test_risk1_inconsistent_pipeline_vs_fingerprint_no_sidecar_inconsistent_reason(self) -> None:
        """MRE: pipeline dict can disagree with pipe segments inside fingerprint; no 'sidecar_inconsistent' tag."""
        cur = {
            "window_start": "2026-01-01T00:00:00",
            "window_end": "2026-02-01T00:00:00",
            "data_hash": "11111111",
            "cfg_hash": "aaaaaa",
            "profile_hash": "none",
            "feature_spec_hash": "fedcba",
            "neg_sample_frac": 1.0,
        }
        stale_fp = trainer_mod._fingerprint_from_chunk_cache_components(
            {**cur, "data_hash": "99999999"},
        )
        bad_pipeline = {**cur, "data_hash": "bbbbbbbb"}
        raw = json.dumps(
            {"v": 1, "fingerprint": stale_fp, "pipeline": bad_pipeline},
            sort_keys=True,
            separators=(",", ":"),
        )
        got_fp, got_pipe = trainer_mod._read_chunk_cache_sidecar(raw)
        self.assertEqual(got_fp, stale_fp)
        self.assertEqual(got_pipe, bad_pipeline)
        reasons = trainer_mod._chunk_cache_miss_reasons(got_fp, got_pipe, cur)
        self.assertNotIn(
            "sidecar_inconsistent",
            reasons,
            "Current implementation does not flag internal JSON skew",
        )
        self.assertIn("data", reasons)

    def test_risk2_pipe_delimiter_breaks_parse_when_spec_hash_contains_pipe(self) -> None:
        """MRE: '|' inside logical spec hash breaks 7-part pipe split -> unparsed."""
        bad_spec = "ab|cd"
        comp = {
            "window_start": "2026-01-01T00:00:00",
            "window_end": "2026-02-01T00:00:00",
            "data_hash": "aaaaaaaa",
            "cfg_hash": "111111",
            "profile_hash": "none",
            "feature_spec_hash": bad_spec,
            "neg_sample_frac": 1.0,
        }
        fp = trainer_mod._fingerprint_from_chunk_cache_components(comp)
        self.assertIn("|", fp)
        parsed = trainer_mod._parse_chunk_cache_fingerprint_pipe(fp)
        self.assertIsNone(
            parsed,
            "Pipe parser requires exactly 7 pipe segments; embedded | in spec breaks this.",
        )

    def test_risk3_incomplete_pipeline_dict_causes_multi_tag_or_window_false_positive(self) -> None:
        """MRE: sparse pipeline keys -> None vs current triggers broad miss_reason tags."""
        cur = {
            "window_start": "2026-01-01T00:00:00",
            "window_end": "2026-02-01T00:00:00",
            "data_hash": "aaaaaaaa",
            "cfg_hash": "111111",
            "profile_hash": "none",
            "feature_spec_hash": "spec1",
            "neg_sample_frac": 1.0,
        }
        stale_fp = trainer_mod._fingerprint_from_chunk_cache_components(
            {**cur, "cfg_hash": "999999"},
        )
        sparse_pipe = {"data_hash": "aaaaaaaa"}
        reasons = trainer_mod._chunk_cache_miss_reasons(stale_fp, sparse_pipe, cur)
        self.assertGreaterEqual(len(reasons), 2)
        self.assertIn("window", reasons)
        self.assertIn("config", reasons)

    def test_risk4_sidecar_read_has_no_max_length_guard_in_source(self) -> None:
        """MRE: _read_chunk_cache_sidecar does not cap input size (DoS / RAM footgun on hostile file)."""
        src = inspect.getsource(trainer_mod._read_chunk_cache_sidecar)
        self.assertNotRegex(
            src,
            r"len\(|MAX_|max_len|size limit",
            "Expected no explicit length guard in sidecar reader (review debt).",
        )

    def test_risk5_utf8_bom_prevents_json_branch(self) -> None:
        """MRE: leading BOM means startswith('{') is false -> legacy pipe path fails."""
        cur = {
            "window_start": "2026-01-01T00:00:00",
            "window_end": "2026-02-01T00:00:00",
            "data_hash": "aaaaaaaa",
            "cfg_hash": "111111",
            "profile_hash": "none",
            "feature_spec_hash": "x",
            "neg_sample_frac": 1.0,
        }
        inner = {
            "v": 1,
            "fingerprint": trainer_mod._fingerprint_from_chunk_cache_components(cur),
            "pipeline": cur,
        }
        raw = "\ufeff" + json.dumps(inner, sort_keys=True, separators=(",", ":"))
        fp, comp = trainer_mod._read_chunk_cache_sidecar(raw)
        self.assertNotEqual(fp, inner["fingerprint"])
        self.assertIsNone(comp)


if __name__ == "__main__":
    unittest.main()
