"""Run a single scorer cycle using the bundle-local paths but with
player-game ready-queue dry-run and shadow scoring disabled.

This script sets a bundle-local HightierServingConfig override (so scorer uses
bundle-local Feast repo + local_state paths), loads the model bundle and runs
``trainer_hightier.serving.scorer.main(['--once', ...])`` in-process so we can
observe a single cycle and produce a dry-run report under bundle local_state.

Why this approach: the normal `python -m trainer_hightier.deploy.main --mode scorer`
path constructs the override internally. We recreate the override here but modify
only the player-game flags to quickly test whether the per-player ready-queue
re-fetch loop is the cause of the stall.

Run from project root (cmd.exe):
    set PYTHONUNBUFFERED=1 && python -u scripts\run_scorer_once_disabled_pg.py

"""
from __future__ import annotations

import dataclasses
import logging
import os
import sys
from dataclasses import fields
from pathlib import Path

_DEFAULT_BUNDLE = r"C:\Projects\Patron_Walkaway\out\gmwds_deploy_20260613-162313-3eb8de4"
BUNDLE_ROOT = Path(os.environ.get("HIGHTIER_DEPLOY_BUNDLE_DIR", _DEFAULT_BUNDLE)).resolve()

# Try to load bundle .env if python-dotenv available so CH_* / other creds are available
try:
    from dotenv import load_dotenv

    env_path = BUNDLE_ROOT / ".env"
    if env_path.is_file():
        load_dotenv(str(env_path), override=False)
        print(f"Loaded .env from {env_path}")
    else:
        print(f"No .env found at {env_path}; proceeding with existing env vars")
except Exception:
    print("python-dotenv not available; proceeding with existing environment variables")

try:
    # Import deploy helper to compute bundle-local serving config
    from trainer_hightier.deploy.main import _load_rel_paths, _serving_config_for_bundle
    from trainer_hightier.config import HightierServingConfig, set_hightier_serving_deploy_override

    rel = _load_rel_paths(BUNDLE_ROOT)
    cfg_base = _serving_config_for_bundle(BUNDLE_ROOT, rel)
    # Apply any CH_* / SOURCE_DB overrides from the bundle .env into the config
    from trainer_hightier.config import apply_hightier_serving_environ_overrides

    try:
        cfg_base = apply_hightier_serving_environ_overrides(cfg_base)
        print("Applied environment overrides from process env into serving config")
    except Exception as exc:
        print("Warning: failed to apply environment overrides:", exc)

    # Debug: show a few ClickHouse-related values that will be used by the scorer
    try:
        print(
            "serving cfg ch_host=",
            getattr(cfg_base, "ch_host", None),
            "ch_user=",
            repr(getattr(cfg_base, "ch_user", None)),
            "ch_password=",
            repr(getattr(cfg_base, "ch_password", None)),
        )
    except Exception:
        pass

    # Create a small proxy object that delegates attribute access to the
    # bundle-derived config but overrides the two player_game flags we want
    # disabled. This avoids constructing a new dataclass instance (which can
    # fail when runtime class signatures differ between installed/editable copies).
    class _ProxyCfg:
        def __init__(self, base):
            self._base = base

        def __getattr__(self, name):
            if name in ("player_game_ready_queue_dry_run_enabled", "player_game_shadow_scoring_enabled"):
                return False
            return getattr(self._base, name)

    cfg_mod = _ProxyCfg(cfg_base)
    set_hightier_serving_deploy_override(cfg_mod)
    print("Set bundle-local deploy serving override (proxy) with player_game dry-run disabled")

    # Prepare scorer argv: pass model_bundle path (bundle models dir) + mapping + allowlist
    model_bundle = BUNDLE_ROOT / rel.get("model_bundle_dir", "models")
    mapping = BUNDLE_ROOT / rel["canonical_mapping_parquet"]
    allowlist = BUNDLE_ROOT / rel.get("adt_allowlist_parquet", "mapping/adt_allowed_players_q0p99.parquet")

    argv = [
        "--once",
        "--bundle-dir",
        str(model_bundle),
        "--canonical-mapping",
        str(mapping),
        "--adt-allowlist",
        str(allowlist),
        "--dry-run-report",
        str(BUNDLE_ROOT / "local_state" / "scorer_dry_run_report.json"),
    ]

    print("Invoking scorer.main with argv:", argv)

    # Import scorer and run
    from trainer_hightier.serving import scorer

    rc = scorer.main(argv)
    print(f"scorer.main returned exit code: {rc}")

    metrics = scorer.get_last_scorer_cycle_metrics()
    print("Last scorer cycle metrics:", metrics)

    # Print a short state: last_processed_etl_insert and prediction_log tail
    try:
        import sqlite3

        from trainer_hightier.serving.state_db import connect_state_db, meta_get

        conn = connect_state_db(Path(cfg_mod.state_db_path))
        try:
            last = meta_get(conn, "last_processed_etl_insert")
            print("state.db last_processed_etl_insert:", last)
        finally:
            conn.close()
    except Exception as e:
        print("Failed to read state DB summary:", e)

except Exception as exc:  # show full stack to console
    print("ERROR running diagnostic scorer:", exc, file=sys.stderr)
    import traceback

    traceback.print_exc()
    raise

sys.exit(0)





