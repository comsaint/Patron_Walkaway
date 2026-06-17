"""Run a single scorer cycle using the bundle-local paths with player-game flags
left as-is (i.e., enabled if the bundle enables them). Start a watchdog thread
that will dump Python thread stacks to local_state/pg_watchdog_stackdump.txt after
`WATCHDOG_DELAY_S` seconds to help capture hangs.

Run from project root (cmd.exe):
    set PYTHONUNBUFFERED=1 && python -u scripts\run_scorer_once_with_pg_enabled_and_watchdog.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

_DEFAULT_BUNDLE = r"C:\Projects\Patron_Walkaway\out\gmwds_deploy_20260613-162313-3eb8de4"
BUNDLE_ROOT = Path(os.environ.get("HIGHTIER_DEPLOY_BUNDLE_DIR", _DEFAULT_BUNDLE)).resolve()
WATCHDOG_DELAY_S = 60

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


def _start_watchdog(out_path: Path, delay: int = WATCHDOG_DELAY_S) -> None:
    try:
        import faulthandler
    except Exception:
        print("faulthandler not available; watchdog disabled")
        return

    def _watch():
        print(f"Watchdog sleeping for {delay}s before dumping thread stacks to {out_path}")
        time.sleep(delay)
        try:
            with open(out_path, "wb") as f:
                faulthandler.dump_traceback(file=f, all_threads=True)
            print(f"Watchdog wrote stack dump to {out_path}")
        except Exception as e:
            print("Watchdog failed to write stack dump:", e)

    th = threading.Thread(target=_watch, daemon=True)
    th.start()


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

    # Set the deploy override to the bundle-derived config (no proxy changes)
    set_hightier_serving_deploy_override(cfg_base)
    print("Set bundle-local deploy serving override (cfg_base) — player_game flags unchanged")

    # Start watchdog that will dump stacks after a delay if process hangs
    watchdog_path = BUNDLE_ROOT / "local_state" / "pg_watchdog_stackdump.txt"
    _start_watchdog(watchdog_path, WATCHDOG_DELAY_S)

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
        str(BUNDLE_ROOT / "local_state" / "scorer_dry_run_report_pg_enabled.json"),
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

        conn = connect_state_db(Path(cfg_base.state_db_path))
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

