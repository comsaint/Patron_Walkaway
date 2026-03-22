# Re-export: make trainer.etl_player_profile resolve to the implementation module
# so that "import trainer.etl_player_profile as etl" / patch("trainer.etl_player_profile.PROFILE_VERSION") work.
# When run as __main__, parse CLI with stdlib-only argparse before loading implementation (pytest subprocess timeouts).
import sys

if __name__ == "__main__":
    from trainer.etl.etl_player_profile_argparse import build_etl_player_profile_argparser

    build_etl_player_profile_argparser().parse_args()
    from trainer.etl import etl_player_profile as _impl

    sys.modules["trainer.etl_player_profile"] = _impl
    _impl.main()
else:
    from trainer.etl import etl_player_profile as _impl

    sys.modules["trainer.etl_player_profile"] = _impl
    backfill = _impl.backfill
    compute_profile_schema_hash = _impl.compute_profile_schema_hash
    LOCAL_PROFILE_SCHEMA_HASH = _impl.LOCAL_PROFILE_SCHEMA_HASH
