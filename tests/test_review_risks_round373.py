"""Minimal reproducible guards for reviewer risks (Round 373).

Scope:
- Convert OPT-001 self-review risks into executable tests.
- Tests only; no production code edits.
- Unresolved risks are marked expectedFailure so they stay visible in CI.
"""

from __future__ import annotations

import inspect
import re
import unittest

import trainer.config as cfg
import trainer.etl_player_profile as etl_mod
import trainer.trainer as trainer_mod


class TestR373Opt001RiskGuards(unittest.TestCase):
    """Guardrails for OPT-001 follow-up risks (#1-#5 from review)."""

    @unittest.expectedFailure
    def test_r373_1_anchor_clamp_should_emit_explicit_warning(self):
        """Risk #1: session-range clamp should warn when it invalidates ideal anchor."""
        src = inspect.getsource(trainer_mod.ensure_player_profile_ready)
        self.assertRegex(
            src,
            r"required_start\s*=\s*max\(\s*required_start\s*,\s*session_rng\[0\]\s*\).*logger\.warning",
            "When session_rng[0] pushes required_start forward, code should emit a clear warning.",
        )

    @unittest.expectedFailure
    def test_r373_2_ensure_profile_signature_should_drop_fast_mode(self):
        """Risk #2: ensure_player_profile_ready still accepts dead-parameter fast_mode."""
        sig = inspect.signature(trainer_mod.ensure_player_profile_ready)
        self.assertNotIn(
            "fast_mode",
            sig.parameters,
            "fast_mode is dead in ensure_player_profile_ready and should be removed from signature.",
        )

    @unittest.expectedFailure
    def test_r373_3_preload_oom_guard_should_consider_available_ram(self):
        """Risk #3: preload OOM guard should use available RAM (psutil), not only on-disk bytes."""
        src = inspect.getsource(etl_mod.backfill)
        self.assertTrue(
            re.search(r"import\s+psutil|virtual_memory\s*\(", src),
            "Expected psutil.virtual_memory()-based guard in backfill preload decision.",
        )

    @unittest.expectedFailure
    def test_r373_4_preload_limit_should_be_config_driven(self):
        """Risk #4: preload byte threshold should be configurable via config.py."""
        self.assertTrue(
            hasattr(cfg, "PROFILE_PRELOAD_MAX_BYTES"),
            "config.py should expose PROFILE_PRELOAD_MAX_BYTES for preload guard tuning.",
        )
        src = inspect.getsource(etl_mod.backfill)
        self.assertIn(
            "PROFILE_PRELOAD_MAX_BYTES",
            src,
            "backfill should read preload threshold from config instead of local hardcode.",
        )

    @unittest.expectedFailure
    def test_r373_5_load_sessions_local_should_accept_max_lookback_days(self):
        """Risk #5: _load_sessions_local should respect caller's horizon, not fixed 365-day constant."""
        sig = inspect.signature(etl_mod._load_sessions_local)
        self.assertIn(
            "max_lookback_days",
            sig.parameters,
            "_load_sessions_local should accept max_lookback_days for tighter pushdown windows.",
        )

        src = inspect.getsource(etl_mod.build_player_profile)
        self.assertRegex(
            src,
            r"_load_sessions_local\(\s*snapshot_dtm\s*,\s*max_lookback_days\s*=\s*max_lookback_days\s*\)",
            "build_player_profile should forward max_lookback_days into _load_sessions_local.",
        )


if __name__ == "__main__":
    unittest.main()
