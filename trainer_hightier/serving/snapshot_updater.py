"""Daily (or manual) snapshot publish: versioned slow-feature Parquet + atomic manifest switch."""

from __future__ import annotations

import argparse
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from trainer_hightier.config import default_hightier_serving_config
from trainer_hightier.serving.feature_supply import MANIFEST_KEY_FE_DERIVED
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
from trainer_hightier.serving.model_bundle import infer_training_cutoff_iso, load_hightier_model_bundle
from trainer_hightier.utils.patron_session_metrics import default_adt_allowed_players_parquet_path
from trainer_hightier.utils.slow_patron_180d_monthly import (
    default_slow_patron_180d_monthly_parquet_path,
    materialize_slow_patron_180d_monthly,
)

logger = logging.getLogger(__name__)


def _utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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
    cleaned_session: Optional[Path] = None,
    cleaned_bet: Optional[Path] = None,
    canonical_mapping: Optional[Path] = None,
    fe_derived_source: Optional[Path] = None,
) -> Path:
    """Build staging artifacts and atomically flip ``active_manifest.json``.

    Default path refreshes the repo-default slow patron Parquet (requires cleaned inputs when
    ``rematerialize_slow``) else copies the last known artifact into a versioned filename.
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

        if rematerialize_slow:
            out = materialize_slow_patron_180d_monthly(
                cleaned_session_parquet=cleaned_session,
                canonical_mapping_parquet=canonical_mapping,
                cleaned_bet_parquet=cleaned_bet,
                out_parquet=staging_slow,
            )
            staging_slow = Path(out).resolve()
        else:
            src = default_slow_patron_180d_monthly_parquet_path()
            if not src.is_file():
                raise FileNotFoundError(
                    f"default slow patron parquet missing ({src}); run materialization or pass --rematerialize-slow"
                )
            shutil.copy2(src, staging_slow)

        if cfg.adt_allowed_players_parquet is not None:
            al_src = Path(cfg.adt_allowed_players_parquet).resolve()
        else:
            al_src = default_adt_allowed_players_parquet_path(float(cfg.adt_allowlist_quantile)).resolve()
        if not al_src.is_file():
            raise FileNotFoundError(f"adt allowlist source missing ({al_src}); sync training artifact or set adt_allowed_players_parquet")
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

        prev_fe: Optional[Path] = None
        if prev is not None:
            prev_fe = prev.fe_derived_parquet
        fe_src = _resolve_fe_derived_source_for_publish(fe_derived_source=fe_derived_source, previous_path=prev_fe)
        fe_staged: Optional[Path] = None
        if fe_src is not None:
            fe_staged = manifest_dir / f"fe_derived_{run_id}.parquet"
            shutil.copy2(fe_src, fe_staged)

        cov_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "version": run_id,
            "slow_patron_parquet": str(staging_slow.resolve()),
            "trial_bet_behavior_parquet": trial_path,
            MANIFEST_KEY_FE_DERIVED: str(fe_staged.resolve()) if fe_staged is not None else None,
            "adt_allowlist_parquet": str(staging_allow.resolve()),
            "adt_allowlist_version": al_sha,
            "coverage_end_exclusive": cov_iso,
            "training_cutoff_iso": cutoff,
            "model_version": bundle.model_version,
        }
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
    p.add_argument("--cleaned-session", type=Path, default=None)
    p.add_argument("--cleaned-bet", type=Path, default=None)
    p.add_argument("--canonical-mapping", type=Path, default=None)
    p.add_argument("--fe-derived-source", type=Path, default=None)
    args = p.parse_args(argv)
    run_snapshot_updater(
        bundle_dir=args.bundle_dir,
        rematerialize_slow=bool(args.rematerialize_slow),
        cleaned_session=args.cleaned_session,
        cleaned_bet=args.cleaned_bet,
        canonical_mapping=args.canonical_mapping,
        fe_derived_source=args.fe_derived_source,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
