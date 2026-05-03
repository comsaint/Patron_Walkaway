"""Unit tests for preprocess_bet_ingestion_fix_registry_v1."""

from pathlib import Path

import pytest

from layered_data_assets.preprocess_bet_ingestion_fix_registry_v1 import (
    load_preprocess_bet_ingestion_fix_registry,
    resolve_bet_ingest_fix004_cap_binding,
)
from layered_data_assets.preprocess_bet_v1 import run_preprocess_bet_v1


def test_resolve_bet_ingest_fix004_from_repo_registry() -> None:
    repo = Path(__file__).resolve().parents[2]
    path = repo / "schema" / "preprocess_bet_ingestion_fix_registry.yaml"
    doc = load_preprocess_bet_ingestion_fix_registry(path)
    cap, fix_id, fix_ver, applied = resolve_bet_ingest_fix004_cap_binding(doc)
    assert cap == 122
    assert fix_id == "BET-INGEST-FIX-004"
    assert fix_ver == "v1"
    assert applied == ["BET-INGEST-FIX-004:v1"]


def test_resolve_raises_on_contract_rule_cap_mismatch() -> None:
    doc = {
        "bulk_historical_ingest_episodes": {
            "synthetic_observed_at_contract": {"ingest_delay_cap_sec": 122},
        },
        "active_rules": [
            {
                "fix_rule_id": "BET-INGEST-FIX-004",
                "fix_rule_version": "v1",
                "enabled": True,
                "action": {
                    "type": "normalize_observed_at",
                    "params": {"cap_delay_sec": 99},
                },
            }
        ],
    }
    with pytest.raises(ValueError, match="mismatch"):
        resolve_bet_ingest_fix004_cap_binding(doc)


def test_resolve_raises_when_fix004_disabled() -> None:
    doc = {
        "bulk_historical_ingest_episodes": {
            "synthetic_observed_at_contract": {"ingest_delay_cap_sec": 122},
        },
        "active_rules": [
            {
                "fix_rule_id": "BET-INGEST-FIX-004",
                "fix_rule_version": "v1",
                "enabled": False,
                "action": {
                    "type": "normalize_observed_at",
                    "params": {"cap_delay_sec": 122},
                },
            }
        ],
    }
    with pytest.raises(ValueError, match="enabled"):
        resolve_bet_ingest_fix004_cap_binding(doc)


def test_registry_version_expected_mismatch_raises(tmp_path: Path) -> None:
    p = tmp_path / "reg.yaml"
    p.write_text(
        "registry_version: wrong\n"
        "bulk_historical_ingest_episodes:\n"
        "  synthetic_observed_at_contract:\n"
        "    ingest_delay_cap_sec: 122\n"
        "active_rules:\n"
        "  - fix_rule_id: BET-INGEST-FIX-004\n"
        "    fix_rule_version: v1\n"
        "    enabled: true\n"
        "    action:\n"
        "      type: normalize_observed_at\n"
        "      params:\n"
        "        cap_delay_sec: 122\n",
        encoding="utf-8",
    )
    try:
        import duckdb
    except ImportError:
        pytest.skip("duckdb not installed")

    inp = tmp_path / "in.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (1::BIGINT, 100::BIGINT, DATE '2026-01-15',
                 TIMESTAMP '2026-01-15 10:00:00', TIMESTAMP '2026-01-15 10:05:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER)
              ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm,
                     is_deleted, is_canceled, is_manual)
            ) TO '{inp.as_posix()}' (FORMAT PARQUET)
            """
        )
        with pytest.raises(ValueError, match="registry_version mismatch"):
            run_preprocess_bet_v1(
                con=con,
                input_paths=[inp],
                output_parquet=tmp_path / "o.parquet",
                gaming_day="2026-01-15",
                dummy_player_ids_parquet=None,
                eligible_player_ids_parquet=None,
                ingestion_fix_registry_path=p,
                ingestion_fix_registry_version_expected="v0.4_draft",
            )
    finally:
        con.close()
