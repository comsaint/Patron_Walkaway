"""High-tier patron objective: fixed precision target → report alert rate.

This package is intentionally small vs ``trainer/``. Wire real IO and
segmentation in later steps.

**DuckDB:** :class:`DuckDbRuntimeConfig` is the package-local SSOT for ephemeral
connection PRAGMAs used across this package. Values are **not** read from
``trainer/`` (e.g. ``trainer.core.config.get_duckdb_memory_config``).
Other packages may still *call* shared helpers; only **config** stays local here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
# Installed / editable package directory ``…/trainer_hightier/`` (contracts, modules).
TRAINER_HIGHTIER_PACKAGE_DIR: Final[Path] = Path(__file__).resolve().parent
# Versioned bundle root (parity with trainer ``DEFAULT_MODEL_DIR`` under ``out/``).
DEFAULT_MODEL_DIR: Final[Path] = _REPO_ROOT / "out" / "models_high_tier_mvp"
# Default root for Frozen deploy bundles emitted by ``build_deploy_package``.
DEFAULT_DEPLOY_OUTPUT_ROOT: Final[Path] = _REPO_ROOT / "out" / "deploy_hightier"
# Shared reproducibility seed (training, FQG subsampling, Optuna sampler).
DEFAULT_RANDOM_SEED: Final[int] = 42
# Step 3 Feast feature service wired by :mod:`trainer_hightier.trainer`.
DEFAULT_TRAINING_FEATURE_SERVICE: Final[str] = "walkaway_bet_trial_v1"

# --- Shared domain constants (keep aligned with defaults in ``HightierServingConfig``) ---
HK_TZ: Final[str] = "Asia/Hong_Kong"
# Frozen next to Step 5 ``model.pkl`` for packaging/schema gate (training-time registry bytes + hash).
FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME: Final[str] = "feature_candidate_registry.snapshot.yaml"
# Copied into deploy_inputs / bundled snapshots when training uses fe__* baseline features.
FE_DERIVED_DEPLOY_PARQUET_BASENAME: Final[str] = "fe_derived_features.parquet"
# Mid-term snapshot freshness vs active_manifest.coverage_end_exclusive (ISO-8601 duration).
MID_TERM_FRESHNESS_SLA_ISO8601: Final[str] = "PT36H"
# Max gaming-day lookback when building mid-term daily snapshots (w30d + buffer).
MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS: Final[int] = 32
# Mid-term snapshot artifact scope labels (sidecar metadata + cache keys).
MID_TERM_SNAPSHOT_SCOPE_TRAINING: Final[str] = "training_step4_only"
MID_TERM_SNAPSHOT_SCOPE_PRODUCTION: Final[str] = "production"
# Training audit columns written by mid-term ASOF enrich.
MID_TERM_ANCHOR_AUDIT_COLUMN: Final[str] = "mid_term_anchor_gaming_day"
MID_TERM_SNAPSHOT_AGE_AUDIT_COLUMN: Final[str] = "mid_term_snapshot_age_days"
MID_TERM_SNAPSHOT_MISSING_AUDIT_COLUMN: Final[str] = "mid_term_snapshot_missing_flag"
# Manifest keys for Route B production feature suppliers.
MANIFEST_KEY_FE_DERIVED_SOURCE_KIND: Final[str] = "fe_derived_source_kind"
MANIFEST_KEY_SLOW_PATRON_GRAIN: Final[str] = "slow_patron_grain"
MANIFEST_KEY_MID_TERM_SNAPSHOT: Final[str] = "mid_term_snapshot_parquet"
MANIFEST_KEY_MID_TERM_GRAIN: Final[str] = "mid_term_grain"
MANIFEST_KEY_MID_TERM_ANCHOR_MAX: Final[str] = "mid_term_anchor_gaming_day_max"
MANIFEST_KEY_MID_TERM_COVERAGE_END: Final[str] = "mid_term_coverage_end_exclusive"
MANIFEST_KEY_MID_TERM_GENERATED_AT: Final[str] = "mid_term_generated_at"
MANIFEST_KEY_MID_TERM_STALE_HARD_CAP_DAYS: Final[str] = "mid_term_stale_hard_cap_days"
MANIFEST_KEY_FE_SHORT_TERM: Final[str] = "fe_short_term_parquet"
MANIFEST_KEY_SLOW_ANCHOR_MAX: Final[str] = "slow_anchor_gaming_day_max"
MANIFEST_KEY_SLOW_GENERATED_AT: Final[str] = "slow_generated_at"
MANIFEST_KEY_SLOW_MONTHLY_GRACE_DAYS: Final[str] = "slow_monthly_grace_days"
MANIFEST_KEY_SLOW_STALE_HARD_CAP_DAYS: Final[str] = "slow_stale_hard_cap_days"
MID_TERM_GRAIN_CANONICAL_DAILY_ASOF: Final[str] = "canonical_daily_asof"
FE_DERIVED_SOURCE_KIND_PRODUCTION: Final[str] = "production_clickhouse"
FE_DERIVED_SOURCE_KIND_SHIPPED: Final[str] = "shipped_training_bundle"
SLOW_PATRON_GRAIN_CANONICAL_ASOF: Final[str] = "canonical_asof"
SLOW_PATRON_GRAIN_BET: Final[str] = "bet_grain"
MID_TERM_SNAPSHOT_DEPLOY_PARQUET_BASENAME: Final[str] = "mid_term_daily_snapshot.parquet"
FE_SHORT_TERM_DEPLOY_PARQUET_BASENAME: Final[str] = "fe_short_term_features.parquet"
# Production snapshot lifecycle (HK wall-clock).
GAMING_DAY_CLOSE_HOUR: Final[int] = 3
MID_TERM_REFRESH_TARGET_HOUR: Final[int] = 4
MID_TERM_STALE_HARD_CAP_DAYS: Final[int] = 3
SLOW_MONTHLY_GRACE_DAYS: Final[int] = 1
SLOW_STALE_HARD_CAP_DAYS: Final[int] = 3
SNAPSHOT_REFRESH_SUPERVISOR_POLL_SECONDS: Final[int] = 300
SNAPSHOT_REFRESH_LOCK_STALE_MINUTES: Final[int] = 360
PRODUCTION_BET_MIRROR_RETENTION_DAYS: Final[int] = MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS + 5
PRODUCTION_SESSION_MIRROR_RETENTION_DAYS: Final[int] = 187
PRODUCTION_BET_MIRROR_REWRITE_DAYS: Final[int] = 3
PRODUCTION_BET_MIRROR_DIRNAME: Final[str] = "cleaned_bet"
PRODUCTION_SESSION_MIRROR_FILENAME: Final[str] = "cleaned_session.parquet"
# Hours of live bets included in production fe_derived micro-batch (7d windows + buffer).
PRODUCTION_FE_COVERAGE_HOURS: Final[int] = 192
PLACEHOLDER_PLAYER_ID: Final[int] = -1
CASINO_PLAYER_ID_CLEAN_SQL: Final[str] = (
    "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') "
    "THEN NULL ELSE trim(casino_player_id) END"
)
WALKAWAY_GAP_MIN: Final[int] = 30
ALERT_HORIZON_MIN: Final[int] = 15
LABEL_LOOKAHEAD_MIN: Final[int] = 45
BET_AVAIL_DELAY_MIN: Final[int] = 1
SCORER_POLL_INTERVAL_SECONDS: Final[int] = 45
#: Hard-fail scoring cycle when Feast entity row missing rate exceeds this fraction.
SCORER_FEAST_ENTITY_MISSING_FAIL_FRACTION: Final[float] = 0.10
#: Run Feast registry / online schema smoke at scorer startup when mid/long Feast columns are required.
SCORER_FEAST_SCHEMA_SMOKE_ENABLED: Final[bool] = True
#: Combined mid/slow Feast online readiness JSON (written by spike / refresh jobs).
FEAST_ONLINE_READINESS_BASENAME: Final[str] = "feast_online_readiness.json"
FEAST_ONLINE_READINESS_SCHEMA_VERSION: Final[int] = 1
#: Fail scorer startup when feast_online_readiness.json is missing or stale for required layers.
SCORER_FEAST_READINESS_ENABLED: Final[bool] = True
#: Allowlist canonical ids sampled for deploy-time Feast online lookup smoke.
SCORER_FEAST_DEPLOY_LOOKUP_SMOKE_SAMPLE_SIZE: Final[int] = 20
FEAST_STARTUP_REFRESH_LOCK_WAIT_SECONDS: Final[int] = 30

# Baseline MODEL columns: softer FQG (high PSI → WARN; unique-constant under sample → WARN; WARN auto-allowlist).
_FQG_BASELINE_MODEL_SOFT_COLUMNS: tuple[str, ...] = (
    "wager",
    "casino_win",
    "is_back_bet",
    "bet_type",
    "type_of_bet",
    "bet__bets_cnt__w1h",
    "bet__wager_sum__w1h",
    "bet__back_bet_ratio__w1h",
    "bet__payout_odds_avg__w1h",
    "patron__theo_win_sum__w180d_m1snap",
    "patron__gaming_days_cnt__w180d_m1snap",
    "patron__adt__w180d_m1snap",
)


@dataclass(frozen=True)
class FeatureQualityGateConfig:
    """Feature Quality Gate v0 thresholds (Working Plan §1.5).

    Central SSOT for FQG; no environment variables — tune here or override in callers.
    Baseline MODEL columns typically auto-approved on WARN entries (see ``warn_autoapprove_columns``).
    """

    fqg_version: str = "v0"
    random_seed: int = DEFAULT_RANDOM_SEED
    max_rows_per_split: int = 200_000
    #: L1 — treat column as categorical if cardinality <= this threshold (excluding nulls).
    low_cardinality_categorical_cutoff: int = 512
    missing_rate_block: float = 0.98
    missing_rate_warn_lo: float = 0.70
    #: Absolute difference in missing rate fraction across splits (e.g. 0.20 = 20pp).
    missing_rate_split_diff_warn: float = 0.20
    near_constant_top1_warn: float = 0.995
    categorical_unseen_frac_warn: float = 0.10
    #: ``p99_abs / max(abs(p50), eps)``.
    numeric_long_tail_warn: float = 1e4
    numeric_long_tail_eps: float = 1e-12
    #: Names containing any of these case-insensitive substrings fail PIT/leak heuristics (BLOCK).
    leakage_column_substrings: tuple[str, ...] = tuple(
        (
            "__future__",
            "_future_",
            "future_win",
            "label_future",
            "walkaway_future",
            "derived_from_label",
            "posterior_target",
            "oracle_",
            "leaked_",
        )
    )
    #: Gate 0 overlay (same sample as L1/L2): per-working-plan Gate 0.
    gate0_missing_max_frac: float = 0.40
    gate0_constant_top1_block: float = 0.995
    gate0_illegal_max_frac: float = 0.005
    psi_bin_count: int = 10
    psi_eps: float = 1e-6
    psi_pass_max: float = 0.10
    psi_warn_max: float = 0.25
    min_rows_month_slice: int = 500
    #: Monthly missing-rate stability: WARN if STRICTLY MORE than ``floor(n_months_ok/2)`` months exceed this deviation ratio vs pooled estimate.
    month_missing_rel_dev_warn: float = 2.0
    uplift_flip_frac_warn: float = 0.40
    #: ``|corr(is_nan,label)| >= this threshold in both splits but disagree in sign ⇒ WARN``.
    mnar_corr_abs_floor: float = 0.10
    #: Baseline MODEL columns: L2 PSI above ``psi_warn_max`` is **WARN** only (never BLOCK), so subsample drift does not abort the default pipeline.
    l2_psi_block_downgrade_to_warn_columns: tuple[str, ...] = _FQG_BASELINE_MODEL_SOFT_COLUMNS
    #: L1 ``nunique==1`` (across splits) becomes **WARN** for these columns (e.g. degenerate under FQG sample).
    #: Same set is used to soften **Gate0** ``top1`` constant BLOCK for baseline registry columns.
    l1_constant_unique_block_downgrade_to_warn_columns: tuple[str, ...] = _FQG_BASELINE_MODEL_SOFT_COLUMNS
    #: MODEL columns WARN without external approval file automatically stay trainable for baseline/compatibility.
    warn_autoapprove_columns: tuple[str, ...] = _FQG_BASELINE_MODEL_SOFT_COLUMNS


# Repo root (parent of ``trainer_hightier/``); used for default DuckDB spill path only.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Upper bound when auto-doubling ``dedup_hash_buckets`` after DuckDB OOM (semantics-preserving).
PREPROCESS_DEDUP_BUCKET_ESCALATION_CEILING: Final[int] = 256


@dataclass(frozen=True)
class DuckDbRuntimeConfig:
    """PRAGMA defaults for ``duckdb.connect(...)`` in ``trainer_hightier``.

    Use :func:`trainer_hightier.utils.duckdb_runtime.apply_duckdb_runtime_pragmas`
    after opening a connection. Tuning is intentionally string/Path based to
    match DuckDB's PRAGMA surface (e.g. ``memory_limit='4GB'``).
    """

    memory_limit: str = "16GB"
    temp_directory: Path | None = _PROJECT_ROOT / "tmp" / "duckdb_spill"
    threads: int | None = None
    # Cap DuckDB spill files under ``temp_directory`` (or OS default temp drive).
    # Example: ``'200GiB'`` when the default inferred cap is too low for large windows.
    max_temp_directory_size: str | None = None
    # ``false`` reduces temp spill for heavy sorts/windows (DuckDB perf guide); rare
    # FND-01 ties on all ORDER BY keys may pick a different survivor vs insertion order.
    preserve_insertion_order: bool = False


@dataclass(frozen=True)
class SessionPreprocessConfig:
    """L0 ``t_session`` → cleaned Parquet: engine choice and pandas-shard batching only.

    DuckDB memory / threads / spill path: set :class:`DuckDbRuntimeConfig` on
    ``HighTierTrainArgs.duckdb_runtime`` (or pass ``duckdb_runtime=`` into
    :func:`trainer_hightier.02_preprocess.preprocess_sessions_from_parquet_streaming`).
    """

    # ``duckdb``: one ``COPY`` from raw Parquet (DuckDB pipelines).
    # ``pandas_shards``: row-group batches → temp shard Parquets → DuckDB merge.
    engine: str = "duckdb"
    # ``dedup_hash_buckets`` (``engine=\"duckdb\"`` only): split FND-01 ``ROW_NUMBER`` dedup by
    # ``mod(abs(hash(session_id)), N)`` to cap peak RAM on huge tables. ``1`` disables bucketing.
    dedup_hash_buckets: int = 8
    # Only for ``pandas_shards``: concatenate this many row groups per shard file.
    row_groups_per_shard: int = 8


@dataclass(frozen=True)
class BetPreprocessConfig:
    """L0 ``t_bet`` → cleaned Parquet (DQ + registry synthetic observed-at + bulk episode tags).

    Only ``engine=\"duckdb\"`` is implemented (full-table single ``COPY``, like session default).
    ``preprocess_registry_yaml`` defaults to ``<repo>/schema/preprocess_l0_data_contract_registry.yaml`` when omitted.

    ``dedup_hash_buckets``: split ``ROW_NUMBER`` dedup by ``mod(abs(hash(bet_id)), N)`` so each
    bucket processes ~1/N of keys at a time (lower peak RAM on huge tables). ``1`` disables bucketing.

    **ADT patron segment:** when ``adt_filter_quantile`` is set (e.g. ``0.99``), keep only bets whose
    ``player_id`` appears in ``adt_allowed_players_parquet`` (one row per allowed ``player_id``, written
    upstream from ``patron_profile_csv`` + ``canonical_mapping_parquet`` via ADT quantile threshold).
    ``patron_profile_csv`` / ``canonical_mapping_parquet`` remain on the config for DuckDB joins and
    trainer orchestration; the bet disk-cache fingerprint does **not** bind cleaned session stats.
    Paths are normally injected by :func:`trainer_hightier.trainer.prepare_training_frame`.
    """

    engine: str = "duckdb"
    preprocess_registry_yaml: Path | None = None
    dedup_hash_buckets: int = 8
    adt_filter_quantile: float | None = None
    patron_profile_csv: Path | None = None
    canonical_mapping_parquet: Path | None = None
    adt_allowed_players_parquet: Path | None = None


@dataclass(frozen=True)
class CanonicalMappingConfig:
    """``player_id`` → ``canonical_id`` built from cleaned session Parquet (trainer D2).

    When ``cutoff_dtm`` is None, cutoff is inferred as
    ``MAX(session_end_dtm)`` then ``MAX(lud_dtm)`` over the cleaned file (HK-naive
    normalisation after inference).
    """

    enabled: bool = True
    cutoff_dtm: datetime | None = None
    legacy_coalesce_cutoff: bool = False
    # After mapping Parquet is written: aggregate ``theo_win`` / ``gaming_day`` → ADT report.
    compile_patron_session_metrics: bool = True
    # Rich per-canonical profile CSV under ``trainer_hightier/artifacts/profile/``.
    compile_patron_profile_csv: bool = True


@dataclass(frozen=True)
class HighTierObjectiveConfig:
    """Defaults for high-tier segment + precision-floor reporting."""

    # Quantile in (0, 1) on patron **ADT** (from ``canonical_patron_profile.csv``): bet preprocess keeps
    # only bets tied (via canonical mapping) to patrons at or above this ADT quantile (~top ``1 - q``).
    # Align naming with ``trainer.training.high_roller_segmentation`` when wiring segment thresholds.
    theo_train_quantile: float = 0.99
    # Require precision >= this value on the **segment** when choosing a score threshold.
    min_precision: float = 0.60
    # Placeholder paths for later steps (Parquet / DuckDB exports).
    segment_scores_parquet: Path | None = None
    labels_parquet: Path | None = None


@dataclass(frozen=True)
class HighTierRunProfile:
    """One-place preset: DuckDB resource PRAGMAs + preprocess dedup bucket counts.

    Select a profile in code (``DEFAULT_RUN_PROFILE_NAME`` / :func:`get_run_profile`) when wiring runs.
    """

    memory_limit: str
    temp_directory: Path | None
    threads: int | None
    max_temp_directory_size: str | None
    preserve_insertion_order: bool
    session_engine: str
    session_dedup_hash_buckets: int
    row_groups_per_shard: int
    bet_dedup_hash_buckets: int


def _default_paths_temp_dir() -> Path | None:
    return _PROJECT_ROOT / "tmp" / "duckdb_spill"


#: Presets keyed by ``--run-profile`` (see :func:`get_run_profile`).
RUN_PROFILES: Final[dict[str, HighTierRunProfile]] = {
    "default": HighTierRunProfile(
        memory_limit="16GB",
        temp_directory=_default_paths_temp_dir(),
        threads=2,
        max_temp_directory_size=None,
        preserve_insertion_order=False,
        session_engine="duckdb",
        session_dedup_hash_buckets=8,
        row_groups_per_shard=8,
        bet_dedup_hash_buckets=32,
    ),
    "laptop_8g": HighTierRunProfile(
        memory_limit="4GB",
        temp_directory=_default_paths_temp_dir(),
        threads=1,
        max_temp_directory_size="20GiB",
        preserve_insertion_order=False,
        session_engine="duckdb",
        session_dedup_hash_buckets=16,
        row_groups_per_shard=8,
        bet_dedup_hash_buckets=32,
    ),
    # Large union of monthly t_bet shards: heavy ROW_NUMBER dedup; lower threads + more hash
    # buckets reduce per-operator RAM vs ``default`` (see DuckDB perf guide).
    "low_peak_memory": HighTierRunProfile(
        memory_limit="10GB",
        temp_directory=_default_paths_temp_dir(),
        threads=1,
        max_temp_directory_size="250GiB",
        preserve_insertion_order=False,
        session_engine="duckdb",
        session_dedup_hash_buckets=16,
        row_groups_per_shard=8,
        bet_dedup_hash_buckets=32,
    ),
    "workstation_64g": HighTierRunProfile(
        memory_limit="48GB",
        temp_directory=_default_paths_temp_dir(),
        threads=8,
        max_temp_directory_size=None,
        preserve_insertion_order=False,
        session_engine="duckdb",
        session_dedup_hash_buckets=8,
        row_groups_per_shard=8,
        bet_dedup_hash_buckets=8,
    ),
}

DEFAULT_RUN_PROFILE_NAME: Final[str] = "default"

# MLflow (trainer_hightier): experiment namespace for training runs.
# Tracking URI loads via ``trainer_hightier.core.mlflow_adapter`` (credential/mlflow.env pattern).
MLFLOW_EXPERIMENT_TRAIN_HIGHTIER: Final[str] = "patron/patron_walkaway/prod/train_hightier"
# Artifact subfolder within each MLflow run (avoid colliding with main trainer ``model_bundle`` layout).
MLFLOW_HIGHTIER_ARTIFACT_PREFIX: Final[str] = "hightier_run"


@dataclass(frozen=True)
class PartitionIngressConfig:
    """Partition snapshot inventory diff / recompute defaults (Step 1b)."""

    #: For each changed YYYYMM, also include this many prior calendar months in recompute set.
    backfill_month_count: int = 1


@dataclass(frozen=True)
class Step4SplitConfig:
    """Deterministic train/val/test split on distinct ``gaming_day`` (calendar order).

    Fractions apply to the **ordered distinct** gaming days, not row counts.
    ``test`` receives the remainder after train and val.
    """

    train_day_fraction: float = 0.70
    val_day_fraction: float = 0.15
    #: When ``None``, defaults to ``trainer_hightier/artifacts/training_data/splits``.
    splits_output_dir: Path | None = None


@dataclass(frozen=True)
class Step5TrainConfig:
    """Step 5: one LightGBM on Step 4 Parquet splits; optional Optuna with time budget."""

    run_step5: bool = True
    #: When ``True``, use :data:`baseline_*` hyperparameters only (no Optuna).
    skip_optuna: bool = False
    #: ``study.optimize(..., timeout=...)`` wall-clock cap in seconds.
    optuna_timeout_sec: float = 60.0 * 60 * 3  # 1 hour = 60*60
    early_stopping_rounds: int = 50
    #: Upper bound on boosting rounds (early stopping usually stops sooner).
    lgb_n_estimators_cap: int = 2000
    baseline_learning_rate: float = 0.05
    baseline_num_leaves: int = 31
    baseline_min_child_samples: int = 20
    baseline_subsample: float = 0.8
    baseline_colsample_bytree: float = 0.8
    baseline_reg_lambda: float = 0.0
    #: When ``True``, final artifact model refits on train+val (test remains holdout-only).
    refit_train_plus_val: bool = True


@dataclass(frozen=True)
class HightierServingConfig:
    """ClickHouse + SQLite paths for ``trainer_hightier`` serving (no environment-variable SSOT).

    Tune credentials and retention here for laptop/production deployments.
    """

    hk_tz: str = "Asia/Hong_Kong"
    source_db: str = "GDP_GMWDS_Raw"
    tbet: str = "t_bet"
    tsession: str = "t_session"
    casino_player_id_clean_sql: str = (
        "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') "
        "THEN NULL ELSE trim(casino_player_id) END"
    )
    #: ClickHouse TCP/HTTP endpoint (``clickhouse_connect``).
    ch_host: str = "gdpedw"
    ch_port: int = 8123
    ch_user: str = ""
    ch_password: str = ""
    ch_secure: bool = False
    placeholder_player_id: int = -1
    walkaway_gap_min: int = 30
    alert_horizon_min: int = 15
    #: ``trainer`` default is ``WALKAWAY_GAP_MIN + ALERT_HORIZON_MIN``.
    label_lookahead_min: int = 45
    validator_alert_retention_days: int = 30
    validation_results_retention_days: int = 180
    validator_cache_prune_interval_seconds: int = 300
    validator_freshness_buffer_minutes: int = 2
    validator_extended_wait_minutes: int = 15
    validator_finality_hours: int = 1
    validator_finalize_on_horizon: bool = True
    validator_no_bet_bet_id_lookup_enabled: bool = True
    validator_fetch_pre_context_minutes: int = 60
    validator_fetch_max_lookback_minutes: int = 180
    validator_fetch_max_lookback_minutes_cap: int = 24 * 60
    validator_no_bet_retry_max_window_minutes: int = 240
    validator_no_bet_bet_id_chunk_size: int = 500
    validator_no_bet_retry_max_alerts: int = 50
    scorer_state_retention_hours: int = 24
    bet_avail_delay_min: int = 1
    session_avail_delay_min: int = 15
    scorer_poll_interval_seconds: float = 30.0
    #: When True and a cycle hits ``hightier_scorer_max_bets_per_cycle``, skip poll sleep (drain backlog).
    scorer_backlog_no_sleep_enabled: bool = True
    #: Allowlist incremental fetch: ``external_input`` (single JOIN query) or legacy ``chunk`` IN-lists.
    scorer_allowlist_join_mode: str = "external_input"
    #: When external-input join fails, fall back to legacy chunk queries (default fail-fast).
    scorer_allowlist_join_fallback_to_chunk: bool = False
    #: Upper bound on cold-start / backfill window for incremental fetches (hours).
    scorer_dynamic_lookback_cap_hours: int = 8
    hightier_scorer_max_bets_per_cycle: int = 2000
    #: Split ADT allowlist ``player_id`` IN-lists into chunks of this size (ClickHouse query limits).
    hightier_scorer_player_id_chunk_size: int = 500
    #: Cap rows buffered while merging chunk query results (0 = disabled). Prevents OOM if mis-tuned.
    hightier_scorer_chunk_merge_row_cap: int = 0
    #: Max distinct ``player_id`` values passed to the hot bet-pool fetch per cycle (OOM guard).
    hightier_scorer_pool_player_fanout_cap: int = 5000
    #: Hours of bet history loaded for 1h rolling features (per cycle, per player pool).
    hot_feature_pool_lookback_hours: int = 6
    state_db_path: Path = field(
        default_factory=lambda: _REPO_ROOT / "trainer_hightier" / "local_state" / "state.db"
    )
    #: All scored rows (pre-alert filter); ``None`` disables writes.
    prediction_log_db_path: Path | None = field(
        default_factory=lambda: _REPO_ROOT / "trainer_hightier" / "local_state" / "prediction_log.db"
    )
    feature_state_db_path: Path = field(
        default_factory=lambda: _REPO_ROOT / "trainer_hightier" / "local_state" / "feature_state.db"
    )
    snapshot_manifest_dir: Path = field(
        default_factory=lambda: _REPO_ROOT
        / "trainer_hightier"
        / "artifacts"
        / "serving_snapshots"
    )
    validator_out_dir: Path = field(
        default_factory=lambda: _REPO_ROOT / "trainer_hightier" / "out_validator_hightier"
    )
    default_model_versions_root: Path = field(default_factory=lambda: DEFAULT_MODEL_DIR)
    #: When True, only score bets whose ``player_id`` appears in the ADT allowlist Parquet.
    high_adt_only: bool = True
    #: Quantile slug for :func:`~trainer_hightier.utils.patron_session_metrics.default_adt_allowed_players_parquet_path`.
    #: Must match training ``theo_train_quantile`` when using default allowlist path.
    adt_allowlist_quantile: float = 0.90
    #: Explicit allowlist path; when ``None``, scorer resolves via manifest → this field → default quantile path.
    adt_allowed_players_parquet: Path | None = None
    #: If True and ``training_metrics.json`` contains ``adt_allowlist_sha256``, scorer requires an exact match.
    adt_allowlist_fail_on_training_hash_mismatch: bool = True
    #: Hours of ``payout_complete_dtm`` coverage for production ``fe_derived`` materialization.
    production_fe_coverage_hours: int = PRODUCTION_FE_COVERAGE_HOURS
    #: Calendar-day lookback for production slow 180d canonical ASOF snapshots.
    production_slow_lookback_days: int = 180
    gaming_day_close_hour: int = GAMING_DAY_CLOSE_HOUR
    mid_term_refresh_target_hour: int = MID_TERM_REFRESH_TARGET_HOUR
    mid_term_stale_hard_cap_days: int = MID_TERM_STALE_HARD_CAP_DAYS
    slow_monthly_grace_days: int = SLOW_MONTHLY_GRACE_DAYS
    slow_stale_hard_cap_days: int = SLOW_STALE_HARD_CAP_DAYS
    snapshot_refresh_supervisor_poll_seconds: int = SNAPSHOT_REFRESH_SUPERVISOR_POLL_SECONDS
    snapshot_refresh_lock_stale_minutes: int = SNAPSHOT_REFRESH_LOCK_STALE_MINUTES
    production_cleaned_bet_mirror_dir: Path | None = None
    production_cleaned_session_mirror_parquet: Path | None = None
    production_bet_mirror_retention_days: int = PRODUCTION_BET_MIRROR_RETENTION_DAYS
    production_session_mirror_retention_days: int = PRODUCTION_SESSION_MIRROR_RETENTION_DAYS
    production_bet_mirror_rewrite_days: int = PRODUCTION_BET_MIRROR_REWRITE_DAYS
    #: Batch hard-fail threshold for Feast entity row missing (see Scorer Runtime Contract SSOT).
    scorer_feast_entity_missing_fail_fraction: float = SCORER_FEAST_ENTITY_MISSING_FAIL_FRACTION
    #: Fail scorer startup when Feast registry / feature views / entity key do not match the model.
    scorer_feast_schema_smoke_enabled: bool = SCORER_FEAST_SCHEMA_SMOKE_ENABLED
    #: Probe ``canonical_id`` for startup online lookup smoke (may legitimately return empty features).
    scorer_feast_schema_smoke_probe_canonical_id: str = "__feast_scorer_smoke_probe__"
    #: When True, require ``feast_online_readiness.json`` for Feast mid/long columns at scorer startup.
    scorer_feast_readiness_enabled: bool = SCORER_FEAST_READINESS_ENABLED
    #: Override path to combined Feast readiness JSON; ``None`` uses package default under artifacts/feast.
    scorer_feast_readiness_path: Path | None = None
    #: Bundle-local Feast repo path; ``None`` uses package default ``trainer_hightier/feast_repo``.
    scorer_feast_repo_path: Path | None = None
    #: Max seconds to wait for bundle-local Feast refresh lock before fail-fast.
    feast_startup_refresh_lock_wait_seconds: int = FEAST_STARTUP_REFRESH_LOCK_WAIT_SECONDS
    #: Deploy / dry-run allowlist sample size for Feast online lookup smoke.
    scorer_feast_deploy_lookup_smoke_sample_size: int = SCORER_FEAST_DEPLOY_LOOKUP_SMOKE_SAMPLE_SIZE


_DEFAULT_HIGHTIER_SERVING: HightierServingConfig = HightierServingConfig()
# Optional one-shot override for portable deploy bundles (set before importing ``serving.runtime_config``).
_DEPLOY_SERVING_OVERRIDE: HightierServingConfig | None = None


def set_hightier_serving_deploy_override(cfg: HightierServingConfig | None) -> None:
    """Replace serving defaults for this process (deploy bundle).

    Must be called **before** the first import of :mod:`trainer_hightier.serving.runtime_config`
    so module-level path snapshots match the bundle layout. Pass ``None`` to clear.
    """
    global _DEPLOY_SERVING_OVERRIDE
    _DEPLOY_SERVING_OVERRIDE = cfg


def default_hightier_serving_config() -> HightierServingConfig:
    """Return the frozen default serving config (single-process SSOT)."""
    if _DEPLOY_SERVING_OVERRIDE is not None:
        return _DEPLOY_SERVING_OVERRIDE
    return _DEFAULT_HIGHTIER_SERVING


def list_run_profile_names() -> tuple[str, ...]:
    """Sorted :data:`RUN_PROFILES` keys (for validation UX)."""
    return tuple(sorted(RUN_PROFILES))


def get_run_profile(name: str) -> HighTierRunProfile:
    """Return the named :class:`HighTierRunProfile` or raise ``ValueError``."""
    key = str(name).strip()
    if key not in RUN_PROFILES:
        names = ", ".join(list_run_profile_names())
        raise ValueError(f"Unknown run profile {key!r}; expected one of: {names}")
    return RUN_PROFILES[key]


def configs_from_run_profile(
    profile: HighTierRunProfile,
) -> tuple[DuckDbRuntimeConfig, SessionPreprocessConfig, BetPreprocessConfig]:
    """Expand a profile into configs used by :class:`~trainer_hightier.trainer.HighTierTrainArgs`."""
    ddb = DuckDbRuntimeConfig(
        memory_limit=profile.memory_limit,
        temp_directory=profile.temp_directory,
        threads=profile.threads,
        max_temp_directory_size=profile.max_temp_directory_size,
        preserve_insertion_order=profile.preserve_insertion_order,
    )
    sess = SessionPreprocessConfig(
        engine=profile.session_engine,
        dedup_hash_buckets=profile.session_dedup_hash_buckets,
        row_groups_per_shard=profile.row_groups_per_shard,
    )
    bet = BetPreprocessConfig(dedup_hash_buckets=profile.bet_dedup_hash_buckets)
    return ddb, sess, bet
