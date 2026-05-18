"""Unified entry for a packaged ``trainer_hightier`` deploy folder.

Call :func:`~trainer_hightier.config.set_hightier_serving_deploy_override` **before**
any ``trainer_hightier.serving`` import so ``runtime_config`` path snapshots match the
bundle. This module parses ``--bundle-dir`` first (injectable SSOT for runtime paths),
applies override, then imports serving — no dependency on repo checkout layout.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
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
        prediction_log_db_path=br / ls / "prediction_log.db",
        feature_state_db_path=br / ls / "feature_state.db",
        snapshot_manifest_dir=br / rel.get("snapshot_manifest_dir", "snapshots"),
        validator_out_dir=br / ls / "validator_out",
    )


def _load_dotenv_if_present(bundle_root: Path) -> None:
    """Load optional ``.env`` from *bundle_root* (does not override existing OS env)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    path = bundle_root / ".env"
    if path.is_file():
        load_dotenv(path, override=False)


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _resolve_log_level() -> int:
    key = (os.environ.get("DEPLOY_LOG_LEVEL") or os.environ.get("LOGLEVEL") or "INFO").strip().upper()
    return int(getattr(logging, key, logging.INFO))


def _apply_environ_overrides_to_serving(cfg: HightierServingConfig) -> HightierServingConfig:
    """Overlay CH / DB fields from the process environment (after optional ``.env`` load)."""
    kw: dict[str, Any] = {}
    if (v := str(os.environ.get("CH_HOST", "")).strip()):
        kw["ch_host"] = v
    if str(os.environ.get("CH_PORT", "")).strip():
        kw["ch_port"] = int(str(os.environ["CH_PORT"]).strip())
    if (v := str(os.environ.get("CH_USER", "")).strip()):
        kw["ch_user"] = v
    pw = str(os.environ.get("CH_PASS", "")).strip() or str(os.environ.get("CH_PASSWORD", "")).strip()
    if pw:
        kw["ch_password"] = pw
    if str(os.environ.get("CH_SECURE", "")).strip():
        kw["ch_secure"] = _parse_bool(os.environ.get("CH_SECURE"), default=cfg.ch_secure)
    if (v := str(os.environ.get("SOURCE_DB", "")).strip()):
        kw["source_db"] = v
    if not kw:
        return cfg
    return replace(cfg, **kw)


def _preflight_frozen_artifacts(bundle_root: Path, rel: dict[str, Any]) -> None:
    """Fail fast when frozen manifest layers or core bundle files are missing."""
    model_bundle = bundle_root / str(rel.get("model_bundle_dir", "models"))
    if not (model_bundle / "model.pkl").is_file():
        raise FileNotFoundError(f"bundle model.pkl missing under {model_bundle}")
    mapping = bundle_root / rel["canonical_mapping_parquet"]
    if not mapping.is_file():
        raise FileNotFoundError(f"bundle mapping missing: {mapping}")
    snap_root = bundle_root / str(rel.get("snapshot_manifest_dir", "snapshots"))
    man_path = snap_root / "active_manifest.json"
    if not man_path.is_file():
        raise FileNotFoundError(f"active_manifest.json missing under {snap_root}")
    man = json.loads(man_path.read_text(encoding="utf-8"))
    if not isinstance(man, dict):
        raise ValueError("active_manifest.json root must be a JSON object")
    for key in ("slow_patron_parquet", "adt_allowlist_parquet"):
        rel_p = man.get(key)
        if not rel_p:
            raise ValueError(f"active_manifest.json missing required key {key!r}")
        fp = (snap_root / str(rel_p)).resolve()
        if not fp.is_file():
            raise FileNotFoundError(
                f"manifest {key}={rel_p!r} resolves to missing file {fp} (under {snap_root})"
            )
    trial = man.get("trial_bet_behavior_parquet")
    if trial:
        tp = (snap_root / str(trial)).resolve()
        if not tp.is_file():
            raise FileNotFoundError(
                f"manifest trial_bet_behavior_parquet={trial!r} missing file {tp} (under {snap_root})"
            )


def _emit_deploy_boot_info(bundle_root: Path, cfg: HightierServingConfig, rel: dict[str, Any]) -> None:
    bi: dict[str, Any] = {}
    p = bundle_root / "bundle_info.json"
    if p.is_file():
        try:
            bi = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            logging.info("[deploy] bundle_info.json present but unreadable")
    mfile_ver: str | None = None
    mv = bundle_root / str(rel.get("model_bundle_dir", "models")) / "model_version"
    if mv.is_file():
        mfile_ver = mv.read_text(encoding="utf-8").strip() or None
    man_ver: str | None = None
    man_path = bundle_root / str(rel.get("snapshot_manifest_dir", "snapshots")) / "active_manifest.json"
    if man_path.is_file():
        try:
            md = json.loads(man_path.read_text(encoding="utf-8"))
            if isinstance(md, dict):
                man_ver = str(md.get("version", "") or "").strip() or None
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    logging.info(
        "[deploy] boot bundle=%s high_adt_only=%s",
        bundle_root,
        cfg.high_adt_only,
    )
    logging.info(
        "[deploy] boot summary model_version(bundle_info)=%s model_version(models/model_version)=%s "
        "manifest_version(active_manifest)=%s allowlist_sha256(bundle_info)=%s manifest_version(bundle_info)=%s",
        bi.get("model_version"),
        mfile_ver,
        man_ver,
        bi.get("allowlist_sha256"),
        bi.get("manifest_version"),
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
    args = _parse_deploy_args(argv)
    br = Path(args.bundle_dir).expanduser().resolve()
    _load_dotenv_if_present(br)
    logging.basicConfig(
        level=_resolve_log_level(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    rel = _load_rel_paths(br)
    cfg = _serving_config_for_bundle(br, rel)
    cfg = _apply_environ_overrides_to_serving(cfg)
    set_hightier_serving_deploy_override(cfg)
    import trainer_hightier.serving.runtime_config  # noqa: F401  # establish paths

    _preflight_frozen_artifacts(br, rel)
    _emit_deploy_boot_info(br, cfg, rel)

    model_bundle = br / rel.get("model_bundle_dir", "models")
    mapping = br / rel["canonical_mapping_parquet"]

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
