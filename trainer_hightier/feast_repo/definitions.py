"""Feast entity / feature views / feature services for trainer_hightier.

前置條件：清洗後的 bet Parquet 必須含 ``prediction_visible_ts_cf``（由
``trainer_hightier`` bet preprocess 寫入）。若尚未重跑 preprocess，``feast plan``
未加 ``--skip-source-validation`` 時可能因缺欄或時間欄檢核而失敗。

離線擷取：見 ``feature_store.yaml`` 的 ``offline_store.type: duckdb``（需
``ibis-framework``；見 repo 根目錄 ``requirements.txt``）。

DuckDB 離線路徑：``FileSource`` 必須帶 ``file_format=ParquetFormat()``，否則
Feast 0.63 的 ``_read_data_source`` 在 ``file_format is None`` 時會回傳 ``None``，
``get_historical_features`` 觸發 ``'NoneType' object has no attribute 'mutate'``。

Explicit ``schema``：避免 Feast 對 ``gaming_day`` 的 ``date32`` 做 schema inference
時觸發不支援的型別對應（與本 repo 的 ``feast==0.63.0`` 行為一致）。

Trial：需先執行 ``trainer_hightier.utils.trial_bet_behavior_1h.materialize_trial_bet_behavior_1h``
產生 ``artifacts/feast/trial_bet_behavior_1h.parquet`` 後，``trial_bet_behavior_1h_features``
與 ``walkaway_bet_trial_v1`` 才可通過來源檢核。

Slow patron：需先執行 ``trainer_hightier.utils.slow_patron_180d_monthly.materialize_slow_patron_180d_monthly``
產生 ``artifacts/feast/slow_patron_180d_monthly.parquet`` 後，``slow_patron_180d_monthly_features`` 方可通過檢核。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from feast import Entity, FeatureService, FeatureView, Field, FileSource
from feast.data_format import ParquetFormat
from feast.types import Float64, Int64, String, UnixTimestamp
from feast.value_type import ValueType

_REPO_ROOT = Path(__file__).resolve().parent
_CLEANED_BET_DATASET = (_REPO_ROOT.parent / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet").resolve()
# Glob all partition shards; hive-style directories use ``hive_partitioning=false`` in DuckDB reads.
_bet_source_path = str((_CLEANED_BET_DATASET / "**" / "*.parquet").resolve()).replace("\\", "/")

bet = Entity(
    name="bet",
    join_keys=["bet_id"],
    value_type=ValueType.DOUBLE,
    description="Bet grain after L0 DQ + dedup; join key matches Parquet ``bet_id``.",
)

cleaned_bet_source = FileSource(
    name="cleaned_bet_parquet",
    path=_bet_source_path,
    file_format=ParquetFormat(),
    timestamp_field="prediction_visible_ts_cf",
    created_timestamp_column="__etl_insert_Dtm_synthetic",
)

cleaned_bet_features = FeatureView(
    name="cleaned_bet_features",
    entities=[bet],
    ttl=timedelta(days=30),
    schema=[
        Field(name="bet_id", dtype=Float64),
        Field(name="session_id", dtype=Float64),
        Field(name="player_id", dtype=Int64),
        Field(name="game_id", dtype=Float64),
        Field(name="table_id", dtype=Float64),
        Field(name="payout_complete_dtm", dtype=UnixTimestamp),
        Field(name="__etl_insert_Dtm", dtype=UnixTimestamp),
        Field(name="wager", dtype=Float64),
        Field(name="status", dtype=String),
        Field(name="casino_win", dtype=Float64),
        Field(name="payout_odds", dtype=Float64),
        Field(name="payout_ha", dtype=Float64),
        Field(name="base_ha", dtype=Float64),
        Field(name="is_back_bet", dtype=Int64),
        Field(name="position_idx", dtype=Float64),
        Field(name="position_code", dtype=String),
        Field(name="position_label", dtype=String),
        Field(name="bet_type", dtype=String),
        Field(name="type_of_bet", dtype=String),
        Field(name="commission", dtype=Float64),
        Field(name="max_wager", dtype=Float64),
        Field(name="std_dev", dtype=Float64),
        Field(name="theo_win", dtype=Float64),
        Field(name="theo_win_cash", dtype=Float64),
        Field(name="true_odds", dtype=Float64),
        Field(name="adjusted_theo_win", dtype=Float64),
        Field(name="is_settled", dtype=Int64),
        Field(name="bet_payout_type", dtype=String),
        Field(name="mixed_stack", dtype=Int64),
        Field(name="auto_resolve_stack", dtype=Int64),
        Field(name="__ts_ms", dtype=Int64),
        Field(name="ingestion_episode_id", dtype=String),
    ],
    source=cleaned_bet_source,
    tags={
        "owner": "trainer_hightier",
        "semantics": "counterfactual_pit",
        "contract": "trainer_hightier/contracts/time_semantics_and_feast_mapping.md",
    },
)

_TRIAL_1H_PARQUET = (_REPO_ROOT.parent / "artifacts" / "feast" / "trial_bet_behavior_1h.parquet").resolve()
_trial_1h_source_path = str(_TRIAL_1H_PARQUET)

trial_bet_behavior_1h_source = FileSource(
    name="trial_bet_behavior_1h_parquet",
    path=_trial_1h_source_path,
    file_format=ParquetFormat(),
    timestamp_field="prediction_visible_ts_cf",
    created_timestamp_column="__etl_insert_Dtm_synthetic",
)

trial_bet_behavior_1h_features = FeatureView(
    name="trial_bet_behavior_1h_features",
    entities=[bet],
    ttl=timedelta(hours=2),
    schema=[
        Field(name="bet_id", dtype=Float64),
        Field(name="bet__bets_cnt__w1h", dtype=Int64),
        Field(name="bet__wager_sum__w1h", dtype=Float64),
        Field(name="bet__back_bet_ratio__w1h", dtype=Float64),
        Field(name="bet__payout_odds_avg__w1h", dtype=Float64),
    ],
    source=trial_bet_behavior_1h_source,
    tags={
        "owner": "trainer_hightier",
        "semantics": "counterfactual_pit",
        "trial": "1h_player_clock_lookback",
        "contract": "trainer_hightier/contracts/trial_bet_behavior_1h_features.yaml",
    },
)

_SLOW_180_PARQUET = (_REPO_ROOT.parent / "artifacts" / "feast" / "slow_patron_180d_monthly.parquet").resolve()
_slow_180_source_path = str(_SLOW_180_PARQUET)

slow_patron_180d_monthly_source = FileSource(
    name="slow_patron_180d_monthly_parquet",
    path=_slow_180_source_path,
    file_format=ParquetFormat(),
    timestamp_field="prediction_visible_ts_cf",
    created_timestamp_column="__etl_insert_Dtm_synthetic",
)

slow_patron_180d_monthly_features = FeatureView(
    name="slow_patron_180d_monthly_features",
    entities=[bet],
    ttl=timedelta(days=50),
    schema=[
        Field(name="bet_id", dtype=Float64),
        Field(name="patron__theo_win_sum__w180d_m1snap", dtype=Float64),
        Field(name="patron__gaming_days_cnt__w180d_m1snap", dtype=Int64),
        Field(name="patron__adt__w180d_m1snap", dtype=Float64),
    ],
    source=slow_patron_180d_monthly_source,
    tags={
        "owner": "trainer_hightier",
        "semantics": "counterfactual_pit",
        "cadence": "monthly_first_gaming_day_snapshot",
        "contract": "trainer_hightier/contracts/slow_patron_180d_monthly_features.yaml",
    },
)

walkaway_bet_trial_v1 = FeatureService(
    name="walkaway_bet_trial_v1",
    features=[
        cleaned_bet_features,
        trial_bet_behavior_1h_features,
        slow_patron_180d_monthly_features,
    ],
)

walkaway_bet_v1 = FeatureService(
    name="walkaway_bet_v1",
    features=[cleaned_bet_features],
)

# Subset services for independent offline retrieval + disk cache keys (Step 3 decomposed path).
# After editing this file, run ``feast apply`` from ``trainer_hightier/feast_repo``.
walkaway_bet_trial_clock_v1 = FeatureService(
    name="walkaway_bet_trial_clock_v1",
    features=[trial_bet_behavior_1h_features],
)

walkaway_bet_slow_snap_v1 = FeatureService(
    name="walkaway_bet_slow_snap_v1",
    features=[slow_patron_180d_monthly_features],
)

# --- Canonical-grain mid-term spike (production feasibility; see feast_mid_term_spike.py) ---
_SPIKE_MID_TERM_PARQUET = (
    _REPO_ROOT.parent / "artifacts" / "feast" / "mid_term_spike_canonical.parquet"
).resolve()
_spike_mid_term_source_path = str(_SPIKE_MID_TERM_PARQUET)

canonical_patron = Entity(
    name="canonical_patron",
    join_keys=["canonical_id"],
    value_type=ValueType.STRING,
    description="Patron grain for mid-term daily snapshots (canonical_id).",
)

mid_term_spike_canonical_source = FileSource(
    name="mid_term_spike_canonical_parquet",
    path=_spike_mid_term_source_path,
    file_format=ParquetFormat(),
    timestamp_field="event_timestamp",
)

mid_term_daily_spike_features = FeatureView(
    name="mid_term_daily_spike_features",
    entities=[canonical_patron],
    ttl=timedelta(days=40),
    schema=[
        Field(name="fe__bets_cnt__w1d", dtype=Int64),
        Field(name="fe__wager_sum__w1d", dtype=Float64),
        Field(name="fe__bets_cnt__w7d", dtype=Int64),
        Field(name="fe__wager_sum__w7d", dtype=Float64),
        Field(name="fe__bets_cnt__w30d", dtype=Int64),
        Field(name="fe__wager_sum__w30d", dtype=Float64),
        Field(name="fe__prior_wager_mean_w30d", dtype=Float64),
        Field(name="fe__prior_wager_std_w30d", dtype=Float64),
        Field(name="fe__prior_odds_mean_w30d", dtype=Float64),
        Field(name="fe__prior_odds_std_w30d", dtype=Float64),
        Field(name="fe__std_wager_w7d", dtype=Float64),
        Field(name="fe__avg_abs_wager_w7d", dtype=Float64),
        Field(name="fe__interarrival_avg_w7d", dtype=Float64),
        Field(name="fe__interarrival_std_w7d", dtype=Float64),
        Field(name="fe__max_pcd_w7d", dtype=UnixTimestamp),
        Field(name="fe__min_pcd_w7d", dtype=UnixTimestamp),
    ],
    source=mid_term_spike_canonical_source,
    tags={
        "owner": "trainer_hightier",
        "cadence": "canonical_daily_asof",
        "spike": "feast_mid_term_feasibility",
    },
)

walkaway_canonical_mid_term_spike_v1 = FeatureService(
    name="walkaway_canonical_mid_term_spike_v1",
    features=[mid_term_daily_spike_features],
)

# --- Canonical-grain long-term spike (see feast_long_term_spike.py) ---
_SPIKE_LONG_TERM_PARQUET = (
    _REPO_ROOT.parent / "artifacts" / "feast" / "long_term_spike_canonical.parquet"
).resolve()
_spike_long_term_source_path = str(_SPIKE_LONG_TERM_PARQUET)

long_term_spike_canonical_source = FileSource(
    name="long_term_spike_canonical_parquet",
    path=_spike_long_term_source_path,
    file_format=ParquetFormat(),
    timestamp_field="event_timestamp",
)

long_term_slow_spike_features = FeatureView(
    name="long_term_slow_spike_features",
    entities=[canonical_patron],
    ttl=timedelta(days=220),
    schema=[
        Field(name="patron__theo_win_sum__w180d_m1snap", dtype=Float64),
        Field(name="patron__gaming_days_cnt__w180d_m1snap", dtype=Int64),
        Field(name="patron__adt__w180d_m1snap", dtype=Float64),
    ],
    source=long_term_spike_canonical_source,
    tags={
        "owner": "trainer_hightier",
        "cadence": "monthly_canonical_asof",
        "spike": "feast_long_term_feasibility",
    },
)

walkaway_canonical_long_term_spike_v1 = FeatureService(
    name="walkaway_canonical_long_term_spike_v1",
    features=[long_term_slow_spike_features],
)
