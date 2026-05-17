"""Unified entry for a packaged ``trainer_hightier`` deploy folder.

Call :func:`~trainer_hightier.config.set_hightier_serving_deploy_override` **before**
any ``trainer_hightier.serving`` import so ``runtime_config`` path snapshots match the
bundle. This module parses ``--bundle-dir`` first, applies override, then imports serving.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from trainer_hightier.config import HightierServingConfig, set_hightier_serving_deploy_override


def _parse_deploy_args(argv: list[str] | None) -> argparse.Namespace:
    pr = argparse.ArgumentParser(description="trainer_hightier deploy bundle runner")
    pr.add_argument("--bundle-dir", type=Path, required=True)
    pr.add_argument("--host", default="127.0.0.1")
    pr.add_argument("--port", type=int, default=8001)
    pr.add_argument(
        "--mode",
        choices=("all", "api", "scorer", "validator"),
        default="all",
        help="Which process to run (default: all = API+validator threads + scorer foreground).",
    )
    return pr.parse_args(argv)


def _load_rel_paths(bundle_root: Path) -> dict[str, Any]:
    p = bundle_root / "deploy_bundle_paths.json"
    if not p.is_file():
        raise FileNotFoundError(f"deploy_bundle_paths.json missing under {bundle_root}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("deploy_bundle_paths.json must be a JSON object")
    return raw


def _serving_config_for_bundle(bundle_root: Path, rel: dict[str, Any]) -> HightierServingConfig:
    br = bundle_root.resolve()
    ls = rel.get("local_state_dir", "local_state")
    base = HightierServingConfig()
    return replace(
        base,
        state_db_path=br / ls / "state.db",
        feature_state_db_path=br / ls / "feature_state.db",
        snapshot_manifest_dir=br / rel.get("snapshot_manifest_dir", "snapshots"),
        validator_out_dir=br / ls / "validator_out",
    )


def _emit_boot_line(bundle_root: Path, cfg: HightierServingConfig) -> None:
    p = bundle_root / "bundle_info.json"
    if not p.is_file():
        logging.info("[deploy] no bundle_info.json under %s", bundle_root)
        return
    bi = json.loads(p.read_text(encoding="utf-8"))
    logging.info(
        "[deploy] boot bundle=%s model_version=%s manifest_version=%s allowlist_sha256=%s high_adt_only=%s",
        bundle_root,
        bi.get("model_version"),
        bi.get("manifest_version"),
        bi.get("allowlist_sha256"),
        cfg.high_adt_only,
    )


def _validator_foreground() -> None:
    old = sys.argv
    try:
        sys.argv = ["trainer_hightier_validator", "--interval", "60"]
        from trainer_hightier.serving.validator import main as vmain

        vmain()
    finally:
        sys.argv = old


def main(argv: list[str] | None = None) -> int:
    """Configure paths from bundle, log versions, then run selected mode."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_deploy_args(argv)
    br = Path(args.bundle_dir).expanduser().resolve()
    rel = _load_rel_paths(br)
    cfg = _serving_config_for_bundle(br, rel)
    set_hightier_serving_deploy_override(cfg)
    import trainer_hightier.serving.runtime_config  # noqa: F401  # establish paths

    _emit_boot_line(br, cfg)
    model_bundle = br / rel.get("model_bundle_dir", "models")
    mapping = br / rel["canonical_mapping_parquet"]
    if not mapping.is_file():
        raise FileNotFoundError(f"bundle mapping missing: {mapping}")
    if not (model_bundle / "model.pkl").is_file():
        raise FileNotFoundError(f"bundle model.pkl missing under {model_bundle}")

    mode = str(args.mode)
    if mode == "api":
        from trainer_hightier.serving import api_server

        logging.info("[deploy] API %s:%s (mode=api)", args.host, args.port)
        api_server.app.run(host=args.host, port=int(args.port), threaded=True)
        return 0
    if mode == "validator":
        logging.info("[deploy] validator foreground (mode=validator)")
        _validator_foreground()
        return 0
    if mode == "scorer":
        from trainer_hightier.serving import scorer

        logging.info("[deploy] scorer foreground (mode=scorer)")
        return int(
            scorer.main(
                [
                    "--bundle-dir",
                    str(model_bundle),
                    "--canonical-mapping",
                    str(mapping),
                ]
            )
        )

    from trainer_hightier.serving import api_server
    from trainer_hightier.serving import scorer

    th_api = threading.Thread(
        target=api_server.app.run,
        kwargs={"host": args.host, "port": int(args.port), "threaded": True},
        name="hightier-api",
        daemon=True,
    )
    th_api.start()
    logging.info("[deploy] API thread %s:%s", args.host, args.port)

    th_val = threading.Thread(target=_validator_foreground, name="hightier-validator", daemon=True)
    th_val.start()

    logging.info("[deploy] scorer foreground (mode=all)")
    return int(
        scorer.main(
            [
                "--bundle-dir",
                str(model_bundle),
                "--canonical-mapping",
                str(mapping),
            ]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
