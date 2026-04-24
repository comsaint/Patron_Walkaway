import pathlib
import re
import unittest


class TestConfigRisks(unittest.TestCase):
    def test_label_lookahead_min_matches_sum_runtime(self):
        # Runtime sanity: values stay consistent even if defined separately.
        from trainer.config import ALERT_HORIZON_MIN, LABEL_LOOKAHEAD_MIN, WALKAWAY_GAP_MIN

        self.assertEqual(LABEL_LOOKAHEAD_MIN, WALKAWAY_GAP_MIN + ALERT_HORIZON_MIN)

    def test_label_lookahead_min_is_derived_in_source(self):
        # Maintainability rule: LABEL_LOOKAHEAD_MIN must reference X + Y in source,
        # not be a numeric literal, so that changing X or Y keeps it consistent.
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        # The public SSOT import surface remains trainer.core.config, but the
        # implementation now lives in the training-domain shard.
        cfg_path = repo_root / "trainer" / "core" / "_config_training_domain.py"
        text = cfg_path.read_text(encoding="utf-8")

        # Find the assignment line and assert it references the two source constants.
        # This is intentionally strict; it should fail while it's a numeric literal.
        m = re.search(r"^\s*LABEL_LOOKAHEAD_MIN\s*=\s*(.+?)\s*(#.*)?$", text, flags=re.M)
        self.assertIsNotNone(
            m,
            "LABEL_LOOKAHEAD_MIN assignment not found in trainer/core/_config_training_domain.py",
        )
        rhs = m.group(1)
        self.assertIn("WALKAWAY_GAP_MIN", rhs)
        self.assertIn("ALERT_HORIZON_MIN", rhs)

