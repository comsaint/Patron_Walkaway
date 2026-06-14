"""High-tier patron objective: alert-band precision at fixed operational capacity.

This package is intentionally small vs ``trainer/``. Wire real IO and
segmentation in later steps.

**DuckDB:** :class:`DuckDbRuntimeConfig` is the package-local SSOT for ephemeral
connection PRAGMAs used across this package. Values are **not** read from
``trainer/`` (e.g. ``trainer.core.config.get_duckdb_memory_config``).
Other packages may still *call* shared helpers; only **config** stays local here.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Final, Literal

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
# Inclusive lower bound on ``gaming_day_event`` for L0 session/bet preprocess (2024 shards excluded).
DEFAULT_L0_PREPROCESS_GAMING_DAY_EVENT_MIN: Final[date] = date(2025, 1, 1)
# Inclusive lower bound on ``gaming_day_event`` for training rows (Step 3 output → Step 5).
# ``None`` keeps the full L0-preprocessed scope; set a later date only for explicit fast experiments.
DEFAULT_TRAINING_GAMING_DAY_EVENT_MIN: Final[date | None] = None

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
MID_TERM_ANCHOR_AUDIT_COLUMN: Final[str] = "mid_term_anchor_gaming_day_event"
MID_TERM_SNAPSHOT_AGE_AUDIT_COLUMN: Final[str] = "mid_term_snapshot_age_days"
MID_TERM_SNAPSHOT_MISSING_AUDIT_COLUMN: Final[str] = "mid_term_snapshot_missing_flag"
# Manifest keys for Route B production feature suppliers.
MANIFEST_KEY_FE_DERIVED_SOURCE_KIND: Final[str] = "fe_derived_source_kind"
MANIFEST_KEY_SLOW_PATRON_GRAIN: Final[str] = "slow_patron_grain"
MANIFEST_KEY_MID_TERM_SNAPSHOT: Final[str] = "mid_term_snapshot_parquet"
MANIFEST_KEY_MID_TERM_GRAIN: Final[str] = "mid_term_grain"
MANIFEST_KEY_MID_TERM_ANCHOR_MAX: Final[str] = "mid_term_anchor_gaming_day_event_max"
MANIFEST_KEY_MID_TERM_COVERAGE_END: Final[str] = "mid_term_coverage_end_exclusive"
MANIFEST_KEY_MID_TERM_GENERATED_AT: Final[str] = "mid_term_generated_at"
MANIFEST_KEY_MID_TERM_STALE_HARD_CAP_DAYS: Final[str] = "mid_term_stale_hard_cap_days"
# Manifest JSON key (legacy string). Semantics: short-term **offline PIT cache** path — see
# ``MANIFEST_KEY_SHORT_TERM_PIT_CACHE`` and ``doc/ssot/Scorer Runtime Contract - SSOT.md`` §Short-term.
MANIFEST_KEY_FE_SHORT_TERM: Final[str] = "fe_short_term_parquet"
MANIFEST_KEY_SHORT_TERM_PIT_CACHE: Final[str] = MANIFEST_KEY_FE_SHORT_TERM
MANIFEST_KEY_SLOW_ANCHOR_MAX: Final[str] = "slow_anchor_gaming_day_event_max"
MANIFEST_KEY_SLOW_ANCHOR_TARGET: Final[str] = "slow_anchor_target"
MANIFEST_KEY_SLOW_ANCHOR_EFFECTIVE: Final[str] = "slow_anchor_effective"
MANIFEST_KEY_SLOW_MONTH_TURN_PHASE: Final[str] = "slow_month_turn_phase"
MANIFEST_KEY_SLOW_GENERATED_AT: Final[str] = "slow_generated_at"
SLOW_SNAPSHOT_SCOPE_PRODUCTION: Final[str] = "production"
SLOW_SNAPSHOT_SCOPE_DIAGNOSTIC: Final[str] = "diagnostic"
SLOW_SNAPSHOT_SCOPE_TRAINING: Final[str] = "training_model_contract"
MANIFEST_KEY_SLOW_MONTHLY_GRACE_DAYS: Final[str] = "slow_monthly_grace_days"
MANIFEST_KEY_SLOW_STALE_HARD_CAP_DAYS: Final[str] = "slow_stale_hard_cap_days"
MID_TERM_GRAIN_CANONICAL_DAILY_ASOF: Final[str] = "canonical_daily_asof"
FE_DERIVED_SOURCE_KIND_PRODUCTION: Final[str] = "production_clickhouse"
FE_DERIVED_SOURCE_KIND_SHIPPED: Final[str] = "shipped_training_bundle"
SLOW_PATRON_GRAIN_CANONICAL_ASOF: Final[str] = "canonical_asof"
SLOW_PATRON_GRAIN_BET: Final[str] = "bet_grain"
MID_TERM_SNAPSHOT_DEPLOY_PARQUET_BASENAME: Final[str] = "mid_term_daily_snapshot.parquet"
# Deploy/bundle filename (legacy). Content: bet-grain short-term **PIT cache**, not mid-style aggregates.
FE_SHORT_TERM_DEPLOY_PARQUET_BASENAME: Final[str] = "fe_short_term_features.parquet"
SHORT_TERM_PIT_CACHE_DEPLOY_BASENAME: Final[str] = FE_SHORT_TERM_DEPLOY_PARQUET_BASENAME
# Step 3.5 training artifact: per-row PIT values for training ``bet_id`` only.
TRAINING_SHORT_TERM_PIT_CACHE_BASENAME: Final[str] = "_main_trainer_fe_short_term.parquet"
TRAINING_TXN_LITE_CACHE_BASENAME: Final[str] = "_main_trainer_txn_lite.parquet"
# L0 ``t_casino_txn`` source integration (see Data pipeline SSOT §5.2).
TXN_L0_RAW_ROOT: Final[Path] = _REPO_ROOT / "data" / "t_casino_txn"
TXN_L0_CLEANED_ROOT: Final[Path] = (
    TRAINER_HIGHTIER_PACKAGE_DIR / "artifacts" / "cleaned" / "cleaned__gmwds_t_casino_txn"
)
TXN_L0_CLEANING_POLICY_ID: Final[str] = "t_casino_txn_l0_v2_schema"
TXN_L0_SOURCE_CONTRACT_REF: Final[str] = "doc/ssot/Data pipeline - SSOT.md#5.2"
TXN_L0_REGISTRY_TABLE_KEY: Final[str] = "gmwds_t_casino_txn"
TXN_L0_EVENT_TIME_COLUMN: Final[str] = "start_dtm"
TXN_L0_OBSERVED_AT_COLUMN: Final[str] = "__etl_insert_Dtm"
TXN_L0_LOGICAL_KEY_COLUMN: Final[str] = "casino_txn_id"
TXN_L0_INGEST_FIX_RULE_ID: Final[str] = "TXN-INGEST-FIX-001"
TXN_L0_PARTIAL_MIN_POST_DEDUP_ROWS: Final[int] = 100_000
TXN_L0_PARTIAL_ROW_RATIO_VS_MEDIAN: Final[float] = 0.20
TXN_L0_PARTIAL_MAX_SHARD_COUNT: Final[int] = 3
# Feature-experiment Wave 1b: ``t_casino_txn`` txn_lite (BUYIN/CASHOUT only; see FND-19).
DEFAULT_T_CASINO_TXN_CLEANED_ROOT: Final[Path] = TXN_L0_CLEANED_ROOT
TXN_LITE_CLEANING_POLICY_ID: Final[str] = "t_casino_txn_l0_v2_schema+l1_fnd19"
TXN_LITE_SOURCE_CONTRACT_REF: Final[str] = "doc/ssot/Data pipeline - SSOT.md#5.2"
TXN_LITE_MATERIALIZER_VERSION: Final[str] = "txn_lite_cashflow_v1_l0"
TXN_LITE_INCLUDED_TYPES: Final[tuple[str, ...]] = ("BUYIN", "CASHOUT")
TXN_LITE_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "txn__has_cash_out__w15m",
    "txn__cash_out_cnt__w1h",
    "txn__cash_out_sum__w1h",
    "txn__net_cash_out_flag__w1h",
    "txn__net_cash_flow__w1h",
    "txn__buyin_cash_sum__w1h",
    "txn__buyin_prize_redemption_flag__w1h",
)
# Window ablation probes (experiment-only; not in feature_candidate_registry until Gate 1 passes).
TXN_LITE_WINDOW_ABLATION_EXTRA_HOURS: Final[tuple[int, ...]] = (4, 24)


def txn_lite_feature_columns(*, extra_window_hours: tuple[int, ...] = ()) -> tuple[str, ...]:
    """Return ``txn__*`` output columns for materialize (base v0 + optional longer-window sums)."""

    extra_cols: list[str] = []
    for hours in extra_window_hours:
        if hours <= 1:
            continue
        suffix = f"w{hours}h"
        extra_cols.extend(
            (
                f"txn__cash_out_sum__{suffix}",
                f"txn__buyin_cash_sum__{suffix}",
                f"txn__net_cash_flow__{suffix}",
            ),
        )
    return tuple(dict.fromkeys(TXN_LITE_FEATURE_COLUMNS + tuple(extra_cols)))
# Month-sharded short-term PIT cache under ``artifacts/training_data/cache/``.
SHORT_TERM_PIT_CACHE_DIRNAME: Final[str] = "short_term_pit_v1"
SHORT_TERM_PIT_CACHE_SCHEMA_VERSION: Final[int] = 2
SHORT_TERM_PIT_SUPPLIER_FAMILY: Final[str] = "short_term:w1h"
# Training materialize batch size (decoupled from scorer cycle size for offline throughput).
DEFAULT_TRAINING_SHORT_TERM_MATERIALIZE_BATCH_SIZE: Final[int] = 100_000
STEP35_MISS_PATH_INDEXED_REPLAY: Final[str] = "indexed_replay"
STEP35_MISS_PATH_BOUNDED: Final[str] = "bounded_duckdb"
DEFAULT_STEP35_MISS_PATH: Final[str] = STEP35_MISS_PATH_INDEXED_REPLAY
INDEXED_REPLAY_GATE_MODE_HARD_FE: Final[str] = "hard_fe_soft_bet_pack"
INDEXED_REPLAY_MATERIALIZER_VERSION: Final[str] = "indexed_replay_emit_opt_v1"
LEGACY_BET_PACK_1H_COLUMNS: Final[tuple[str, ...]] = (
    "bet__bets_cnt__w1h",
    "bet__wager_sum__w1h",
    "bet__back_bet_ratio__w1h",
    "bet__payout_odds_avg__w1h",
)
LEGACY_BET_PACK_WAIVER_MAX_MISMATCH_RATIO: Final[float] = 1e-4
LEGACY_BET_PACK_WAIVER_ROOT_CAUSE: Final[str] = (
    "indexed full-month replay includes canonical alias player history that the "
    "bounded 2000-row batch pool does not include"
)
# Neighbor months included when invalidating shards after partition inventory deltas.
SHORT_TERM_PIT_SOURCE_NEIGHBOR_MONTHS: Final[int] = 1
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
#: Offline train/eval player-level alert cooldown (Step 5 operational simulation).
#: 120m chosen from the 2026-06-12 episode policy study: cooldowns >=120m and the
#: episode-cap policy tie within seed noise, all beat 60m; 120m is the simplest.
PLAYER_ALERT_COOLDOWN_MIN: Final[int] = 120
LABELS_CANONICAL_SHARD_COUNT: Final[int] = 32
DEFAULT_USE_SHARDED_LABELS_CACHE: Final[bool] = False
LABEL_LOOKAHEAD_MIN: Final[int] = 45
#: Bet availability delay before scoring (minutes). Keep >= ingest_delay_cap_sec (122s)
#: from preprocess_l0_data_contract_registry so ``prediction_visible_ts_cf`` guarantees
#: synthetic observed-at visibility at score time.
BET_AVAIL_DELAY_MIN: Final[int] = 2
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
FEAST_REFRESH_SUPERVISOR_POLL_SECONDS: Final[int] = 300
FEAST_BACKGROUND_REFRESH_LOCK_WAIT_SECONDS: Final[int] = 0
#: Option B bounded ASOF: max gaming-day age of mid-term anchor vs bet ``gaming_day`` (inclusive 1..N).
PRODUCTION_MID_ASOF_BACKFILL_DAYS: Final[int] = 30
#: Bootstrap mid-term Feast refresh: anchor days materialized (Option B N-day window).
PRODUCTION_MID_FEAST_BOOTSTRAP_ANCHOR_DAYS: Final[int] = PRODUCTION_MID_ASOF_BACKFILL_DAYS
#: Hard-fail when sampled mid Feast columns exceed this null fraction (aligns with training ~5%).
SCORER_FEAST_MID_CELL_NULL_FAIL_FRACTION: Final[float] = 0.05
#: Log WARNING when a scorer cycle Feast online lookup exceeds this latency (ms).
SCORER_FEAST_LOOKUP_LATENCY_WARN_MS: Final[float] = 500.0
#: Validator INFO heartbeat when no alerts are verified in a cycle.
DEPLOY_VALIDATOR_HEARTBEAT_SECONDS: Final[int] = 300
#: Informational allowlist coverage target for ops dashboards (not enforced as a hard gate).
SCORER_FEAST_MID_TARGET_CANONICAL_COVERAGE_FRACTION: Final[float] = 0.95
#: Shipped in deploy bundles for Feast bootstrap seed (training mid snapshot copy).
MID_TERM_BOOTSTRAP_SEED_PARQUET_BASENAME: Final[str] = "mid_term_bootstrap_seed.parquet"
#: Mid columns checked for cell-null smoke (core daily snapshot primitives; ASOF parity focus).
SCORER_FEAST_MID_SMOKE_COLUMNS: Final[tuple[str, ...]] = (
    "fe__bets_cnt__w1d",
    "fe__wager_sum__w1d",
    "fe__bets_cnt__w7d",
    "fe__wager_sum__w7d",
    "fe__bets_cnt__w30d",
    "fe__wager_sum__w30d",
    "fe__std_wager_w7d",
    "fe__avg_abs_wager_w7d",
)
#: Default training mid snapshot used to seed production Feast bootstrap (repo-local).
DEFAULT_TRAINING_MID_SNAPSHOT_PARQUET: Final[str] = (
    "trainer_hightier/artifacts/training_data/_main_trainer_mid_term_daily_snapshot.parquet"
)

#: Short-term 1h pack (``bet__*`` column names). Same supplier as other short PIT features.
#: Prefer this name in new code/docs; ``SHORT_TERM_TRIAL_BET_COLUMNS`` is a backward-compatible alias.
SHORT_TERM_PACK_1H_COLUMNS: Final[tuple[str, ...]] = (
    "bet__bets_cnt__w1h",
    "bet__wager_sum__w1h",
    "bet__back_bet_ratio__w1h",
    "bet__payout_odds_avg__w1h",
)
SHORT_TERM_TRIAL_BET_COLUMNS: Final[tuple[str, ...]] = SHORT_TERM_PACK_1H_COLUMNS

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
class L0PreprocessDataScopeConfig:
    """Inclusive ``gaming_day_event`` bounds applied when cleaning raw L0 Parquet."""

    gaming_day_event_min: date | None = DEFAULT_L0_PREPROCESS_GAMING_DAY_EVENT_MIN
    gaming_day_event_max: date | None = None

    def manifest_block(self) -> dict[str, str | None]:
        """JSON-serializable fragment for preprocess cache manifests."""
        return {
            "gaming_day_event_min": (
                self.gaming_day_event_min.isoformat() if self.gaming_day_event_min is not None else None
            ),
            "gaming_day_event_max": (
                self.gaming_day_event_max.isoformat() if self.gaming_day_event_max is not None else None
            ),
        }


# Unbounded scope for unit tests that use synthetic pre-2025 fixtures.
L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED: Final[L0PreprocessDataScopeConfig] = L0PreprocessDataScopeConfig(
    gaming_day_event_min=None,
    gaming_day_event_max=None,
)


@dataclass(frozen=True)
class TrainingDataScopeConfig:
    """Inclusive ``gaming_day_event`` bounds applied to training rows after Step 3."""

    gaming_day_event_min: date | None = DEFAULT_TRAINING_GAMING_DAY_EVENT_MIN
    gaming_day_event_max: date | None = None

    def manifest_block(self) -> dict[str, str | None]:
        """JSON-serializable fragment for run reports."""
        return {
            "gaming_day_event_min": (
                self.gaming_day_event_min.isoformat() if self.gaming_day_event_min is not None else None
            ),
            "gaming_day_event_max": (
                self.gaming_day_event_max.isoformat() if self.gaming_day_event_max is not None else None
            ),
        }


# Unbounded training scope for unit tests.
TRAINING_DATA_SCOPE_TEST_UNBOUNDED: Final[TrainingDataScopeConfig] = TrainingDataScopeConfig(
    gaming_day_event_min=None,
    gaming_day_event_max=None,
)

DEFAULT_RECENT_FULL_MONTHS: Final[int] = 3
DATA_COMPLETENESS_MODE_WARN: Final[str] = "warn"
DATA_COMPLETENESS_MODE_STRICT: Final[str] = "strict"
TRAINING_RUN_KIND_SPEED: Final[str] = "speed_iteration"
TRAINING_RUN_KIND_RELEASE: Final[str] = "release_promoted"
NEG_SAMPLE_SCOPE_TRAIN_ONLY: Final[str] = "train_only"
DataCompletenessMode = Literal["warn", "strict"]
TrainingRunKind = Literal["speed_iteration", "release_promoted"]


def _policy_blob_sha256(payload: dict[str, object]) -> str:
    """Return SHA-256 hex digest of a sorted JSON policy blob."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class TrainingScopePolicy:
    """Target-row horizon policy (SSOT TA-001 / TA-005).

    When ``recent_full_months`` is ``None``, horizon filtering is disabled (legacy all-history
    rows within ``TrainingDataScopeConfig``).
    """

    recent_full_months: int | None = None  # experiment: recent 6 full months (was 3)
    include_current_partial_month: bool = True  # ... and include current partial month
    as_of_date: date | None = None
    data_completeness_mode: DataCompletenessMode = DATA_COMPLETENESS_MODE_WARN

    def manifest_block(self) -> dict[str, object]:
        """JSON-serializable policy fragment for run artifacts."""
        return {
            "recent_full_months": self.recent_full_months,
            "include_current_partial_month": bool(self.include_current_partial_month),
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date is not None else None,
            "data_completeness_mode": str(self.data_completeness_mode),
        }


@dataclass(frozen=True)
class ResolvedTrainingScope:
    """Resolved target months and date bounds for a training run."""

    policy: TrainingScopePolicy
    as_of_date: date
    horizon_enabled: bool
    target_months: tuple[str, ...]
    partial_target_months: frozenset[str]
    target_start_date: date | None
    target_end_date: date | None

    def manifest_block(self) -> dict[str, object]:
        """JSON-serializable resolved scope for run artifacts."""
        return {
            **self.policy.manifest_block(),
            "horizon_enabled": bool(self.horizon_enabled),
            "as_of_date_resolved": self.as_of_date.isoformat(),
            "target_months": list(self.target_months),
            "partial_target_months": sorted(self.partial_target_months),
            "target_start_date": (
                self.target_start_date.isoformat() if self.target_start_date is not None else None
            ),
            "target_end_date": (
                self.target_end_date.isoformat() if self.target_end_date is not None else None
            ),
        }


@dataclass(frozen=True)
class SamplePolicy:
    """Train-only negative downsampling policy (SSOT TA-008)."""

    neg_sample_frac: float = 1.0  # default off; speed runs may lower in trainer CLI later
    neg_sample_seed: int = DEFAULT_RANDOM_SEED
    neg_sample_scope: str = NEG_SAMPLE_SCOPE_TRAIN_ONLY

    def __post_init__(self) -> None:
        frac = float(self.neg_sample_frac)
        if not (0.0 < frac <= 1.0):
            raise ValueError(
                f"neg_sample_frac must be in (0, 1], got {self.neg_sample_frac!r}",
            )
        if str(self.neg_sample_scope) != NEG_SAMPLE_SCOPE_TRAIN_ONLY:
            raise ValueError(
                f"neg_sample_scope must be {NEG_SAMPLE_SCOPE_TRAIN_ONLY!r}, "
                f"got {self.neg_sample_scope!r}",
            )

    def manifest_block(self) -> dict[str, object]:
        """JSON-serializable sampling policy fragment."""
        return {
            "neg_sample_frac": float(self.neg_sample_frac),
            "neg_sample_seed": int(self.neg_sample_seed),
            "neg_sample_scope": str(self.neg_sample_scope),
            "enabled": float(self.neg_sample_frac) < 1.0,
        }


@dataclass(frozen=True)
class FeatureScreeningPolicy:
    """Optional pre-Step-5 feature screening hook (default off)."""

    enabled: bool = False
    manifest_path: Path | None = None

    def manifest_block(self) -> dict[str, object]:
        """JSON-serializable screening policy fragment."""
        mp = self.manifest_path
        return {
            "enabled": bool(self.enabled),
            "manifest_path": str(mp.resolve()) if mp is not None else None,
        }


def _shift_calendar_month(yyyymm: str, *, delta_months: int) -> str:
    """Shift ``YYYYMM`` by *delta_months* on the calendar axis."""
    ym = str(yyyymm).strip()
    if len(ym) != 6 or not ym.isdigit():
        raise ValueError(f"month must be six YYYYMM digits, got {yyyymm!r}")
    y = int(ym[:4])
    m = int(ym[4:6])
    m += int(delta_months)
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}{m:02d}"


def _month_last_day(year: int, month: int) -> date:
    """Return the last calendar day of ``year``/``month``."""
    _, last = calendar.monthrange(year, month)
    return date(year, month, last)


def resolve_training_scope(
    policy: TrainingScopePolicy,
    *,
    as_of: date | None = None,
) -> ResolvedTrainingScope:
    """Resolve target months and inclusive date bounds from horizon policy."""
    as_of_date = policy.as_of_date or as_of or date.today()
    if policy.recent_full_months is None:
        return ResolvedTrainingScope(
            policy=policy,
            as_of_date=as_of_date,
            horizon_enabled=False,
            target_months=(),
            partial_target_months=frozenset(),
            target_start_date=None,
            target_end_date=None,
        )
    recent = int(policy.recent_full_months)
    if recent <= 0:
        raise ValueError(f"recent_full_months must be positive when set, got {recent!r}")
    anchor_y, anchor_m = as_of_date.year, as_of_date.month
    anchor_ym = f"{anchor_y:04d}{anchor_m:02d}"
    full_months = [_shift_calendar_month(anchor_ym, delta_months=-offset) for offset in range(recent, 0, -1)]
    target_months = list(full_months)
    partial: set[str] = set()
    if policy.include_current_partial_month:
        if anchor_ym not in target_months:
            target_months.append(anchor_ym)
        partial.add(anchor_ym)
    earliest_ym = min(target_months)
    start = date(int(earliest_ym[:4]), int(earliest_ym[4:6]), 1)
    if partial:
        end = as_of_date
    else:
        latest_ym = max(full_months)
        end = _month_last_day(int(latest_ym[:4]), int(latest_ym[4:6]))
    return ResolvedTrainingScope(
        policy=policy,
        as_of_date=as_of_date,
        horizon_enabled=True,
        target_months=tuple(sorted(target_months)),
        partial_target_months=frozenset(partial),
        target_start_date=start,
        target_end_date=end,
    )


def training_scope_policy_fingerprint(resolved: ResolvedTrainingScope) -> str:
    """Fingerprint for target horizon policy (distinct from L0 ``training_scope_fingerprint``)."""
    return _policy_blob_sha256(
        {
            "kind": "training_scope_policy_v1",
            **resolved.manifest_block(),
        },
    )


def sample_policy_fingerprint(policy: SamplePolicy) -> str:
    """Fingerprint for train-only negative downsampling policy."""
    return _policy_blob_sha256({"kind": "sample_policy_v1", **policy.manifest_block()})


def feature_selection_policy_fingerprint(policy: FeatureScreeningPolicy) -> str:
    """Fingerprint for optional feature screening hook policy."""
    return _policy_blob_sha256({"kind": "feature_screening_policy_v1", **policy.manifest_block()})


def validate_sample_policy_for_run(
    policy: SamplePolicy,
    *,
    run_kind: TrainingRunKind,
) -> None:
    """Fail-fast when release runs attempt downsampling."""
    if run_kind == TRAINING_RUN_KIND_RELEASE and float(policy.neg_sample_frac) != 1.0:
        raise ValueError(
            "release_promoted runs require neg_sample_frac=1.0; "
            f"got neg_sample_frac={policy.neg_sample_frac!r}",
        )


def validate_feature_screening_policy(policy: FeatureScreeningPolicy) -> None:
    """Fail-fast when screening is enabled without a readable manifest path."""
    if not policy.enabled:
        return
    if policy.manifest_path is None:
        raise ValueError("feature_screening enabled=True requires manifest_path")
    path = Path(policy.manifest_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"feature_screening manifest not found at {path}")


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
    #: Drop rows outside inclusive ``gaming_day_event`` bounds after HK day derivation.
    data_scope: L0PreprocessDataScopeConfig = field(default_factory=L0PreprocessDataScopeConfig)


@dataclass(frozen=True)
class BetPreprocessConfig:
    """L0 ``t_bet`` → cleaned Parquet (DQ + registry synthetic observed-at + bulk episode tags).

    Only ``engine=\"duckdb\"`` is implemented (full-table single ``COPY``, like session default).
    ``preprocess_registry_yaml`` defaults to ``<repo>/schema/preprocess_l0_data_contract_registry.yaml`` when omitted.

    ``dedup_hash_buckets``: split ``ROW_NUMBER`` dedup by ``mod(abs(hash(bet_id)), N)`` so each
    bucket processes ~1/N of keys at a time (lower peak RAM on huge tables). ``1`` disables bucketing.

    **ADT patron segment:** when ``adt_filter_quantile`` is set (e.g. ``0.95``), keep only bets whose
    ``player_id`` appears in ``adt_allowed_players_parquet`` (one row per allowed ``player_id``, written
    upstream from ``patron_profile_csv`` + ``canonical_mapping_parquet`` via ADT quantile threshold).
    ``patron_profile_csv`` / ``canonical_mapping_parquet`` remain on the config for DuckDB joins and
    trainer orchestration; the bet disk-cache fingerprint does **not** bind cleaned session stats.
    Paths are normally injected by :func:`trainer_hightier.trainer.prepare_training_frame`.
    """

    engine: str = "duckdb"
    preprocess_registry_yaml: Path | None = None
    dedup_hash_buckets: int = 16
    adt_filter_quantile: float | None = None
    patron_profile_csv: Path | None = None
    canonical_mapping_parquet: Path | None = None
    adt_allowed_players_parquet: Path | None = None
    #: Drop rows outside inclusive ``gaming_day_event`` bounds after HK day derivation.
    data_scope: L0PreprocessDataScopeConfig = field(default_factory=L0PreprocessDataScopeConfig)


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
    """Defaults for high-tier segment + threshold selection / reporting."""

    # Quantile in (0, 1) on patron **ADT** (from ``canonical_patron_profile.csv``): bet preprocess keeps
    # only bets tied (via canonical mapping) to patrons at or above this ADT quantile (~top ``1 - q``).
    # Align naming with ``trainer.training.high_roller_segmentation`` when wiring segment thresholds.
    theo_train_quantile: float = 0.95
    #: ``alert_band_precision``: maximize operational precision at ``target_alerts_per_hour``.
    #: ``min_precision``: legacy recall@precision-floor picker (backward compatibility).
    selection_policy: Literal["alert_band_precision", "min_precision"] = "alert_band_precision"
    #: Target operational alert rates (alerts/hour) for band objective and reporting.
    target_alerts_per_hour: tuple[float, ...] = (1.0, 2.0)
    #: Deployed score threshold is calibrated to this alerts/hour target on validation.
    deployment_target_alerts_per_hour: float = 1.0
    #: Legacy precision floor when ``selection_policy='min_precision'``.
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
# Production incident debug bundle (``collect_diag.py``).
DIAG_BUNDLE_SCHEMA_VERSION: Final[int] = 1
DIAG_BUNDLE_RETENTION_COUNT: Final[int] = 3
DIAG_BUNDLE_EXPORTS_SUBDIR: Final[str] = "diag_exports"
MLFLOW_DIAG_ARTIFACT_PREFIX: Final[str] = "prod_diag"
DIAG_SUPPLIER_RCA_MAX_BETS: Final[int] = 5000


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
    #: When set, drop split rows whose ``canonical_id`` is absent from this slow monthly Parquet.
    slow_patron_parquet: Path | None = None


@dataclass(frozen=True)
class OptunaFloatParamRange:
    """Inclusive Optuna ``trial.suggest_float`` bounds."""

    low: float
    high: float
    log: bool = False

    def __post_init__(self) -> None:
        if float(self.low) >= float(self.high):
            raise ValueError(
                f"OptunaFloatParamRange requires low < high; got low={self.low!r}, high={self.high!r}",
            )


@dataclass(frozen=True)
class OptunaIntParamRange:
    """Inclusive Optuna ``trial.suggest_int`` bounds."""

    low: int
    high: int

    def __post_init__(self) -> None:
        if int(self.low) >= int(self.high):
            raise ValueError(
                f"OptunaIntParamRange requires low < high; got low={self.low!r}, high={self.high!r}",
            )


@dataclass(frozen=True)
class Step5LgbFixedConfig:
    """LightGBM kwargs held fixed across baseline and Optuna runs."""

    objective: str = "binary"
    metric: str = "binary_logloss"
    verbosity: int = -1
    n_jobs: int = -1
    device: str | None = None
    gpu_device_id: int = 0


@dataclass(frozen=True)
class Step5OptunaSearchConfig:
    """Optuna hyperparameter search space for Step 5 LightGBM tuning.

    Defaults bias toward shallower trees, larger leaf support, and non-trivial L1/L2
    so TPE cannot explore the deep ``num_leaves`` / near-zero regularization region
    that overfit validation recall in prior runs.
    """

    learning_rate: OptunaFloatParamRange = field(
        default_factory=lambda: OptunaFloatParamRange(0.02, 0.15, log=True),
    )
    num_leaves: OptunaIntParamRange = field(
        default_factory=lambda: OptunaIntParamRange(16, 31),
    )
    max_depth: OptunaIntParamRange = field(
        default_factory=lambda: OptunaIntParamRange(4, 10),
    )
    min_child_samples: OptunaIntParamRange = field(
        default_factory=lambda: OptunaIntParamRange(50, 250),
    )
    subsample: OptunaFloatParamRange = field(
        default_factory=lambda: OptunaFloatParamRange(0.6, 0.9),
    )
    colsample_bytree: OptunaFloatParamRange = field(
        default_factory=lambda: OptunaFloatParamRange(0.6, 0.9),
    )
    reg_alpha: OptunaFloatParamRange = field(
        default_factory=lambda: OptunaFloatParamRange(0.0, 1.0),
    )
    reg_lambda: OptunaFloatParamRange = field(
        default_factory=lambda: OptunaFloatParamRange(0.1, 5.0, log=True),
    )


@dataclass(frozen=True)
class PlayerAlertPolicyConfig:
    """Shared train/serve player-level alert suppression policy."""

    suppression_enabled: bool = True
    cooldown_min: int = PLAYER_ALERT_COOLDOWN_MIN
    threshold_selection_enabled: bool = False
    sample_weight_enabled: bool = False


@dataclass(frozen=True)
class Step5TrainConfig:
    """Step 5: one LightGBM on Step 4 Parquet splits; optional Optuna with time budget."""

    run_step5: bool = True
    #: When ``True``, use :data:`baseline_*` hyperparameters only (no Optuna).
    skip_optuna: bool = False
    #: ``study.optimize(..., timeout=...)`` wall-clock cap in seconds.
    optuna_timeout_sec: float = 60 * 60  # Optuna wall-clock budget (1h default)
    #: Optuna TPE search bounds (ignored when ``skip_optuna`` is ``True``).
    optuna_search: Step5OptunaSearchConfig = field(default_factory=Step5OptunaSearchConfig)
    #: LightGBM kwargs not tuned by Optuna (objective, metric, verbosity, n_jobs).
    lgb_fixed: Step5LgbFixedConfig = field(default_factory=Step5LgbFixedConfig)
    early_stopping_rounds: int = 30
    #: Upper bound on boosting rounds (early stopping usually stops sooner).
    lgb_n_estimators_cap: int = 2000
    #: Baseline LightGBM params (``--skip-optuna``); centered inside :attr:`optuna_search`.
    baseline_learning_rate: float = 0.05
    baseline_num_leaves: int = 24
    baseline_max_depth: int = 8
    baseline_min_child_samples: int = 100
    baseline_subsample: float = 0.7
    baseline_colsample_bytree: float = 0.7
    baseline_reg_alpha: float = 0.1
    baseline_reg_lambda: float = 1.0
    #: When ``True``, final artifact model refits on train+val (test remains holdout-only).
    refit_train_plus_val: bool = False
    player_alert_policy: PlayerAlertPolicyConfig = field(default_factory=PlayerAlertPolicyConfig)


@dataclass(frozen=True)
class PreTrainFeatureGateConfig:
    """Step 4.5: short-term PIT train vs live replay gate before Step 5."""

    run_pre_train_gate: bool = True
    max_rows: int = 200_000
    batch_size: int = 2000
    diff_fraction_fail_threshold: float = 0.02


@dataclass(frozen=True)
class Step6ParityConfig:
    """Step 6: post-training train/serve parity + deploy-ready E2E gate."""

    run_step6: bool = True
    hard_fail_slow_gate: bool = True
    hard_fail_all_feature_gate: bool = True
    hard_fail_deploy_e2e_gate: bool = True
    #: Fail all-feature gate when any compared feature exceeds this train/serve diff fraction.
    all_feature_diff_fraction_fail_threshold: float = 0.02
    max_rows: int = 200_000
    batch_size: int = 2000
    #: When False, Step 6 skips full short-layer replay (covered by Step 4.5).
    run_short_full_replay_in_step6: bool = True
    #: Optional short smoke replay on a subsample (diagnostic only).
    run_short_smoke_in_step6: bool = False
    #: Wall-clock budget for parity + deploy bundle build + deploy E2E (seconds).
    step6_total_timeout_seconds: int = 1800
    #: Retry the full Step 6 sequence once on failure (shared timeout window).
    step6_auto_retry_once: bool = True
    #: Run production-like deploy E2E (fresh bundle venv) after parity.
    step6_deploy_e2e_enabled: bool = True
    #: Scorability sample size for deploy E2E gate (keep small for 30-minute Step 6 budget).
    step6_deploy_e2e_max_bets: int = 500
    #: Compare training short-term features against raw ``t_bet`` partition recompute.
    run_raw_source_sanity: bool = True
    hard_fail_raw_source_sanity: bool = True
    raw_source_sanity_max_rows: int = 200
    raw_source_undercount_ratio_threshold: float = 2.0
    raw_source_undercount_fail_fraction: float = 0.02


PRE_TRAIN_FEATURE_GATE_JSON_BASENAME: Final[str] = "pre_train_feature_gate.json"


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
    prediction_validation_enabled: bool = True
    prediction_validation_max_rows_per_cycle: int = 200
    prediction_validation_cycle_budget_seconds: float = 20.0
    prediction_validation_retention_days: int = 180
    scorer_state_retention_hours: int = 24
    bet_avail_delay_min: int = BET_AVAIL_DELAY_MIN
    session_avail_delay_min: int = 15
    scorer_poll_interval_seconds: float = 30.0
    scorer_feast_lookup_latency_warn_ms: float = SCORER_FEAST_LOOKUP_LATENCY_WARN_MS
    deploy_validator_heartbeat_seconds: int = DEPLOY_VALIDATOR_HEARTBEAT_SECONDS
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
    feast_refresh_supervisor_poll_seconds: int = FEAST_REFRESH_SUPERVISOR_POLL_SECONDS
    feast_background_refresh_lock_wait_seconds: int = FEAST_BACKGROUND_REFRESH_LOCK_WAIT_SECONDS
    #: Deploy / dry-run allowlist sample size for Feast online lookup smoke.
    scorer_feast_deploy_lookup_smoke_sample_size: int = SCORER_FEAST_DEPLOY_LOOKUP_SMOKE_SAMPLE_SIZE
    #: Bounded ASOF window (gaming days) for mid-term train/serve parity (Option B).
    production_mid_asof_backfill_days: int = PRODUCTION_MID_ASOF_BACKFILL_DAYS
    #: Bootstrap anchor-day span for mid-term Feast online refresh (Option B N-day window).
    production_mid_feast_bootstrap_anchor_days: int = PRODUCTION_MID_FEAST_BOOTSTRAP_ANCHOR_DAYS
    #: Hard-fail smoke when mid Feast cell null rate exceeds this fraction.
    scorer_feast_mid_cell_null_fail_fraction: float = SCORER_FEAST_MID_CELL_NULL_FAIL_FRACTION
    #: Informational allowlist coverage target (logged only; not a hard refresh gate).
    scorer_feast_mid_target_canonical_coverage_fraction: float = (
        SCORER_FEAST_MID_TARGET_CANONICAL_COVERAGE_FRACTION
    )
    #: Mid columns used for cell-null smoke gate (core ASOF parity primitives).
    scorer_feast_mid_smoke_columns: tuple[str, ...] = SCORER_FEAST_MID_SMOKE_COLUMNS
    #: Optional training mid snapshot parquet for bootstrap seed; ``None`` uses package default when present.
    training_mid_snapshot_parquet: Path | None = None
    player_alert_policy: PlayerAlertPolicyConfig = field(
        default_factory=lambda: PlayerAlertPolicyConfig(cooldown_min=ALERT_HORIZON_MIN),
    )


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


def apply_hightier_serving_environ_overrides(cfg: HightierServingConfig) -> HightierServingConfig:
    """Overlay ClickHouse fields from process env (after optional bundle ``.env`` load).

    Recognized variables: ``CH_HOST``, ``CH_PORT``, ``CH_USER``, ``CH_PASS`` or
    ``CH_PASSWORD``, ``CH_SECURE``, ``SOURCE_DB``.
    """
    kw: dict[str, object] = {}
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
        raw = str(os.environ.get("CH_SECURE", "")).strip().lower()
        kw["ch_secure"] = raw in ("1", "true", "yes", "on")
    if (v := str(os.environ.get("SOURCE_DB", "")).strip()):
        kw["source_db"] = v
    if not kw:
        return cfg
    return replace(cfg, **kw)


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
