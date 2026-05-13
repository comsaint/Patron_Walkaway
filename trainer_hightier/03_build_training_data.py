"""Offline training-set export for high-tier trainer (step 3).

Pipeline order:

1. :mod:`trainer_hightier.01_data_ingest` — raw Parquet ingress / schema QC.
2. :mod:`trainer_hightier.02_preprocess` — cleaned ``t_session`` + ``t_bet``
   (and optional walkaway labels via :mod:`trainer_hightier.trainer`).
3. **This module** — Feast ``get_historical_features`` on cleaned bet entities +
   left-join ``walkaway_labels.parquet`` → ``artifacts/training_data/``.

Invoke with ``python -m trainer_hightier.03_build_training_data`` (numeric
prefix matches steps 1–2; use :func:`importlib.import_module` if importing from code).

Steps (see :func:`build_training_data`):

1. Optional: materialize derived feature Parquets (1h trial + 180d monthly slow).
2. Write entity Parquet (``bet_id``, ``event_timestamp``) from cleaned bet.
3. ``get_historical_features`` + ``persist`` → staging feature Parquet (Ibis/DuckDB).
4. DuckDB left-join labels on ``bet_id`` → ``artifacts/training_data/training_set.parquet``.

Prerequisites: ``feast apply`` has been run from ``feast_repo``; offline store
paths in ``definitions.py`` resolve to existing materialized Parquets.

Memory / scale: Feast’s Ibis+DuckDB offline path loads the **full entity**
``DataFrame`` into memory (``bet_id`` + ``event_timestamp`` only — ~16 bytes/row
order-of-magnitude plus pandas overhead). Join and feature SQL run in Ibis/DuckDB;
ensure ``feature_store.yaml`` ``staging_location`` has disk space. Align
``duckdb`` / ``ibis-framework`` versions with repo ``requirements.txt`` (mismatches
can break ``ibis.read_parquet`` at retrieval time).
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from trainer_hightier.config import DuckDbRuntimeConfig, configs_from_run_profile, get_run_profile
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas
from trainer_hightier.utils.slow_patron_180d_monthly import (
    default_slow_patron_180d_monthly_parquet_path,
    materialize_slow_patron_180d_monthly,
)
from trainer_hightier.utils.trial_bet_behavior_1h import (
    default_trial_bet_behavior_1h_parquet_path,
    materialize_trial_bet_behavior_1h,
)

logger = logging.getLogger(__name__)

_DEFAULT_RUN_PROFILE = "default"

TRAINER_HIGHTIER_ROOT = Path(__file__).resolve().parent
REPO_ROOT = TRAINER_HIGHTIER_ROOT.parent
DEFAULT_FEAST_REPO = TRAINER_HIGHTIER_ROOT / "feast_repo"
DEFAULT_CLEANED_BET = TRAINER_HIGHTIER_ROOT / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet.parquet"
DEFAULT_LABELS = TRAINER_HIGHTIER_ROOT / "artifacts" / "labels" / "walkaway_labels.parquet"
DEFAULT_TRAINING_DIR = TRAINER_HIGHTIER_ROOT / "artifacts" / "training_data"
DEFAULT_OUTPUT = DEFAULT_TRAINING_DIR / "training_set.parquet"
DEFAULT_FEATURE_SERVICE = "walkaway_bet_trial_v1"


def _path_posix(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


@dataclass(frozen=True)
class BuildTrainingDataArgs:
    """Configuration for :func:`build_training_data`."""

    feast_repo: Path
    cleaned_bet_parquet: Path
    labels_parquet: Path
    output_parquet: Path
    feature_service_name: str
    materialize_derived_features: bool
    max_entity_rows: int | None
    duckdb_runtime: DuckDbRuntimeConfig


def _validate_prereqs(
    *,
    feast_repo: Path,
    cleaned_bet: Path,
    labels_parquet: Path,
    materialize_derived: bool,
    feature_service_name: str,
) -> None:
    reg = feast_repo / "data" / "registry.db"
    if not reg.is_file():
        raise FileNotFoundError(
            f"Feast registry missing at {reg}; run `feast apply` from {feast_repo.resolve()}."
        )
    if not cleaned_bet.is_file():
        raise FileNotFoundError(f"Cleaned bet Parquet not found: {cleaned_bet}")
    if not labels_parquet.is_file():
        raise FileNotFoundError(
            f"Labels Parquet not found: {labels_parquet} "
            f"(run trainer pipeline Step 2c or materialize_walkaway_labels_from_cleaned_bet)."
        )
    needs_derived = feature_service_name.strip() == DEFAULT_FEATURE_SERVICE
    if needs_derived and not materialize_derived:
        trial = default_trial_bet_behavior_1h_parquet_path(repo_root=REPO_ROOT)
        slow = default_slow_patron_180d_monthly_parquet_path(repo_root=REPO_ROOT)
        missing = [p for p in (trial, slow) if not p.is_file()]
        if missing:
            raise FileNotFoundError(
                "Derived Feast Parquet(s) missing (trial 1h and/or slow 180d). "
                f"Missing: {[str(m) for m in missing]}. "
                "Pass --materialize-derived to build them, use --feature-service walkaway_bet_v1 "
                "for cleaned bet features only, or materialize manually."
            )


def _maybe_materialize_derived(cfg: BuildTrainingDataArgs) -> None:
    if not cfg.materialize_derived_features:
        return
    if cfg.feature_service_name.strip() != DEFAULT_FEATURE_SERVICE:
        logger.info(
            "Skip trial/slow materialization: feature service %r !== %r.",
            cfg.feature_service_name,
            DEFAULT_FEATURE_SERVICE,
        )
        return
    logger.info("Materializing trial 1h behavior Parquet …")
    materialize_trial_bet_behavior_1h(
        cleaned_bet_parquet=cfg.cleaned_bet_parquet,
        duckdb_runtime=cfg.duckdb_runtime,
    )
    logger.info("Materializing slow patron 180d monthly Parquet …")
    materialize_slow_patron_180d_monthly(duckdb_runtime=cfg.duckdb_runtime)


def _write_entity_parquet(
    cleaned_bet: Path,
    entity_out: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
    max_rows: int | None,
) -> None:
    bet_esc = _path_posix(cleaned_bet).replace("'", "''")
    ent_esc = _path_posix(entity_out).replace("'", "''")
    lim = f"LIMIT {int(max_rows)}" if max_rows is not None else ""
    sql = f"""
COPY (
  SELECT
    TRY_CAST(bet_id AS DOUBLE) AS bet_id,
    CAST(prediction_visible_ts_cf AS TIMESTAMPTZ) AS event_timestamp
  FROM read_parquet('{bet_esc}')
  WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
    AND prediction_visible_ts_cf IS NOT NULL
  {lim}
) TO '{ent_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(sql)
    finally:
        con.close()


def _feast_features_to_parquet(
    *,
    feast_repo: Path,
    entity_parquet: Path,
    feature_service_name: str,
    features_parquet: Path,
) -> None:
    """Load entity rows as a DataFrame (Feast Ibis backend requires in-memory entity_df)."""
    import pandas as pd

    from feast import FeatureStore
    from feast.infra.offline_stores.file_source import SavedDatasetFileStorage

    entity_df = pd.read_parquet(entity_parquet)
    src = FeatureStore(repo_path=str(feast_repo.resolve()))
    try:
        svc = src.get_feature_service(feature_service_name)
    except Exception as exc:
        raise ValueError(
            f"Could not load feature service {feature_service_name!r} from registry. "
            f"Check `feast apply` and definitions.py."
        ) from exc

    job = src.get_historical_features(
        entity_df=entity_df,
        features=svc,
        full_feature_names=False,
    )
    features_parquet.parent.mkdir(parents=True, exist_ok=True)
    job.persist(
        SavedDatasetFileStorage(path=str(features_parquet.resolve())),
        allow_overwrite=True,
    )


def _training_manifest(
    *,
    output_parquet: Path,
    feast_repo: Path,
    feature_service: str,
    row_count: int | None,
    labels_parquet: Path,
) -> dict[str, Any]:
    return {
        "output_parquet": str(output_parquet.resolve()),
        "row_count": row_count,
        "feast_repo": str(feast_repo.resolve()),
        "feature_service": feature_service,
        "labels_parquet": str(labels_parquet.resolve()),
    }


def _join_labels_to_features(
    *,
    features_parquet: Path,
    labels_parquet: Path,
    output_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> int | None:
    f_esc = _path_posix(features_parquet).replace("'", "''")
    l_esc = _path_posix(labels_parquet).replace("'", "''")
    o_esc = _path_posix(output_parquet).replace("'", "''")
    sql = f"""
COPY (
  SELECT
    h.*,
    y.label AS walkaway_label,
    y.censored AS walkaway_censored
  FROM read_parquet('{f_esc}') h
  LEFT JOIN (
    SELECT
      TRY_CAST(bet_id AS DOUBLE) AS bet_id,
      CAST(label AS SMALLINT) AS label,
      CAST(censored AS BOOLEAN) AS censored
    FROM read_parquet('{l_esc}')
  ) y ON TRY_CAST(h.bet_id AS DOUBLE) = y.bet_id
) TO '{o_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(sql)
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{o_esc}')").fetchone()
        return int(n[0]) if n else None
    finally:
        con.close()


def build_training_data(cfg: BuildTrainingDataArgs) -> Path:
    """Produce ``cfg.output_parquet``; return its resolved path.

    Raises:
        FileNotFoundError: Missing inputs or derived Parquets when not materializing.
        ValueError: Feast feature service or join failure.
    """
    _validate_prereqs(
        feast_repo=cfg.feast_repo,
        cleaned_bet=cfg.cleaned_bet_parquet,
        labels_parquet=cfg.labels_parquet,
        materialize_derived=cfg.materialize_derived_features,
        feature_service_name=cfg.feature_service_name,
    )
    _maybe_materialize_derived(cfg)

    out_dir = cfg.output_parquet.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    entity_pq = out_dir / "_entity_bet_ts.parquet"
    staging_feat = out_dir / "_staging_feast_features.parquet"

    try:
        _write_entity_parquet(
            cfg.cleaned_bet_parquet,
            entity_pq,
            duckdb_runtime=cfg.duckdb_runtime,
            max_rows=cfg.max_entity_rows,
        )
        _feast_features_to_parquet(
            feast_repo=cfg.feast_repo,
            entity_parquet=entity_pq,
            feature_service_name=cfg.feature_service_name,
            features_parquet=staging_feat,
        )
        n_rows = _join_labels_to_features(
            features_parquet=staging_feat,
            labels_parquet=cfg.labels_parquet,
            output_parquet=cfg.output_parquet,
            duckdb_runtime=cfg.duckdb_runtime,
        )
    finally:
        for p in (entity_pq, staging_feat):
            if p.is_file():
                try:
                    p.unlink()
                except OSError:
                    logger.warning("Could not remove staging file %s", p)

    meta = out_dir / "training_set.manifest.json"
    meta.write_text(
        json.dumps(
            _training_manifest(
                output_parquet=cfg.output_parquet,
                feast_repo=cfg.feast_repo,
                feature_service=cfg.feature_service_name,
                row_count=n_rows,
                labels_parquet=cfg.labels_parquet,
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("Training set written %s (rows=%s)", cfg.output_parquet.resolve(), n_rows)
    return cfg.output_parquet.resolve()


def _parse_args() -> BuildTrainingDataArgs:
    p = argparse.ArgumentParser(description="Feast features + labels → training Parquet.")
    p.add_argument(
        "--feast-repo",
        type=Path,
        default=DEFAULT_FEAST_REPO,
        help="Feast feature repo (default: trainer_hightier/feast_repo).",
    )
    p.add_argument(
        "--cleaned-bet",
        type=Path,
        default=DEFAULT_CLEANED_BET,
        help="Cleaned t_bet Parquet.",
    )
    p.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABELS,
        help="walkaway_labels.parquet (bet_id, label, censored).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output training Parquet path.",
    )
    p.add_argument(
        "--feature-service",
        type=str,
        default=DEFAULT_FEATURE_SERVICE,
        help="Feast feature service name (default: walkaway_bet_trial_v1).",
    )
    p.add_argument(
        "--materialize-derived",
        action="store_true",
        help="Run trial 1h + slow 180d materializers before Feast (heavy).",
    )
    p.add_argument(
        "--max-entity-rows",
        type=int,
        default=None,
        help="Limit entity rows for debugging (LIMIT in entity Parquet).",
    )
    p.add_argument(
        "--run-profile",
        type=str,
        default=_DEFAULT_RUN_PROFILE,
        help="DuckDB PRAGMA preset for non-Feast steps (see config.RUN_PROFILES).",
    )
    ns = p.parse_args()
    duckdb_rt, _, _ = configs_from_run_profile(get_run_profile(ns.run_profile))
    return BuildTrainingDataArgs(
        feast_repo=Path(ns.feast_repo).resolve(),
        cleaned_bet_parquet=Path(ns.cleaned_bet).resolve(),
        labels_parquet=Path(ns.labels).resolve(),
        output_parquet=Path(ns.output).resolve(),
        feature_service_name=str(ns.feature_service),
        materialize_derived_features=bool(ns.materialize_derived),
        max_entity_rows=ns.max_entity_rows,
        duckdb_runtime=duckdb_rt,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    build_training_data(_parse_args())


if __name__ == "__main__":
    main()
