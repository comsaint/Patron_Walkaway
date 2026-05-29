"""Unit tests for Feast long-term (180d) feasibility spike (no ClickHouse)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig, HightierServingConfig
from trainer_hightier.feature_experiment.feast_long_term_spike import (
    SPIKE_LONG_TERM_FEATURE_COLUMNS,
    FeastLongTermSpikeConfig,
    _sanitize_ch_session_export_df,
    _write_feast_spike_parquet,
    compute_long_term_spike_snapshot,
    export_clickhouse_sessions_to_parquet,
    run_spike,
)
from trainer_hightier.tests.test_patron_session_metrics import _sess_row


def _write_min_session(path: Path) -> None:
    from datetime import date, timedelta

    from trainer_hightier.utils.slow_month_turn import resolve_slow_month_turn_context

    anchor = resolve_slow_month_turn_context(date.today()).slow_anchor_required
    gd2 = anchor
    gd1 = anchor - timedelta(days=7)
    sess = pd.DataFrame([_sess_row(100, 1, 100.0, gd1), _sess_row(100, 2, 50.0, gd2)])
    pq.write_table(pa.Table.from_pandas(sess), path)


def _write_mapping(path: Path) -> None:
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame({"player_id": [100], "canonical_id": ["c1"]})),
        path,
    )


def test_sanitize_ch_session_export_drops_bad_player_id() -> None:
    raw = pd.DataFrame(
        {
            "player_id": [1, None, "bad"],
            "gaming_day_event": ["2024-01-05", "2024-01-06", "2024-01-07"],
            "theo_win": [10.0, 20.0, 30.0],
        }
    )
    got = _sanitize_ch_session_export_df(raw)
    assert len(got) == 1
    assert int(got.iloc[0]["player_id"]) == 1


def test_write_feast_spike_parquet_collapses_latest_anchor(tmp_path: Path) -> None:
    full = tmp_path / "full.parquet"
    rows = [
        {
            "canonical_id": "c1",
            "anchor_gaming_day_event": pd.Timestamp("2024-01-31"),
            "patron__theo_win_sum__w180d_m1snap": 100.0,
            "patron__gaming_days_cnt__w180d_m1snap": 1,
            "patron__adt__w180d_m1snap": 100.0,
        },
        {
            "canonical_id": "c1",
            "anchor_gaming_day_event": pd.Timestamp("2024-02-29"),
            "patron__theo_win_sum__w180d_m1snap": 150.0,
            "patron__gaming_days_cnt__w180d_m1snap": 2,
            "patron__adt__w180d_m1snap": 75.0,
        },
    ]
    pd.DataFrame(rows).to_parquet(full, index=False)
    out = tmp_path / "feast.parquet"
    nrows = _write_feast_spike_parquet(full, out)
    assert nrows == 1
    df = pd.read_parquet(out)
    assert float(df.iloc[0]["patron__theo_win_sum__w180d_m1snap"]) == 150.0
    assert "event_timestamp" in df.columns


def test_clickhouse_session_export_chunks(tmp_path: Path) -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def query_df(self, query: str, parameters: dict) -> pd.DataFrame:
            self.queries.append(query)
            return pd.DataFrame(
                {
                    "player_id": [1],
                    "gaming_day_event": [pd.Timestamp("2024-01-05")],
                    "theo_win": [10.0],
                }
            )

    fake_client = _FakeClient()
    cfg = HightierServingConfig(hightier_scorer_player_id_chunk_size=2)
    with (
        patch(
            "trainer_hightier.feature_experiment.feast_long_term_spike.default_hightier_serving_config",
            return_value=cfg,
        ),
        patch(
            "trainer_hightier.feature_experiment.feast_long_term_spike.get_clickhouse_client",
            return_value=fake_client,
        ),
    ):
        meta = export_clickhouse_sessions_to_parquet(
            tmp_path / "sess.parquet",
            gaming_day_start=date(2024, 1, 1),
            gaming_day_end=date(2024, 6, 1),
            player_ids=frozenset({1, 2, 3, 4, 5}),
        )

    assert meta["query_count"] == 3
    assert "player_id IN (1,2)" in fake_client.queries[0]
    assert "SELECT\n                player_id," in fake_client.queries[0]
    assert meta["rows_dropped_on_sanitize"] == 0


def test_compute_long_term_spike_snapshot_local(tmp_path: Path) -> None:
    sess = tmp_path / "sess.parquet"
    _write_min_session(sess)
    cmap = tmp_path / "map.parquet"
    _write_mapping(cmap)
    feast_art = tmp_path / "feast"
    cfg = FeastLongTermSpikeConfig(
        feast_repo=tmp_path / "feast_repo",
        spike_parquet=feast_art / "slow_patron_180d_monthly.parquet",
        staging_dir=feast_art / "staging",
        canonical_mapping_parquet=cmap,
        duckdb_runtime=DuckDbRuntimeConfig(),
        lookback_days=180,
    )
    out, meta = compute_long_term_spike_snapshot(
        session_path=sess,
        cfg=cfg,
        lookback_days=180,
    )
    assert out.is_file()
    assert meta["feast_spike_rows"] >= 1
    df = pd.read_parquet(out)
    for col in SPIKE_LONG_TERM_FEATURE_COLUMNS:
        assert col in df.columns


def test_run_spike_end_to_end_local_mock_feast(tmp_path: Path) -> None:
    sess = tmp_path / "sess.parquet"
    _write_min_session(sess)
    cmap = tmp_path / "map.parquet"
    _write_mapping(cmap)
    allow = tmp_path / "allow.parquet"
    pd.DataFrame({"player_id": [100]}).to_parquet(allow, index=False)
    feast_repo = Path(__file__).resolve().parents[1] / "feast_repo"
    feast_art = tmp_path / "feast"
    report_path = feast_art / "report.json"
    cfg = FeastLongTermSpikeConfig(
        feast_repo=feast_repo,
        spike_parquet=feast_art / "slow_patron_180d_monthly.parquet",
        staging_dir=feast_art / "staging",
        report_path=report_path,
        canonical_mapping_parquet=cmap,
        adt_allowlist_parquet=allow,
        local_cleaned_session=sess,
        session_source="local_cleaned",
        duckdb_runtime=DuckDbRuntimeConfig(),
        lookback_days=180,
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
            "lookup_missing_by_feature": {c: 0 for c in SPIKE_LONG_TERM_FEATURE_COLUMNS},
            "lookup_latency_ms_batch": 3.0,
            "lookup_latency_ms_per_entity": 3.0 / max(1, n),
            "lookup_latency_ms_p50": 3.0,
            "lookup_latency_ms_p95": 3.0,
        }

    bounds = (date(2024, 1, 1), date(2024, 6, 1))
    with (
        patch(
            "trainer_hightier.feature_experiment.feast_long_term_spike._session_gaming_day_bounds",
            return_value=bounds,
        ),
        patch(
            "trainer_hightier.feature_experiment.feast_long_term_spike.run_feast_apply",
            side_effect=_fake_apply,
        ),
        patch(
            "trainer_hightier.feature_experiment.feast_long_term_spike.run_feast_materialize",
            side_effect=_fake_materialize,
        ),
        patch(
            "trainer_hightier.feature_experiment.feast_long_term_spike.run_online_lookup_smoke",
            side_effect=_fake_lookup,
        ),
    ):
        report = run_spike(cfg)

    assert report["verdict"] in ("pass", "marginal", "fail")
    assert report_path.is_file()
    loaded = json.loads(report_path.read_text(encoding="utf-8"))
    assert loaded["session_source"] == "local_cleaned"
    assert loaded["scope"] == "adt_allowlist"
    assert "compute_seconds_total" in loaded
