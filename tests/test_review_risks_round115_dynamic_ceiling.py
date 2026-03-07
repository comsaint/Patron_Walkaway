"""Minimal reproducible tests for Round 115 dynamic-ceiling review risks.

Tests-only change set:
- No production code edits.
- Unresolved risks are encoded as expected failures so they remain visible.
"""

from __future__ import annotations

import unittest

import trainer.config as cfg
import trainer.etl_player_profile as etl_mod


_GIB = 1024 ** 3


class _TempConfig:
    """Temporarily patch config attributes during a test."""

    def __init__(self, **updates):
        self._updates = updates
        self._old = {}

    def __enter__(self):
        for k, v in self._updates.items():
            self._old[k] = getattr(cfg, k)
            setattr(cfg, k, v)
        return self

    def __exit__(self, exc_type, exc, tb):
        for k, v in self._old.items():
            setattr(cfg, k, v)
        return False


class TestR115DynamicCeilingRiskGuards(unittest.TestCase):
    """Guards for dynamic ceiling behavior in _compute_duckdb_memory_limit_bytes."""

    def test_r115_0_config_should_expose_ram_max_fraction(self):
        """Sanity: config should expose PROFILE_DUCKDB_RAM_MAX_FRACTION."""
        self.assertTrue(
            hasattr(cfg, "PROFILE_DUCKDB_RAM_MAX_FRACTION"),
            "Missing config knob: PROFILE_DUCKDB_RAM_MAX_FRACTION",
        )

    def test_r115_1_none_ram_max_fraction_should_preserve_legacy_behavior(self):
        """When RAM_MAX_FRACTION is None, behavior should match pre-change clamp."""
        with _TempConfig(
            PROFILE_DUCKDB_RAM_FRACTION=0.5,
            PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB=0.5,
            PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB=8.0,
            PROFILE_DUCKDB_RAM_MAX_FRACTION=None,
        ):
            got = etl_mod._compute_duckdb_memory_limit_bytes(10 * _GIB)
        # legacy formula: clamp(10*0.5, 0.5, 8.0) => 5.0 GiB
        self.assertEqual(got, 5 * _GIB)

    def test_r115_2_invalid_ram_max_fraction_should_fallback_to_fixed_max(self):
        """Invalid RAM_MAX_FRACTION should fallback (equivalent to None path)."""
        common = dict(
            PROFILE_DUCKDB_RAM_FRACTION=0.5,
            PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB=0.5,
            PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB=8.0,
        )
        with _TempConfig(PROFILE_DUCKDB_RAM_MAX_FRACTION=None, **common):
            legacy = etl_mod._compute_duckdb_memory_limit_bytes(10 * _GIB)
        with _TempConfig(PROFILE_DUCKDB_RAM_MAX_FRACTION=-0.25, **common):
            invalid = etl_mod._compute_duckdb_memory_limit_bytes(10 * _GIB)
        self.assertEqual(invalid, legacy)

    def test_r115_3_dynamic_ceiling_should_raise_cap_on_high_ram(self):
        """Risk #1: high-RAM case should exceed fixed MAX_GB when configured."""
        with _TempConfig(
            PROFILE_DUCKDB_RAM_FRACTION=0.5,
            PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB=0.5,
            PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB=8.0,
            PROFILE_DUCKDB_RAM_MAX_FRACTION=0.45,
        ):
            got = etl_mod._compute_duckdb_memory_limit_bytes(44 * _GIB)
        # Intended behavior from review: should be > 8 GiB (e.g. around 19.8 GiB).
        self.assertGreater(got, 8 * _GIB)

    def test_r115_4_dynamic_ceiling_should_not_reduce_moderate_ram_budget(self):
        """Risk #1: dynamic ceiling should not be stricter than legacy path."""
        common = dict(
            PROFILE_DUCKDB_RAM_FRACTION=0.5,
            PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB=0.5,
            PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB=8.0,
        )
        with _TempConfig(PROFILE_DUCKDB_RAM_MAX_FRACTION=None, **common):
            legacy = etl_mod._compute_duckdb_memory_limit_bytes(10 * _GIB)
        with _TempConfig(PROFILE_DUCKDB_RAM_MAX_FRACTION=0.45, **common):
            dynamic = etl_mod._compute_duckdb_memory_limit_bytes(10 * _GIB)
        self.assertGreaterEqual(dynamic, legacy)

    def test_r115_5_docstring_should_mention_ram_max_fraction_ceiling(self):
        """Risk #2: docstring should document RAM_MAX_FRACTION ceiling logic."""
        doc = etl_mod._compute_duckdb_memory_limit_bytes.__doc__ or ""
        self.assertIn("PROFILE_DUCKDB_RAM_MAX_FRACTION", doc)
        self.assertRegex(doc, r"effective.*ceiling|ceiling.*effective")

    def test_r115_6_should_warn_when_ram_max_fraction_less_than_fraction(self):
        """Risk #3: warn if RAM_MAX_FRACTION < RAM_FRACTION (semantic mismatch)."""
        with _TempConfig(
            PROFILE_DUCKDB_RAM_FRACTION=0.5,
            PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB=0.5,
            PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB=8.0,
            PROFILE_DUCKDB_RAM_MAX_FRACTION=0.45,
        ):
            with self.assertLogs(etl_mod.logger, level="WARNING") as cm:
                etl_mod._compute_duckdb_memory_limit_bytes(44 * _GIB)
        self.assertTrue(
            any("RAM_MAX_FRACTION" in m and "RAM_FRACTION" in m for m in cm.output),
            "Expected warning when RAM_MAX_FRACTION < RAM_FRACTION",
        )


if __name__ == "__main__":
    unittest.main()
