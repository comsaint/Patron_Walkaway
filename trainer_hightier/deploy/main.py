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
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trainer_hightier.config import HK_TZ, HightierServingConfig, set_hightier_serving_deploy_override
from trainer_hightier.serving.contracts import (
    META_KEY_MID_TERM_REFRESH_LAST_ATTEMPT,
    META_KEY_REFRESH_SUPERVISOR_LAST_CHECK,
    META_KEY_SLOW_REFRESH_LAST_ATTEMPT,
    META_KEY_SLOW_REFRESH_LAST_CHECK_DAY,
    META_KEY_SOURCE_MIRROR_BET_STATUS,
    META_KEY_SOURCE_MIRROR_SESSION_STATUS,
)


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
    pr.add_argument(
        "--no-refresh-supervisor",
        action="store_true",
        help="Disable legacy Parquet snapshot refresh supervisor (debug only).",
    )
    pr.add_argument(
        "--no-feast-startup-refresh",
        action="store_true",
        help="Skip startup Feast online refresh (debug only; scorer likely fails readiness).",
    )
    pr.add_argument(
        "--force-feast-refresh",
        action="store_true",
        help="Force Feast online refresh even when readiness looks fresh.",
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
    feast_art = rel.get("feast_artifacts_dir", "artifacts/feast")
    feast_repo = rel.get("feast_repo_dir", "feast_repo")
    base = HightierServingConfig()
    return replace(
        base,
        state_db_path=br / ls / "state.db",
        prediction_log_db_path=br / ls / "prediction_log.db",
        feature_state_db_path=br / ls / "feature_state.db",
        snapshot_manifest_dir=br / rel.get("snapshot_manifest_dir", "snapshots"),
        validator_out_dir=br / ls / "validator_out",
        production_cleaned_bet_mirror_dir=br / "source_mirror" / "cleaned_bet",
        production_cleaned_session_mirror_parquet=br / "source_mirror" / "cleaned_session.parquet",
        scorer_feast_repo_path=(br / feast_repo).resolve(),
        scorer_feast_readiness_path=(br / rel.get(
            "feast_readiness_path", f"{feast_art}/feast_online_readiness.json"
        )).resolve(),
        adt_allowed_players_parquet=(br / rel.get(
            "adt_allowlist_parquet", "mapping/adt_allowed_players_q0p99.parquet"
        )).resolve(),
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
    feast_repo = bundle_root / rel.get("feast_repo_dir", "feast_repo")
    if not feast_repo.is_dir():
        raise FileNotFoundError(f"bundle feast_repo missing: {feast_repo}")
    allowlist = bundle_root / rel.get("adt_allowlist_parquet", "mapping/adt_allowed_players_q0p99.parquet")
    if not allowlist.is_file():
        raise FileNotFoundError(f"bundle ADT allowlist missing: {allowlist}")
    snap_root = bundle_root / str(rel.get("snapshot_manifest_dir", "snapshots"))
    man_path = snap_root / "active_manifest.json"
    if not man_path.is_file():
        logging.warning("[deploy] active_manifest.json missing under %s (Feast bundle ok)", snap_root)
        return
    man = json.loads(man_path.read_text(encoding="utf-8"))
    if not isinstance(man, dict):
        raise ValueError("active_manifest.json root must be a JSON object")
    for legacy_key in (
        "trial_bet_behavior_parquet",
        "slow_patron_parquet",
        "fe_derived_parquet",
        "fe_short_term_parquet",
        "mid_term_snapshot_parquet",
    ):
        if man.get(legacy_key):
            logging.warning(
                "[deploy] metadata-only Feast bundle ignores legacy manifest key %s",
                legacy_key,
            )


def _preflight_feature_supplyability(bundle_root: Path, rel: dict[str, Any]) -> None:
    """Verify model feature columns have runtime suppliers (registry + bundled parquet)."""

    from trainer_hightier.serving.feature_supply import (
        assert_feature_supplyability_or_raise,
        load_frozen_registry_for_bundle,
        model_feature_columns_from_pickle,
    )

    model_bundle = bundle_root / str(rel.get("model_bundle_dir", "models"))
    snap_p = model_bundle / "feature_candidate_registry.snapshot.yaml"
    if not snap_p.is_file():
        logging.warning(
            "[deploy] skip feature-supply preflight (%s missing); rebuild bundle with frozen registry",
            snap_p.name,
        )
        return
    snap = load_frozen_registry_for_bundle(model_bundle)
    model_feats = model_feature_columns_from_pickle(model_bundle)
    snap_root = bundle_root / str(rel.get("snapshot_manifest_dir", "snapshots"))
    man_path = snap_root / "active_manifest.json"
    man: dict[str, Any] = {}
    if man_path.is_file():
        raw = json.loads(man_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("active_manifest.json root must be a JSON object")
        man = raw
    else:
        logging.warning(
            "[deploy] skip feature-supply preflight (active_manifest.json missing under %s)",
            snap_root,
        )

    def _layer_path(key: str) -> Path | None:
        rel_p = man.get(key)
        if not rel_p:
            return None
        fp = (snap_root / str(rel_p)).resolve()
        return fp if fp.is_file() else None

    assert_feature_supplyability_or_raise(
        snap,
        model_feats,
        slow_pack_path=_layer_path("slow_patron_parquet"),
        trial_pack_path=_layer_path("trial_bet_behavior_parquet"),
        fe_pack_path=_layer_path("fe_derived_parquet"),
        fe_short_term_pack_path=_layer_path("fe_short_term_parquet"),
        mid_term_pack_path=_layer_path("mid_term_snapshot_parquet"),
        manifest=man if isinstance(man, dict) else None,
        scorer_v2_feast_mode=True,
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


def _refresh_lock_path(cfg: HightierServingConfig) -> Path:
    """Return the bundle-local snapshot refresh lock path."""

    p = Path(cfg.snapshot_manifest_dir).resolve() / ".snapshot_refresh_supervisor.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _try_acquire_refresh_lock(cfg: HightierServingConfig) -> int | None:
    """Acquire an inter-process refresh lock, removing clearly stale locks."""

    lock = _refresh_lock_path(cfg)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    stale_after = timedelta(minutes=int(cfg.snapshot_refresh_lock_stale_minutes))
    try:
        fd = os.open(str(lock), flags)
    except FileExistsError:
        try:
            mtime = datetime.fromtimestamp(lock.stat().st_mtime, tz=timezone.utc)
            if datetime.now(timezone.utc) - mtime <= stale_after:
                return None
            lock.unlink(missing_ok=True)
            fd = os.open(str(lock), flags)
        except FileExistsError:
            return None
    os.write(fd, f"pid={os.getpid()} ts={datetime.now(timezone.utc).isoformat()}\n".encode("utf-8"))
    return fd


def _release_refresh_lock(cfg: HightierServingConfig, fd: int) -> None:
    """Release the inter-process refresh lock."""

    os.close(fd)
    try:
        _refresh_lock_path(cfg).unlink(missing_ok=True)
    except OSError:
        logging.warning("[deploy] failed to remove snapshot refresh lock", exc_info=True)


def _record_supervisor_mirror_status(cfg: HightierServingConfig) -> None:
    """Persist latest production source mirror validation summaries."""

    from trainer_hightier.serving.feature_state_store import feature_state_meta_set
    from trainer_hightier.serving.production_source_mirror import (
        validate_production_bet_mirror,
        validate_production_session_mirror,
    )

    bet = validate_production_bet_mirror()
    sess = validate_production_session_mirror()
    feature_state_meta_set(META_KEY_SOURCE_MIRROR_BET_STATUS, bet.message, path=cfg.feature_state_db_path)
    feature_state_meta_set(
        META_KEY_SOURCE_MIRROR_SESSION_STATUS,
        sess.message,
        path=cfg.feature_state_db_path,
    )


def _startup_snapshot_repair_or_raise(
    model_bundle: Path,
    mapping: Path,
    cfg: HightierServingConfig,
) -> None:
    """Synchronously repair hard-failure snapshot states before scorer startup."""

    from trainer_hightier.serving.feature_state_store import feature_state_meta_set
    from trainer_hightier.serving.snapshot_freshness import build_deploy_startup_snapshot_plan
    from trainer_hightier.serving.snapshot_updater import run_mid_term_refresh, run_slow_refresh

    plan = build_deploy_startup_snapshot_plan(cfg)
    logging.info(
        "[deploy] startup snapshot plan mid_hard=%s slow_hard=%s mid_reason=%s slow_reason=%s",
        plan.mid_hard_failure,
        plan.slow_hard_failure,
        plan.mid_reason,
        plan.slow_reason,
    )
    if not plan.mid_startup_refresh and not plan.slow_startup_refresh:
        _record_supervisor_mirror_status(cfg)
        return
    fd = _try_acquire_refresh_lock(cfg)
    if fd is None:
        raise RuntimeError(
            "[deploy] startup snapshot repair blocked: refresh lock held by another process"
        )
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        if plan.mid_startup_refresh:
            logging.warning("[deploy] startup repairing mid-term snapshot: %s", plan.mid_reason)
            run_mid_term_refresh(bundle_dir=model_bundle, canonical_mapping=mapping, bootstrap=False)
            feature_state_meta_set(
                META_KEY_MID_TERM_REFRESH_LAST_ATTEMPT,
                now_iso,
                path=cfg.feature_state_db_path,
            )
        if plan.slow_startup_refresh:
            logging.warning("[deploy] startup repairing slow snapshot: %s", plan.slow_reason)
            run_slow_refresh(canonical_mapping=mapping)
            feature_state_meta_set(
                META_KEY_SLOW_REFRESH_LAST_ATTEMPT,
                now_iso,
                path=cfg.feature_state_db_path,
            )
    except Exception as exc:
        raise RuntimeError(f"[deploy] startup snapshot repair failed: {exc}") from exc
    finally:
        _release_refresh_lock(cfg, fd)
    after = build_deploy_startup_snapshot_plan(cfg)
    if after.mid_hard_failure or after.slow_hard_failure:
        raise RuntimeError(
            "[deploy] startup snapshot repair incomplete: "
            f"mid={after.mid_reason}; slow={after.slow_reason}"
        )
    _record_supervisor_mirror_status(cfg)


def _mid_term_refresh_needed(cfg: HightierServingConfig) -> tuple[bool, str]:
    """Return whether the mid-term snapshot should refresh now."""

    from trainer_hightier.serving.feature_state_store import read_active_manifest
    from trainer_hightier.serving.snapshot_freshness import (
        evaluate_mid_term_freshness,
        read_mid_term_anchor_max,
    )

    man = read_active_manifest()
    if man is None:
        return True, "active manifest missing"
    anchor = read_mid_term_anchor_max(man.mid_term_snapshot_parquet, man.raw)
    status = evaluate_mid_term_freshness(
        anchor_max=anchor,
        hard_cap_days=int(cfg.mid_term_stale_hard_cap_days),
        close_hour=int(cfg.gaming_day_close_hour),
    )
    now_hk = datetime.now(ZoneInfo(HK_TZ))
    after_refresh_target = int(now_hk.hour) >= int(cfg.mid_term_refresh_target_hour)
    if status.status in ("missing", "hard_cap_breached"):
        return True, status.message
    if status.status == "stale_allowed" and after_refresh_target:
        return True, status.message
    return False, status.message


def _slow_refresh_needed(cfg: HightierServingConfig) -> tuple[bool, str]:
    """Return whether the monthly slow patron snapshot should refresh now."""

    from trainer_hightier.serving.feature_state_store import (
        feature_state_meta_get,
        feature_state_meta_set,
        read_active_manifest,
    )
    from trainer_hightier.serving.snapshot_freshness import evaluate_slow_freshness, read_slow_anchor_max

    today = datetime.now(ZoneInfo(HK_TZ)).date().isoformat()
    last_check = feature_state_meta_get(
        META_KEY_SLOW_REFRESH_LAST_CHECK_DAY,
        path=cfg.feature_state_db_path,
    )
    if last_check == today:
        return False, "slow refresh already checked today"
    feature_state_meta_set(
        META_KEY_SLOW_REFRESH_LAST_CHECK_DAY,
        today,
        path=cfg.feature_state_db_path,
    )

    man = read_active_manifest()
    if man is None:
        return True, "active manifest missing"
    anchor = read_slow_anchor_max(man.slow_patron_parquet, man.raw)
    status = evaluate_slow_freshness(
        anchor_max=anchor,
        monthly_grace_days=int(cfg.slow_monthly_grace_days),
        hard_cap_days=int(cfg.slow_stale_hard_cap_days),
        close_hour=int(cfg.gaming_day_close_hour),
    )
    if status.status in ("missing", "stale_allowed", "hard_cap_breached"):
        return True, status.message
    return False, status.message


def _refresh_supervisor_once(
    model_bundle: Path,
    mapping: Path,
    cfg: HightierServingConfig,
    *,
    fail_on_error: bool = False,
) -> None:
    """Run one deploy-managed refresh check for mid-term and slow snapshots."""

    from trainer_hightier.serving.feature_state_store import feature_state_meta_set

    feature_state_meta_set(
        META_KEY_REFRESH_SUPERVISOR_LAST_CHECK,
        datetime.now(timezone.utc).isoformat(),
        path=cfg.feature_state_db_path,
    )
    mid_needed, mid_reason = _mid_term_refresh_needed(cfg)
    slow_needed, slow_reason = _slow_refresh_needed(cfg)
    if not mid_needed and not slow_needed:
        logging.info("[deploy] snapshot refresh not needed: mid=%s slow=%s", mid_reason, slow_reason)
        _record_supervisor_mirror_status(cfg)
        return
    fd = _try_acquire_refresh_lock(cfg)
    if fd is None:
        logging.info("[deploy] snapshot refresh skipped; another deploy process holds the lock")
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        from trainer_hightier.serving.snapshot_updater import run_mid_term_refresh, run_slow_refresh

        if mid_needed:
            logging.warning("[deploy] refreshing mid-term snapshot: %s", mid_reason)
            run_mid_term_refresh(bundle_dir=model_bundle, canonical_mapping=mapping, bootstrap=False)
            feature_state_meta_set(
                META_KEY_MID_TERM_REFRESH_LAST_ATTEMPT,
                now_iso,
                path=cfg.feature_state_db_path,
            )
        if slow_needed:
            logging.warning("[deploy] refreshing slow patron snapshot: %s", slow_reason)
            run_slow_refresh(canonical_mapping=mapping)
            feature_state_meta_set(
                META_KEY_SLOW_REFRESH_LAST_ATTEMPT,
                now_iso,
                path=cfg.feature_state_db_path,
            )
    except Exception:
        if fail_on_error:
            raise
        logging.exception("[deploy] snapshot refresh attempt failed; last good manifest remains active")
    finally:
        _release_refresh_lock(cfg, fd)
    _record_supervisor_mirror_status(cfg)


def _refresh_supervisor_loop(model_bundle: Path, mapping: Path, cfg: HightierServingConfig) -> None:
    """Continuously maintain production snapshots for this deploy process."""

    interval = max(60, int(cfg.snapshot_refresh_supervisor_poll_seconds))
    while True:
        _refresh_supervisor_once(model_bundle, mapping, cfg)
        time.sleep(interval)


def _start_refresh_supervisor(model_bundle: Path, mapping: Path, cfg: HightierServingConfig) -> None:
    """Run startup hard-failure repair, then launch the background refresh loop."""

    _startup_snapshot_repair_or_raise(model_bundle, mapping, cfg)
    th = threading.Thread(
        target=_refresh_supervisor_loop,
        args=(model_bundle, mapping, cfg),
        name="hightier-refresh-supervisor",
        daemon=True,
    )
    th.start()
    logging.info(
        "[deploy] refresh supervisor thread started poll_seconds=%d",
        int(cfg.snapshot_refresh_supervisor_poll_seconds),
    )


def _feast_refresh_lock_path(cfg: HightierServingConfig) -> Path:
    """Return bundle-local Feast refresh lock under feast artifacts."""
    readiness = Path(cfg.scorer_feast_readiness_path or (Path(cfg.feature_state_db_path).parent / "feast_readiness.json"))
    lock_dir = readiness.parent
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / ".feast_online_refresh.lock"


def _try_acquire_feast_refresh_lock(cfg: HightierServingConfig) -> int | None:
    """Acquire Feast refresh lock with short timeout; return fd or None."""
    lock = _feast_refresh_lock_path(cfg)
    wait_s = max(1, int(cfg.feast_startup_refresh_lock_wait_seconds))
    deadline = time.monotonic() + wait_s
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    while True:
        try:
            fd = os.open(str(lock), flags)
            os.write(
                fd,
                f"pid={os.getpid()} ts={datetime.now(timezone.utc).isoformat()}\n".encode("utf-8"),
            )
            return fd
        except FileExistsError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)


def _release_feast_refresh_lock(cfg: HightierServingConfig, fd: int) -> None:
    """Release bundle-local Feast refresh lock."""
    os.close(fd)
    try:
        _feast_refresh_lock_path(cfg).unlink(missing_ok=True)
    except OSError:
        logging.warning("[deploy] failed to remove Feast refresh lock", exc_info=True)


def _bundle_allowlist_path(bundle_root: Path, rel: dict[str, Any]) -> Path:
    return (bundle_root / rel.get("adt_allowlist_parquet", "mapping/adt_allowed_players_q0p99.parquet")).resolve()


def _needs_feast_startup_refresh(
    cfg: HightierServingConfig,
    *,
    force: bool,
    require_mid: bool,
    require_slow: bool,
) -> tuple[bool, str]:
    """Return whether deploy should run startup Feast refresh."""
    from trainer_hightier.serving.feast_readiness import (
        evaluate_feast_readiness_gate,
        load_feast_online_readiness,
        resolve_feast_readiness_path,
    )

    if force:
        return True, "forced by --force-feast-refresh"
    path = resolve_feast_readiness_path(cfg)
    readiness = load_feast_online_readiness(path)
    gate = evaluate_feast_readiness_gate(
        readiness,
        require_mid=require_mid,
        require_slow=require_slow,
        readiness_path=path,
        close_hour=int(cfg.gaming_day_close_hour),
        mid_hard_cap_days=int(cfg.mid_term_stale_hard_cap_days),
        slow_hard_cap_days=int(cfg.slow_stale_hard_cap_days),
        slow_grace_days=int(cfg.slow_monthly_grace_days),
    )
    if gate.ok:
        return False, "readiness fresh"
    return True, gate.hard_failure_reason or "readiness gate not ok"


def _run_deploy_feast_smoke_or_raise(
    cfg: HightierServingConfig,
    *,
    mapping: Path,
    allowlist: Path,
    mid_columns: tuple[str, ...],
    slow_columns: tuple[str, ...],
) -> None:
    """Run deploy Feast readiness + allowlist lookup smoke for model-specific columns."""
    from trainer_hightier.serving.feast_readiness import run_deploy_feast_readiness_check

    gate = run_deploy_feast_readiness_check(
        require_mid=bool(mid_columns),
        require_slow=bool(slow_columns),
        allowlist_parquet=allowlist,
        canonical_mapping_parquet=mapping,
        mid_columns=mid_columns,
        slow_columns=slow_columns,
        run_lookup_smoke=True,
    )
    if not gate.ok:
        raise RuntimeError(gate.hard_failure_reason or "[deploy] Feast readiness smoke failed")
    logging.info("[deploy] feast readiness smoke ok %s", gate.to_log_dict())


def _startup_feast_refresh_or_raise(
    bundle_root: Path,
    rel: dict[str, Any],
    cfg: HightierServingConfig,
    *,
    mapping: Path,
    force: bool,
    skip_refresh: bool,
) -> None:
    """Run startup Feast refresh + smoke for scorer-capable deploy modes."""
    from trainer_hightier.serving import feast_online_refresh as refresh_mod
    from trainer_hightier.serving.feature_supply import (
        assert_scorer_supplier_plan_or_raise,
        build_scorer_supplier_plan,
        load_frozen_registry_for_bundle,
        model_feature_columns_from_pickle,
    )

    model_bundle = bundle_root / str(rel.get("model_bundle_dir", "models"))
    allowlist = _bundle_allowlist_path(bundle_root, rel)
    feast_repo = Path(cfg.scorer_feast_repo_path or (bundle_root / "feast_repo")).resolve()
    if not feast_repo.is_dir():
        raise FileNotFoundError(f"[deploy] feast_repo missing: {feast_repo}")

    snap = load_frozen_registry_for_bundle(model_bundle)
    model_feats = model_feature_columns_from_pickle(model_bundle)
    plan = build_scorer_supplier_plan(snap, model_feats)
    assert_scorer_supplier_plan_or_raise(plan)
    require_mid = bool(plan.feast_mid_cols or plan.mid_composite_cols)
    require_slow = bool(plan.feast_slow_cols)

    if skip_refresh:
        logging.warning("[deploy] --no-feast-startup-refresh: skipping refresh (smoke still runs)")
        _run_deploy_feast_smoke_or_raise(
            cfg,
            mapping=mapping,
            allowlist=allowlist,
            mid_columns=plan.feast_mid_cols,
            slow_columns=plan.feast_slow_cols,
        )
        return

    need_refresh, reason = _needs_feast_startup_refresh(
        cfg,
        force=force,
        require_mid=require_mid,
        require_slow=require_slow,
    )
    if need_refresh:
        logging.warning("[deploy] startup Feast refresh required: %s", reason)
        fd = _try_acquire_feast_refresh_lock(cfg)
        if fd is None:
            raise RuntimeError(
                "[deploy] startup Feast refresh blocked: another process holds the refresh lock"
            )
        layers: list[str] = []
        if require_mid:
            layers.append("mid")
        if require_slow:
            layers.append("slow")
        if not layers:
            layers = ["mid", "slow"]
        try:
            opts = refresh_mod._resolve_refresh_options(
                layers=",".join(layers),
                source="clickhouse",
                skip_apply=False,
                skip_materialize=False,
                smoke_only=False,
                dry_run=False,
                feast_repo=feast_repo,
                readiness_path=cfg.scorer_feast_readiness_path,
                canonical_mapping=mapping,
                adt_allowlist=allowlist,
                local_cleaned_bet=None,
                local_cleaned_session=None,
                max_smoke_entities=int(cfg.scorer_feast_deploy_lookup_smoke_sample_size),
                summary_path=(Path(cfg.scorer_feast_readiness_path).parent / "feast_online_refresh_report.json"),
            )
            refresh_mod.run_feast_online_refresh(opts)
        finally:
            _release_feast_refresh_lock(cfg, fd)
    else:
        logging.info("[deploy] startup Feast refresh skipped: %s", reason)

    _run_deploy_feast_smoke_or_raise(
        cfg,
        mapping=mapping,
        allowlist=allowlist,
        mid_columns=plan.feast_mid_cols,
        slow_columns=plan.feast_slow_cols,
    )


def _scorer_argv(
    *,
    model_bundle: Path,
    mapping: Path,
    allowlist: Path,
) -> list[str]:
    """Build scorer CLI argv for deploy foreground."""
    return [
        "--bundle-dir",
        str(model_bundle),
        "--canonical-mapping",
        str(mapping),
        "--adt-allowlist",
        str(allowlist),
    ]


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

    model_bundle = br / rel.get("model_bundle_dir", "models")
    mapping = br / rel["canonical_mapping_parquet"]
    allowlist = _bundle_allowlist_path(br, rel)
    mode = str(args.mode)

    _preflight_frozen_artifacts(br, rel)
    if mode in ("all", "scorer"):
        _startup_feast_refresh_or_raise(
            br,
            rel,
            cfg,
            mapping=mapping,
            force=bool(args.force_feast_refresh),
            skip_refresh=bool(args.no_feast_startup_refresh),
        )
    elif mode in ("api", "validator") and not bool(args.no_feast_startup_refresh):
        logging.warning(
            "[deploy] mode=%s does not run Feast startup refresh; use mode=all or mode=scorer first",
            mode,
        )
    _preflight_feature_supplyability(br, rel)
    _emit_deploy_boot_info(br, cfg, rel)

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
                _scorer_argv(
                    model_bundle=model_bundle,
                    mapping=mapping,
                    allowlist=allowlist,
                )
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
            _scorer_argv(
                model_bundle=model_bundle,
                mapping=mapping,
                allowlist=allowlist,
            )
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
