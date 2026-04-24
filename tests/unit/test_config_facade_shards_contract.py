from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import trainer.config as top_config
import trainer.core.config as core_config
from trainer.core import (
    _config_clickhouse_sources as clickhouse_shard,
    _config_env_paths as env_paths_shard,
    _config_serving_runtime as serving_shard,
    _config_training_domain as domain_shard,
    _config_training_memory as memory_shard,
    _config_validator as validator_shard,
)


class TestConfigFacadeShardReexportContract(unittest.TestCase):
    """Lock representative facade re-exports after config module sharding."""

    def test_core_facade_reexports_representative_values_from_each_shard(self) -> None:
        cases = [
            ("DEFAULT_MODEL_DIR", env_paths_shard.DEFAULT_MODEL_DIR),
            ("CH_HOST", clickhouse_shard.CH_HOST),
            ("SCORER_LOOKBACK_HOURS", serving_shard.SCORER_LOOKBACK_HOURS),
            ("VALIDATOR_FINALIZE_ON_HORIZON", validator_shard.VALIDATOR_FINALIZE_ON_HORIZON),
            ("WALKAWAY_GAP_MIN", domain_shard.WALKAWAY_GAP_MIN),
            ("STEP9_TRAIN_FROM_FILE", memory_shard.STEP9_TRAIN_FROM_FILE),
        ]
        for name, shard_value in cases:
            self.assertTrue(hasattr(core_config, name), f"core facade must expose {name}")
            self.assertEqual(getattr(core_config, name), shard_value, f"core facade drifted for {name}")

    def test_top_level_facade_reexports_match_core_facade(self) -> None:
        for name in (
            "DEFAULT_MODEL_DIR",
            "CH_HOST",
            "SCORER_LOOKBACK_HOURS",
            "VALIDATOR_FINALIZE_ON_HORIZON",
            "WALKAWAY_GAP_MIN",
            "STEP9_TRAIN_FROM_FILE",
        ):
            self.assertTrue(hasattr(top_config, name), f"trainer.config must expose {name}")
            self.assertEqual(
                getattr(top_config, name),
                getattr(core_config, name),
                f"trainer.config drifted from trainer.core.config for {name}",
            )


class TestChunkTwoStageCacheFacadeMonkeypatchContract(unittest.TestCase):
    """Facade helper should keep honoring monkeypatches on trainer.core.config."""

    def test_core_helper_uses_facade_default_when_env_empty(self) -> None:
        with patch.dict(os.environ, {"CHUNK_TWO_STAGE_CACHE": ""}, clear=False):
            with patch.object(core_config, "CHUNK_TWO_STAGE_CACHE_DEFAULT", False):
                self.assertFalse(core_config.chunk_two_stage_cache_enabled())


if __name__ == "__main__":
    unittest.main()
