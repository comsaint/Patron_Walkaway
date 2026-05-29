"""Unit tests for Feast mid-term feasibility spike (no ClickHouse)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from trainer_hightier.config import DuckDbRuntimeConfig, HightierServingConfig
from trainer_hightier.feature_experiment.feast_mid_term_spike import (
    SPIKE_MID_TERM_FEATURE_COLUMNS,
    FeastMidTermSpikeConfig,
    _add_event_timestamp_column,
    _feast_entity_rows,
    _split_player_id_chunks,
    compute_mid_term_spike_snapshot,
    export_clickhouse_bets_to_parquet,
    run_online_lookup_smoke,
    run_spike,
)


def _write_cleaned_bet(root: Path, rows: list[dict[str, object]]) -> None:
    part = root / "gaming_month=202605" / "gaming_day_key=2026-05-18"
    part.mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(part / "data.parquet", index=False)


def _write_mapping(path: Path) -> None:
    pd.DataFrame({"player_id": [1, 2], "canonical_id": ["c1", "c2"]}).to_parquet(path, index=False)


def test_split_player_id_chunks_stable_sorted() -> None:
    """Allowlist chunks must be bounded and deterministic."""

    assert _split_player_id_chunks(frozenset({5, 1, 3, 2, 4}), 2) == [[1, 2], [3, 4], [5]]


def test_feast_entity_rows_dict_of_lists() -> None:
    """Feast 0.63 rejects pandas Series columns (no ``.val``); use dict-of-lists."""
    rows = _feast_entity_rows(["c1", "c2"])
    assert rows == {"canonical_id": ["c1", "c2"]}
    assert not isinstance(rows["canonical_id"], pd.Series)


def test_spike_mid_term_feature_columns_match_snapshot_materializer() -> None:
    """Full-schema spike exposes all mid-term snapshot base columns."""
    assert len(SPIKE_MID_TERM_FEATURE_COLUMNS) == 18
    assert "fe__bets_cnt__w1d" in SPIKE_MID_TERM_FEATURE_COLUMNS
    assert "fe__min_pcd_w7d" in SPIKE_MID_TERM_FEATURE_COLUMNS


def test_online_lookup_uses_dict_entity_rows(tmp_path: Path) -> None:
    """``get_online_features`` must receive dict entity_rows in one batch call."""

    captured: list[object] = []

    class _FakeResponse:
        def __init__(self, canonical_ids: list[str]) -> None:
            self._canonical_ids = canonical_ids

        def to_df(self) -> pd.DataFrame:
            n = len(self._canonical_ids)
            return pd.DataFrame(
                {
                    "canonical_id": self._canonical_ids,
                    **{c: [1.0] * n for c in SPIKE_MID_TERM_FEATURE_COLUMNS},
                }
            )

    class _FakeStore:
        def __init__(self, *, repo_path: str) -> None:
            self.repo_path = repo_path

        def get_online_features(self, *, features, entity_rows):
            captured.append(entity_rows)
            return _FakeResponse(list(entity_rows["canonical_id"]))

    feast_repo = tmp_path / "feast_repo"
    feast_repo.mkdir()
    with patch("feast.FeatureStore", _FakeStore):
        result = run_online_lookup_smoke(
            feast_repo,
            canonical_ids=["c1", "c2", "c3"],
            batch_size=2,
        )

    assert result["lookup_ok_rows"] == 2
    assert result["lookup_batch_size"] == 2
    assert captured == [{"canonical_id": ["c1", "c2"]}]
    assert result["lookup_latency_ms_batch"] == result["lookup_latency_ms_p50"]


def test_clickhouse_export_chunks_player_filter(tmp_path: Path) -> None:
    """Small-sample export must not build one giant ClickHouse IN-list."""

    class _FakeClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def query_df(self, query: str, parameters: dict) -> pd.DataFrame:
            self.queries.append(query)
            assert parameters["g_start"] == date(2026, 5, 1)
            assert parameters["g_end"] == date(2026, 5, 3)
            return pd.DataFrame(
                {
                    "player_id": [1],
                    "gaming_day_event": [pd.Timestamp("2026-05-01")],
                    "payout_complete_dtm": [pd.Timestamp("2026-05-01T10:00:00Z")],
                    "wager": [100.0],
                    "payout_odds": [2.0],
                }
            )

    fake_client = _FakeClient()
    cfg = HightierServingConfig(hightier_scorer_player_id_chunk_size=2)
    with (
        patch(
            "trainer_hightier.feature_experiment.feast_mid_term_spike.default_hightier_serving_config",
            return_value=cfg,
        ),
        patch(
            "trainer_hightier.feature_experiment.feast_mid_term_spike.get_clickhouse_client",
            return_value=fake_client,
        ),
    ):
        meta = export_clickhouse_bets_to_parquet(
            tmp_path / "out.parquet",
            bets_gaming_day_start=date(2026, 5, 1),
            bets_gaming_day_end=date(2026, 5, 3),
            player_ids=frozenset({1, 2, 3, 4, 5}),
        )

    assert meta["query_count"] == 3
    assert meta["player_id_chunk_count"] == 3
    assert meta["player_id_chunk_size"] == 2
    assert len(fake_client.queries) == 3
    assert "player_id IN (1,2)" in fake_client.queries[0]
    assert "player_id IN (3,4)" in fake_client.queries[1]
    assert "player_id IN (5)" in fake_client.queries[2]
    for q in fake_client.queries:
        assert "CAST(wager AS Nullable(Float64))" in q
        assert "CAST(payout_odds AS Nullable(Float64))" in q


def test_add_event_timestamp_collapses_to_latest_anchor(tmp_path: Path) -> None:
    full = tmp_path / "full.parquet"
    row_old = {"canonical_id": "c1", "anchor_gaming_day_event": pd.Timestamp("2026-05-17")}
    row_new = {"canonical_id": "c1", "anchor_gaming_day_event": pd.Timestamp("2026-05-18")}
    for col in SPIKE_MID_TERM_FEATURE_COLUMNS:
        row_old[col] = 10.0
        row_new[col] = 20.0 if "w7d" in col or col.endswith("w1d") else 10.0
    row_new["fe__wager_sum__w7d"] = 20.0
    pd.DataFrame([row_old, row_new]).to_parquet(full, index=False)
    out = tmp_path / "feast.parquet"
    nrows = _add_event_timestamp_column(full, out)
    assert nrows == 1
    df = pd.read_parquet(out)
    assert list(df.columns) == ["canonical_id", *SPIKE_MID_TERM_FEATURE_COLUMNS, "event_timestamp"]
    assert float(df.iloc[0]["fe__wager_sum__w7d"]) == 20.0


def test_compute_mid_term_spike_snapshot_local(tmp_path: Path) -> None:
    cleaned = tmp_path / "cleaned"
    _write_cleaned_bet(
        cleaned,
        [
            {
                "player_id": 1,
                "gaming_day_event": pd.Timestamp("2026-05-18"),
                "payout_complete_dtm": pd.Timestamp("2026-05-18T10:00:00Z"),
                "wager": 100.0,
                "payout_odds": 2.0,
            },
            {
                "player_id": 1,
                "gaming_day_event": pd.Timestamp("2026-05-17"),
                "payout_complete_dtm": pd.Timestamp("2026-05-17T12:00:00Z"),
                "wager": 50.0,
                "payout_odds": 1.5,
            },
        ],
    )
    cmap = tmp_path / "map.parquet"
    _write_mapping(cmap)
    feast_art = tmp_path / "feast"
    cfg = FeastMidTermSpikeConfig(
        feast_repo=tmp_path / "feast_repo",
        spike_parquet=feast_art / "mid_term_spike_canonical.parquet",
        staging_dir=feast_art / "staging",
        canonical_mapping_parquet=cmap,
        duckdb_runtime=DuckDbRuntimeConfig(),
        anchor_days=1,
    )
    out, meta = compute_mid_term_spike_snapshot(
        cleaned_bet_path=cleaned,
        cfg=cfg,
        canonical_universe_parquet=None,
        anchor_start=pd.Timestamp("2026-05-17").date(),
        anchor_end=pd.Timestamp("2026-05-17").date(),
        bets_gday_start=pd.Timestamp("2026-04-01").date(),
        bets_gday_end=pd.Timestamp("2026-05-17").date(),
    )
    assert out.is_file()
    assert meta["feast_spike_rows"] >= 1
    df = pd.read_parquet(out)
    for col in SPIKE_MID_TERM_FEATURE_COLUMNS:
        assert col in df.columns


def test_run_spike_end_to_end_local_mock_feast(tmp_path: Path) -> None:
    cleaned = tmp_path / "cleaned"
    _write_cleaned_bet(
        cleaned,
        [
            {
                "player_id": 1,
                "gaming_day_event": pd.Timestamp("2026-05-18"),
                "payout_complete_dtm": pd.Timestamp("2026-05-18T10:00:00Z"),
                "wager": 80.0,
                "payout_odds": 2.0,
            },
            {
                "player_id": 1,
                "gaming_day_event": pd.Timestamp("2026-05-17"),
                "payout_complete_dtm": pd.Timestamp("2026-05-17T11:00:00Z"),
                "wager": 40.0,
                "payout_odds": 1.5,
            },
        ],
    )
    cmap = tmp_path / "map.parquet"
    _write_mapping(cmap)
    feast_repo = Path(__file__).resolve().parents[1] / "feast_repo"
    feast_art = tmp_path / "feast"
    report_path = feast_art / "report.json"
    cfg = FeastMidTermSpikeConfig(
        feast_repo=feast_repo,
        spike_parquet=feast_art / "mid_term_spike_canonical.parquet",
        staging_dir=feast_art / "staging",
        report_path=report_path,
        canonical_mapping_parquet=cmap,
        local_cleaned_bet=cleaned,
        bet_source="local_cleaned",
        sample_mode="wider_sample",
        anchor_days=1,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )

    def _fake_apply(repo: Path) -> float:
        return 0.01

    def _fake_materialize(repo: Path, *, start, end) -> float:
        return 0.02

    def _fake_lookup(repo: Path, *, canonical_ids: list[str], batch_size: int) -> dict:
        n = min(len(canonical_ids), batch_size)
        return {
            "lookup_batch_size": n,
            "lookup_ok_rows": n,
            "lookup_missing_by_feature": {c: 0 for c in SPIKE_MID_TERM_FEATURE_COLUMNS},
            "lookup_latency_ms_batch": 2.0,
            "lookup_latency_ms_per_entity": 2.0 / max(1, n),
            "lookup_latency_ms_p50": 2.0,
            "lookup_latency_ms_p95": 2.0,
        }

    bounds = (
        date(2026, 5, 17),
        date(2026, 5, 17),
        date(2026, 4, 1),
        date(2026, 5, 17),
    )
    with (
        patch(
            "trainer_hightier.feature_experiment.feast_mid_term_spike._anchor_bounds",
            return_value=bounds,
        ),
        patch(
            "trainer_hightier.feature_experiment.feast_mid_term_spike.run_feast_apply",
            side_effect=_fake_apply,
        ),
        patch(
            "trainer_hightier.feature_experiment.feast_mid_term_spike.run_feast_materialize",
            side_effect=_fake_materialize,
        ),
        patch(
            "trainer_hightier.feature_experiment.feast_mid_term_spike.run_online_lookup_smoke",
            side_effect=_fake_lookup,
        ),
    ):
        report = run_spike(cfg)

    assert report["verdict"] in ("pass", "marginal", "fail")
    assert report_path.is_file()
    loaded = json.loads(report_path.read_text(encoding="utf-8"))
    assert loaded["bet_source"] == "local_cleaned"
    assert "compute_seconds_total" in loaded
