"""ADT allowlist resolution, filtering, and feature_state manifest meta (high_tier serving)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from zoneinfo import ZoneInfo

from trainer_hightier.config import HightierServingConfig
from trainer_hightier.serving.adt_allowlist import (
    check_training_allowlist_sha256,
    filter_bets_by_adt_allowlist,
    resolve_adt_allowlist_path,
    sha256_file,
)
from trainer_hightier.serving.feature_state_store import (
    ActiveSnapshotManifest,
    init_feature_state_db,
    upsert_adt_allowlist_meta,
)


def test_filter_bets_by_adt_allowlist() -> None:
    bets = pd.DataFrame({"player_id": [1, 2, 3], "wager": [10.0, 20.0, 30.0]})
    out = filter_bets_by_adt_allowlist(bets, frozenset({2}))
    assert list(out["player_id"]) == [2]


def test_resolve_adt_allowlist_path_cli_over_manifest(tmp_path: Path) -> None:
    cli_p = tmp_path / "cli.parquet"
    pd.DataFrame({"player_id": [1]}).to_parquet(cli_p, index=False)
    man_p = tmp_path / "man.parquet"
    pd.DataFrame({"player_id": [2]}).to_parquet(man_p, index=False)
    cfg = HightierServingConfig(adt_allowed_players_parquet=tmp_path / "cfg.parquet")
    m = ActiveSnapshotManifest(
        version="v",
        slow_patron_parquet=tmp_path / "s.parquet",
        fe_derived_parquet=None,
        trial_bet_behavior_parquet=None,
        adt_allowlist_parquet=man_p,
        adt_allowlist_version=None,
        coverage_end_exclusive=None,
        training_cutoff_iso=None,
        mid_term_snapshot_parquet=None,
        fe_short_term_parquet=None,
        raw={},
    )
    got = resolve_adt_allowlist_path(cfg, manifest=m, cli_path=cli_p)
    assert got.resolve() == cli_p.resolve()


def test_resolve_adt_allowlist_path_manifest_before_cfg(tmp_path: Path) -> None:
    man_p = tmp_path / "man.parquet"
    pd.DataFrame({"player_id": [2]}).to_parquet(man_p, index=False)
    cfg_p = tmp_path / "cfg.parquet"
    pd.DataFrame({"player_id": [1]}).to_parquet(cfg_p, index=False)
    cfg = HightierServingConfig(adt_allowed_players_parquet=cfg_p)
    m = ActiveSnapshotManifest(
        version="v",
        slow_patron_parquet=tmp_path / "s.parquet",
        fe_derived_parquet=None,
        trial_bet_behavior_parquet=None,
        adt_allowlist_parquet=man_p,
        adt_allowlist_version="abc",
        coverage_end_exclusive=None,
        training_cutoff_iso=None,
        mid_term_snapshot_parquet=None,
        fe_short_term_parquet=None,
        raw={},
    )
    got = resolve_adt_allowlist_path(cfg, manifest=m, cli_path=None)
    assert got.resolve() == man_p.resolve()


def test_check_training_allowlist_sha256_fail_fast() -> None:
    a64 = "a" * 64
    b64 = "b" * 64
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        check_training_allowlist_sha256({"adt_allowlist_sha256": a64}, b64, fail_fast=True)


def test_check_training_allowlist_sha256_degraded_continue() -> None:
    a64 = "a" * 64
    b64 = "b" * 64
    ok = check_training_allowlist_sha256({"adt_allowlist_sha256": a64}, b64, fail_fast=False)
    assert ok is False


def test_active_manifest_from_dict_adt_fields(tmp_path: Path) -> None:
    al = tmp_path / "a.parquet"
    pd.DataFrame({"player_id": [1]}).to_parquet(al, index=False)
    slow = tmp_path / "slow.parquet"
    pd.DataFrame({"player_id": [1]}).to_parquet(slow, index=False)
    d = {
        "version": "rid",
        "slow_patron_parquet": str(slow),
        "adt_allowlist_parquet": str(al),
        "adt_allowlist_version": "ver1",
    }
    m = ActiveSnapshotManifest.from_dict(d)
    assert m.adt_allowlist_version == "ver1"
    assert m.adt_allowlist_parquet is not None and m.adt_allowlist_parquet.is_file()


def test_upsert_adt_allowlist_meta_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "fs.db"
    init_feature_state_db(db)
    ap = tmp_path / "x.parquet"
    pd.DataFrame({"player_id": [9]}).to_parquet(ap, index=False)
    h = sha256_file(ap)
    upsert_adt_allowlist_meta(artifact_path=ap, version=h, sha256_hex=h, row_count=1, path=db)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT path, version, sha256_hex, row_count FROM adt_allowlist_meta WHERE id=1",
        ).fetchone()
    assert row is not None
    assert row[2] == h


def test_score_once_allowlist_all_skipped_advances_watermark(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When every bet row is filtered out by allowlist, ETL cursor must still advance."""
    hk = ZoneInfo("Asia/Hong_Kong")
    etl = pd.Timestamp("2025-06-01 10:00:00", tz=hk)
    bets = pd.DataFrame(
        {
            "bet_id": ["b1"],
            "is_back_bet": [0],
            "bet_type": ["x"],
            "type_of_bet": ["y"],
            "__etl_insert_Dtm": [etl],
            "payout_complete_dtm": [etl],
            "gaming_day_event": ["2025-06-01"],
            "session_id": ["s1"],
            "player_id": [999],
            "table_id": [1],
            "position_idx": [1],
            "wager": [1.0],
            "casino_win": [0.0],
            "payout_odds": [1.0],
            "status": [1],
        },
    )

    from trainer_hightier.serving import scorer as scorer_mod
    from trainer_hightier.serving.state_db import get_last_processed_etl_insert, init_state_db, set_last_processed_etl_insert

    db = tmp_path / "st.db"
    init_state_db(db)
    import sqlite3

    with sqlite3.connect(db) as conn:
        set_last_processed_etl_insert(conn, (etl - pd.Timedelta(hours=1)).to_pydatetime())
        conn.commit()

    mini_probe = bets[["bet_id", "__etl_insert_Dtm", "payout_complete_dtm"]].copy()
    monkeypatch.setattr(scorer_mod, "fetch_bets_incremental_etl_probe", lambda *a, **k: mini_probe)
    monkeypatch.setattr(scorer_mod, "fetch_bets_incremental", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(scorer_mod, "fetch_bet_pool_window", lambda *a, **k: pd.DataFrame())

    bundle = MagicMock(
        feature_columns=("player_id",),
        categorical_columns=(),
        category_categories={},
        threshold=0.5,
        model_version="mv",
        model=MagicMock(predict_proba=lambda x: np.zeros((len(x), 2))),
    )
    from trainer_hightier.serving.feast_online_adapter import MockFeastOnlineAdapter

    conn = sqlite3.connect(db)
    try:
        before = get_last_processed_etl_insert(conn)
        n = scorer_mod.score_once(
            conn,
            bundle,
            feast_adapter=MockFeastOnlineAdapter(features_by_canonical={}),
            high_adt_only=True,
            allowlist_ids=frozenset({1}),
        )
        after = get_last_processed_etl_insert(conn)
    finally:
        conn.close()
    assert n == 0
    assert before is not None and after is not None
    assert pd.Timestamp(after).as_unit("ns") == etl.as_unit("ns")
