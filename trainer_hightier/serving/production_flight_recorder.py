"""Production flight recorder CLI (initialize recording root; shadow hooks attach later)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trainer_hightier.serving.flight_recorder.config import (
    DEFAULT_CONFIG_REL,
    FlightRecorderConfig,
)
from trainer_hightier.serving.flight_recorder.attach import attach_production_flight_recorders


def main(argv: list[str] | None = None) -> int:
    """Initialize or refresh a flight recording root on a deploy bundle."""
    parser = argparse.ArgumentParser(description="Production flight recorder")
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=("init", "shadow"),
        default="init",
        help="init: layout+identity+manifest; shadow: same init (hooks attach via deploy).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Recording config YAML (default: bundle local_state/flight_recording_config.yaml).",
    )
    parser.add_argument(
        "--no-sqlite-export",
        action="store_true",
        help="Skip SQLite state exports during init.",
    )
    args = parser.parse_args(argv)
    bundle_dir = args.bundle_dir.resolve()
    cfg_path = args.config or (bundle_dir / DEFAULT_CONFIG_REL)
    if cfg_path.is_file():
        config = FlightRecorderConfig.from_yaml_path(cfg_path)
    else:
        config = FlightRecorderConfig()
    if not config.enabled:
        print(json.dumps({"status": "disabled", "bundle_dir": str(bundle_dir)}))
        return 0
    if args.mode == "shadow":
        ctx = attach_production_flight_recorders(
            bundle_dir,
            config_path=cfg_path if cfg_path.is_file() else None,
            export_sqlite=not args.no_sqlite_export,
        )
        shadow_attached = True
    else:
        from trainer_hightier.serving.flight_recorder.init_recording import init_recording_root

        ctx = init_recording_root(
            bundle_dir,
            config,
            write_default_config=not cfg_path.is_file(),
            export_sqlite=not args.no_sqlite_export,
        )
        shadow_attached = False
    summary = {
        "status": "ok",
        "mode": args.mode,
        "recording_root": str(ctx.recording.root),
        "model_version": ctx.recording.model_version,
        "partial": ctx.recording.partial,
        "manifest": str(ctx.recording.root / "MANIFEST.json"),
        "scorer_recorder_attached": shadow_attached and config.capture_scorer_stages,
        "validator_recorder_attached": shadow_attached and config.capture_validator_stages,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
