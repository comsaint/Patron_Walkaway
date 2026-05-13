"""Feast entity / feature views / feature services for trainer_hightier.

前置條件：清洗後的 bet Parquet 必須含 ``prediction_visible_ts_cf``（由
``trainer_hightier`` bet preprocess 寫入）。若尚未重跑 preprocess，``feast plan``
未加 ``--skip-source-validation`` 時可能因缺欄或時間欄檢核而失敗。

離線擷取：見 ``feature_store.yaml`` 的 ``offline_store.type: duckdb``（需
``ibis-framework``；見 repo 根目錄 ``requirements.txt``）。

Explicit ``schema``：避免 Feast 對 ``gaming_day`` 的 ``date32`` 做 schema inference
時觸發不支援的型別對應（與本 repo 的 ``feast==0.63.0`` 行為一致）。

未列入 ``schema`` 的 Parquet 欄位（含 ``gaming_day``）仍存在檔案中，但**不**透過
本 FeatureView 註冊；若訓練需要 ``gaming_day``，請另開 view 或以字串欄在 preprocess
中衍生後再註冊。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from feast import Entity, FeatureService, FeatureView, Field, FileSource
from feast.types import Float64, Int64, String, UnixTimestamp
from feast.value_type import ValueType

_REPO_ROOT = Path(__file__).resolve().parent
_CLEANED_BET_PARQUET = (_REPO_ROOT.parent / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet.parquet").resolve()

# Absolute path so ``feast apply`` works from any CWD.
_bet_source_path = str(_CLEANED_BET_PARQUET)

bet = Entity(
    name="bet",
    join_keys=["bet_id"],
    value_type=ValueType.DOUBLE,
    description="Bet grain after L0 DQ + dedup; join key matches Parquet ``bet_id``.",
)

cleaned_bet_source = FileSource(
    name="cleaned_bet_parquet",
    path=_bet_source_path,
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
        Field(name="wager_nn", dtype=Float64),
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

walkaway_bet_v1 = FeatureService(
    name="walkaway_bet_v1",
    features=[cleaned_bet_features],
)
