"""Run a single scorer cycle using the bundle-local paths but with
player-game shadow scoring disabled (ready-queue left as-is).

Run from project root (cmd.exe):
    set PYTHONUNBUFFERED=1 && python -u scripts\run_scorer_once_disable_pg_shadow_only.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

BUNDLE_ROOT = Path(r"C:\Projects\Patron_Walkaway\out\gmwds_deploy_20260613-162313-3eb8de4").resolve()

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
    from trainer_hightier.config import set_hightier_serving_deploy_override

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

    # Proxy: disable only the player_game shadow scoring
    class _ProxyCfg:
        def __init__(self, base):
            self._base = base

        def __getattr__(self, name):
            if name == "player_game_shadow_scoring_enabled":
                return False
            return getattr(self._base, name)

    cfg_mod = _ProxyCfg(cfg_base)
    set_hightier_serving_deploy_override(cfg_mod)
    print("Set bundle-local deploy serving override (proxy) with player_game shadow scoring disabled")

    # Prepare scorer argv
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
        str(BUNDLE_ROOT / "local_state" / "scorer_dry_run_report_shadow_disabled.json"),
    ]

    print("Invoking scorer.main with argv:", argv)

    from trainer_hightier.serving import scorer

    rc = scorer.main(argv)
    print(f"scorer.main returned exit code: {rc}")

    metrics = scorer.get_last_scorer_cycle_metrics()
    print("Last scorer cycle metrics:", metrics)

    # Print a short state summary
    try:
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

