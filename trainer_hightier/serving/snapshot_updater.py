"""Daily (or manual) snapshot publish: versioned slow-feature Parquet + atomic manifest switch."""

from __future__ import annotations

import argparse
import logging
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from trainer_hightier.config import (
    FE_DERIVED_SOURCE_KIND_PRODUCTION,
    MANIFEST_KEY_FE_SHORT_TERM,
    MANIFEST_KEY_MID_TERM_ANCHOR_MAX,
    MANIFEST_KEY_MID_TERM_COVERAGE_END,
    MANIFEST_KEY_MID_TERM_GENERATED_AT,
    MANIFEST_KEY_MID_TERM_GRAIN,
    MANIFEST_KEY_MID_TERM_SNAPSHOT,
    MANIFEST_KEY_MID_TERM_STALE_HARD_CAP_DAYS,
    MANIFEST_KEY_SLOW_ANCHOR_MAX,
    MANIFEST_KEY_SLOW_GENERATED_AT,
    MANIFEST_KEY_SLOW_MONTHLY_GRACE_DAYS,
    MANIFEST_KEY_SLOW_STALE_HARD_CAP_DAYS,
    MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
    MID_TERM_STALE_HARD_CAP_DAYS,
    SLOW_MONTHLY_GRACE_DAYS,
    SLOW_PATRON_GRAIN_CANONICAL_ASOF,
    SLOW_STALE_HARD_CAP_DAYS,
    default_hightier_serving_config,
)
from trainer_hightier.serving.adt_allowlist import parquet_player_id_row_count, sha256_file
from trainer_hightier.serving.feature_state_store import (
    init_feature_state_db,
    log_job_finish,
    log_job_start,
    publish_manifest_atomic,
    read_active_manifest,
    update_watermark,
    upsert_adt_allowlist_meta,
)
from trainer_hightier.serving.feature_supply import (
    MANIFEST_KEY_FE_DERIVED,
    load_frozen_registry_for_bundle,
    model_feature_columns_from_pickle,
)
from trainer_hightier.serving.model_bundle import infer_training_cutoff_iso, load_hightier_model_bundle
from trainer_hightier.serving.production_materialize import (
    materialize_production_fe_short_term,
    materialize_production_mid_term_daily_snapshot,
    materialize_production_fe_derived,
    materialize_production_slow_canonical_asof,
    resolve_production_canonical_mapping,
)
from trainer_hightier.serving.production_source_mirror import ensure_production_mirrors_ready
from trainer_hightier.serving.snapshot_freshness import (
    serving_gaming_day,
    validate_mid_term_artifact,
    validate_slow_artifact,
)
from trainer_hightier.feature_experiment.feature_cadence import (
    classify_model_fe_features,
    short_term_enrich_columns_with_dependencies,
)
from trainer_hightier.utils.patron_session_metrics import default_adt_allowed_players_parquet_path
from trainer_hightier.utils.slow_patron_180d_monthly import (
    default_slow_patron_180d_monthly_parquet_path,
    materialize_slow_patron_180d_monthly,
)

logger = logging.getLogger(__name__)


def _utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _resolve_model_fe_split(bundle_dir: Path | None) -> dict[str, tuple[str, ...]]:
    """Return short/mid ``fe__*`` columns for the loaded model bundle."""

    bundle = load_hightier_model_bundle(bundle_dir=bundle_dir)
    model_bundle = Path(bundle.bundle_dir).resolve()
    snap = load_frozen_registry_for_bundle(model_bundle)
    model_feats = model_feature_columns_from_pickle(model_bundle)
    return classify_model_fe_features(snap, model_feats)


def _merge_manifest_payload(
    *,
    previous: dict[str, Any] | None,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Merge new layer paths into manifest while preserving untouched layers."""

    base: dict[str, Any] = dict(previous or {})
    base.update({k: v for k, v in updates.items() if v is not None})
    return base


def run_mid_term_refresh(
    *,
    bundle_dir: Path | None = None,
    cleaned_bet: Path | None = None,
    canonical_mapping: Path | None = None,
    bootstrap: bool = False,
) -> Path:
    """Refresh mid-term canonical daily snapshot for latest ``D - 1`` anchor."""

    cfg = default_hightier_serving_config()
    init_feature_state_db()
    run_id = _utc_run_id()
    log_job_start(run_id, detail="mid_term_refresh")
    try:
        bet_src = cleaned_bet
        if bet_src is None:
            from trainer_hightier.serving.production_materialize import default_production_cleaned_bet_path

            bet_src = default_production_cleaned_bet_path()
        ensure_production_mirrors_ready(
            for_mid_term=True,
            for_slow=False,
            cleaned_bet=Path(bet_src),
        )
        manifest_dir = Path(cfg.snapshot_manifest_dir).resolve()
        manifest_dir.mkdir(parents=True, exist_ok=True)
        cmap = resolve_production_canonical_mapping(canonical_mapping)
        if cfg.adt_allowed_players_parquet is not None:
            al_src = Path(cfg.adt_allowed_players_parquet).resolve()
        else:
            al_src = default_adt_allowed_players_parquet_path(float(cfg.adt_allowlist_quantile)).resolve()
        if not al_src.is_file():
            raise FileNotFoundError(f"adt allowlist missing: {al_src}")

        serving_day = serving_gaming_day(close_hour=int(cfg.gaming_day_close_hour))
        anchor_end = serving_day - timedelta(days=1)
        anchor_start = anchor_end - timedelta(days=1) if bootstrap else anchor_end

        staging = manifest_dir / f"mid_term_daily_{run_id}.parquet"
        staging, mid_meta = materialize_production_mid_term_daily_snapshot(
            cleaned_bet_parquet=Path(bet_src),
            canonical_mapping_parquet=cmap,
            adt_allowlist_parquet=al_src,
            out_parquet=staging,
            anchor_gaming_day_start=anchor_start,
            anchor_gaming_day_end=anchor_end,
        )
        val = validate_mid_term_artifact(staging)
        if val.hard_failure:
            raise ValueError(f"mid-term staging validation failed: {val.message}")

        prev = read_active_manifest()
        prev_raw = dict(prev.raw) if prev is not None else {}
        cov_iso = datetime.now(timezone.utc).isoformat()
        payload = _merge_manifest_payload(
            previous=prev_raw,
            updates={
                "version": run_id,
                MANIFEST_KEY_MID_TERM_SNAPSHOT: str(staging.resolve()),
                MANIFEST_KEY_MID_TERM_GRAIN: MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
                MANIFEST_KEY_MID_TERM_COVERAGE_END: cov_iso,
                "coverage_end_exclusive": cov_iso,
                MANIFEST_KEY_MID_TERM_GENERATED_AT: cov_iso,
                MANIFEST_KEY_MID_TERM_ANCHOR_MAX: mid_meta.get("mid_term_anchor_gaming_day_max"),
                MANIFEST_KEY_MID_TERM_STALE_HARD_CAP_DAYS: int(cfg.mid_term_stale_hard_cap_days),
                "fe_derived_source_kind": FE_DERIVED_SOURCE_KIND_PRODUCTION,
            },
        )
        publish_manifest_atomic(payload)
        update_watermark("mid_term", cov_iso)
        log_job_finish(run_id, status="ok", detail=str(staging))
        return staging
    except Exception as exc:
        log_job_finish(run_id, status="error", detail=str(exc)[:1800])
        raise


def run_slow_refresh(
    *,
    cleaned_session: Path | None = None,
    canonical_mapping: Path | None = None,
) -> Path:
    """Refresh monthly slow patron canonical ASOF snapshot."""

    cfg = default_hightier_serving_config()
    init_feature_state_db()
    run_id = _utc_run_id()
    log_job_start(run_id, detail="slow_refresh")
    try:
        ensure_production_mirrors_ready(for_mid_term=False, for_slow=True)
        manifest_dir = Path(cfg.snapshot_manifest_dir).resolve()
        manifest_dir.mkdir(parents=True, exist_ok=True)
        cmap = resolve_production_canonical_mapping(canonical_mapping)
        sess_src = cleaned_session
        if sess_src is None:
            from trainer_hightier.serving.production_materialize import default_production_cleaned_session_path

            sess_src = default_production_cleaned_session_path()
        staging = manifest_dir / f"slow_patron_{run_id}.parquet"
        staging, slow_meta = materialize_production_slow_canonical_asof(
            cleaned_session_parquet=Path(sess_src),
            canonical_mapping_parquet=cmap,
            out_parquet=staging,
        )
        from trainer_hightier.serving.snapshot_freshness import read_slow_anchor_max

        anchor_max = read_slow_anchor_max(staging, None)
        if anchor_max is not None:
            slow_meta["anchor_gaming_day_max"] = anchor_max.isoformat()
        val = validate_slow_artifact(staging, manifest_grain=SLOW_PATRON_GRAIN_CANONICAL_ASOF)
        if val.hard_failure:
            raise ValueError(f"slow staging validation failed: {val.message}")

        prev = read_active_manifest()
        prev_raw = dict(prev.raw) if prev is not None else {}
        cov_iso = datetime.now(timezone.utc).isoformat()
        payload = _merge_manifest_payload(
            previous=prev_raw,
            updates={
                "version": run_id,
                "slow_patron_parquet": str(staging.resolve()),
                "slow_patron_grain": SLOW_PATRON_GRAIN_CANONICAL_ASOF,
                MANIFEST_KEY_SLOW_GENERATED_AT: cov_iso,
                MANIFEST_KEY_SLOW_ANCHOR_MAX: slow_meta.get("anchor_gaming_day_max"),
                MANIFEST_KEY_SLOW_MONTHLY_GRACE_DAYS: int(cfg.slow_monthly_grace_days),
                MANIFEST_KEY_SLOW_STALE_HARD_CAP_DAYS: int(cfg.slow_stale_hard_cap_days),
            },
        )
        publish_manifest_atomic(payload)
        update_watermark("slow", cov_iso)
        log_job_finish(run_id, status="ok", detail=str(staging))
        return staging
    except Exception as exc:
        log_job_finish(run_id, status="error", detail=str(exc)[:1800])
        raise


def _resolve_fe_derived_source_for_publish(
    *,
    fe_derived_source: Optional[Path],
    previous_path: Optional[Path],
) -> Optional[Path]:
    """Pick a local ``fe_derived`` parquet to copy into the staged manifest (no network I/O)."""

    if fe_derived_source is not None and Path(fe_derived_source).is_file():
        return Path(fe_derived_source).resolve()
    if previous_path is not None and Path(previous_path).is_file():
        return Path(previous_path).resolve()
    repo_fe = (
        Path(__file__).resolve().parents[1] / "artifacts" / "training_data" / "_main_trainer_fe_derived.parquet"
    )
    if repo_fe.is_file():
        return repo_fe.resolve()
    return None


def run_snapshot_updater(
    *,
    bundle_dir: Optional[Path] = None,
    rematerialize_slow: bool = False,
    production: bool = False,
    cleaned_session: Optional[Path] = None,
    cleaned_bet: Optional[Path] = None,
    canonical_mapping: Optional[Path] = None,
    fe_derived_source: Optional[Path] = None,
) -> Path:
    """Build staging artifacts and atomically flip ``active_manifest.json``.

    When ``production=True``, materialize Route B suppliers (canonical ASOF slow + production
    ``fe_derived`` from cleaned bet) instead of copying training artifacts.
    """
    cfg = default_hightier_serving_config()
    init_feature_state_db()
    run_id = _utc_run_id()
    log_job_start(run_id, detail="snapshot_updater")
    try:
        bundle = load_hightier_model_bundle(bundle_dir=bundle_dir)
        cutoff = infer_training_cutoff_iso(bundle.training_metrics)
        manifest_dir = Path(cfg.snapshot_manifest_dir).resolve()
        manifest_dir.mkdir(parents=True, exist_ok=True)
        staging_slow = manifest_dir / f"slow_patron_{run_id}.parquet"
        cmap = resolve_production_canonical_mapping(canonical_mapping)

        if production:
            sess_src = cleaned_session
            if sess_src is None:
                from trainer_hightier.serving.production_materialize import default_production_cleaned_session_path

                sess_src = default_production_cleaned_session_path()
            bet_src_for_validation = cleaned_bet
            if bet_src_for_validation is None:
                from trainer_hightier.serving.production_materialize import default_production_cleaned_bet_path

                bet_src_for_validation = default_production_cleaned_bet_path()
            ensure_production_mirrors_ready(
                for_mid_term=True,
                for_slow=True,
                cleaned_bet=Path(bet_src_for_validation),
                cleaned_session=Path(sess_src),
            )
            staging_slow, slow_meta = materialize_production_slow_canonical_asof(
                cleaned_session_parquet=Path(sess_src),
                canonical_mapping_parquet=cmap,
                out_parquet=staging_slow,
            )
            slow_grain = SLOW_PATRON_GRAIN_CANONICAL_ASOF
            slow_val = validate_slow_artifact(staging_slow, manifest_grain=slow_grain)
            if slow_val.hard_failure:
                raise ValueError(f"slow patron validation failed: {slow_val.message}")
        elif rematerialize_slow:
            out = materialize_slow_patron_180d_monthly(
                cleaned_session_parquet=cleaned_session,
                canonical_mapping_parquet=canonical_mapping,
                cleaned_bet_parquet=cleaned_bet,
                out_parquet=staging_slow,
            )
            staging_slow = Path(out).resolve()
            slow_grain = None
            slow_meta = {}
        else:
            src = default_slow_patron_180d_monthly_parquet_path()
            if not src.is_file():
                raise FileNotFoundError(
                    f"default slow patron parquet missing ({src}); run materialization or pass --production"
                )
            shutil.copy2(src, staging_slow)
            slow_grain = None
            slow_meta = {}

        if cfg.adt_allowed_players_parquet is not None:
            al_src = Path(cfg.adt_allowed_players_parquet).resolve()
        else:
            al_src = default_adt_allowed_players_parquet_path(float(cfg.adt_allowlist_quantile)).resolve()
        if not al_src.is_file():
            raise FileNotFoundError(
                f"adt allowlist source missing ({al_src}); sync training artifact or set adt_allowed_players_parquet"
            )
        staging_allow = manifest_dir / f"adt_allowed_players_{run_id}.parquet"
        shutil.copy2(al_src, staging_allow)
        al_sha = sha256_file(staging_allow)
        al_rows = parquet_player_id_row_count(staging_allow)

        prev = read_active_manifest()
        trial_path = None
        if prev is not None and prev.trial_bet_behavior_parquet is not None:
            p = prev.trial_bet_behavior_parquet
            if p.is_file():
                trial_path = str(p.resolve())

        fe_staged: Optional[Path] = None
        fe_short_staged: Optional[Path] = None
        mid_staged: Optional[Path] = None
        fe_meta: dict = {}
        mid_meta: dict = {}
        cov_iso = datetime.now(timezone.utc).isoformat()
        fe_split = _resolve_model_fe_split(bundle_dir) if production else {"short_term": (), "mid_term": ()}
        short_cols = short_term_enrich_columns_with_dependencies(
            fe_split["short_term"],
            fe_split["mid_term"],
        )
        if production:
            bet_src = cleaned_bet
            if bet_src is None:
                from trainer_hightier.serving.production_materialize import default_production_cleaned_bet_path

                bet_src = default_production_cleaned_bet_path()
            serving_day = serving_gaming_day(close_hour=int(cfg.gaming_day_close_hour))
            anchor_end = serving_day - timedelta(days=1)
            mid_staged = manifest_dir / f"mid_term_daily_{run_id}.parquet"
            mid_staged, mid_meta = materialize_production_mid_term_daily_snapshot(
                cleaned_bet_parquet=Path(bet_src),
                canonical_mapping_parquet=cmap,
                adt_allowlist_parquet=al_src,
                out_parquet=mid_staged,
                anchor_gaming_day_start=anchor_end - timedelta(days=1),
                anchor_gaming_day_end=anchor_end,
            )
            mid_val = validate_mid_term_artifact(mid_staged)
            if mid_val.hard_failure:
                raise ValueError(f"mid-term validation failed: {mid_val.message}")
            if short_cols:
                fe_short_staged = manifest_dir / f"fe_short_term_{run_id}.parquet"
                fe_short_staged, fe_meta = materialize_production_fe_short_term(
                    cleaned_bet_parquet=Path(bet_src),
                    adt_allowlist_parquet=al_src,
                    out_parquet=fe_short_staged,
                    canonical_mapping_parquet=cmap,
                    short_term_columns=short_cols,
                    coverage_hours=int(cfg.production_fe_coverage_hours),
                )
                fe_staged = fe_short_staged
            cov_raw = fe_meta.get("coverage_end_exclusive") or mid_meta.get("coverage_end_exclusive")
            if isinstance(cov_raw, str) and cov_raw.strip():
                cov_iso = cov_raw.strip()
        else:
            prev_fe: Optional[Path] = None
            if prev is not None:
                prev_fe = prev.fe_derived_parquet
            fe_src = _resolve_fe_derived_source_for_publish(
                fe_derived_source=fe_derived_source, previous_path=prev_fe
            )
            if fe_src is not None:
                fe_staged = manifest_dir / f"fe_derived_{run_id}.parquet"
                shutil.copy2(fe_src, fe_staged)

        payload = {
            "version": run_id,
            "slow_patron_parquet": str(staging_slow.resolve()),
            "trial_bet_behavior_parquet": trial_path,
            MANIFEST_KEY_FE_DERIVED: str(fe_staged.resolve()) if fe_staged is not None else None,
            MANIFEST_KEY_FE_SHORT_TERM: str(fe_short_staged.resolve()) if fe_short_staged is not None else None,
            MANIFEST_KEY_MID_TERM_SNAPSHOT: str(mid_staged.resolve()) if mid_staged is not None else None,
            "adt_allowlist_parquet": str(staging_allow.resolve()),
            "adt_allowlist_version": al_sha,
            "coverage_end_exclusive": cov_iso,
            MANIFEST_KEY_MID_TERM_COVERAGE_END: cov_iso if mid_staged is not None else None,
            "training_cutoff_iso": cutoff,
            "model_version": bundle.model_version,
        }
        if production:
            payload["fe_derived_source_kind"] = FE_DERIVED_SOURCE_KIND_PRODUCTION
            payload[MANIFEST_KEY_MID_TERM_GRAIN] = MID_TERM_GRAIN_CANONICAL_DAILY_ASOF
            payload[MANIFEST_KEY_MID_TERM_STALE_HARD_CAP_DAYS] = int(cfg.mid_term_stale_hard_cap_days)
            payload[MANIFEST_KEY_SLOW_MONTHLY_GRACE_DAYS] = int(cfg.slow_monthly_grace_days)
            payload[MANIFEST_KEY_SLOW_STALE_HARD_CAP_DAYS] = int(cfg.slow_stale_hard_cap_days)
            payload["slow_patron_grain"] = slow_grain or SLOW_PATRON_GRAIN_CANONICAL_ASOF
            payload[MANIFEST_KEY_MID_TERM_GENERATED_AT] = cov_iso if mid_staged is not None else None
            payload[MANIFEST_KEY_MID_TERM_ANCHOR_MAX] = mid_meta.get("mid_term_anchor_gaming_day_max")
            if fe_meta:
                payload["fe_derived_coverage_start"] = fe_meta.get("coverage_start")
                payload["fe_derived_row_count"] = fe_meta.get("row_count")
                payload["fe_derived_distinct_bet_count"] = fe_meta.get("distinct_bet_count")
            if slow_meta:
                payload["slow_patron_row_count"] = slow_meta.get("row_count")
                payload[MANIFEST_KEY_SLOW_GENERATED_AT] = cov_iso
                payload[MANIFEST_KEY_SLOW_ANCHOR_MAX] = slow_meta.get("anchor_gaming_day_max")

        publish_manifest_atomic(payload)
        upsert_adt_allowlist_meta(
            artifact_path=staging_allow,
            version=al_sha,
            sha256_hex=al_sha,
            row_count=al_rows,
        )
        update_watermark("slow", cov_iso)
        if fe_staged is not None:
            update_watermark("fe_derived", cov_iso)
        if mid_staged is not None:
            update_watermark("mid_term", cov_iso)
        log_job_finish(run_id, status="ok", detail=str(staging_slow))
        return staging_slow
    except Exception as exc:
        log_job_finish(run_id, status="error", detail=str(exc)[:1800])
        raise


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="trainer_hightier snapshot updater (daily)")
    p.add_argument("--bundle-dir", type=Path, default=None)
    p.add_argument("--rematerialize-slow", action="store_true")
    p.add_argument(
        "--production",
        action="store_true",
        help="Route B: materialize production fe_short_term + mid_term + canonical ASOF slow",
    )
    p.add_argument(
        "--refresh-mid-term",
        action="store_true",
        help="Refresh only mid-term canonical daily snapshot (D-1 anchor)",
    )
    p.add_argument(
        "--refresh-slow",
        action="store_true",
        help="Refresh only slow patron canonical ASOF snapshot",
    )
    p.add_argument(
        "--bootstrap-mid-term",
        action="store_true",
        help="When refreshing mid-term, build anchors {D-2, D-1}",
    )
    p.add_argument("--cleaned-session", type=Path, default=None)
    p.add_argument("--cleaned-bet", type=Path, default=None)
    p.add_argument("--canonical-mapping", type=Path, default=None)
    p.add_argument("--fe-derived-source", type=Path, default=None)
    args = p.parse_args(argv)
    if args.refresh_mid_term:
        run_mid_term_refresh(
            bundle_dir=args.bundle_dir,
            cleaned_bet=args.cleaned_bet,
            canonical_mapping=args.canonical_mapping,
            bootstrap=bool(args.bootstrap_mid_term),
        )
        return 0
    if args.refresh_slow:
        run_slow_refresh(
            cleaned_session=args.cleaned_session,
            canonical_mapping=args.canonical_mapping,
        )
        return 0
    run_snapshot_updater(
        bundle_dir=args.bundle_dir,
        rematerialize_slow=bool(args.rematerialize_slow),
        production=bool(args.production),
        cleaned_session=args.cleaned_session,
        cleaned_bet=args.cleaned_bet,
        canonical_mapping=args.canonical_mapping,
        fe_derived_source=args.fe_derived_source,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
