# Re-export: make trainer.etl_player_profile resolve to the implementation module
# so that "import trainer.etl_player_profile as etl" / patch("trainer.etl_player_profile.PROFILE_VERSION") work.
# When run as __main__ (e.g. python -m trainer.etl_player_profile), forward to the implementation's main().
# Re-export below for type checker; at runtime sys.modules overwrite makes the module resolve to _impl.
import sys
from trainer.etl import etl_player_profile as _impl  # noqa: F401

sys.modules["trainer.etl_player_profile"] = _impl

# Type-checker visible re-exports (runtime resolution is via sys.modules above)
backfill = _impl.backfill
compute_profile_schema_hash = _impl.compute_profile_schema_hash
LOCAL_PROFILE_SCHEMA_HASH = _impl.LOCAL_PROFILE_SCHEMA_HASH

if __name__ == "__main__":
    _impl.main()
