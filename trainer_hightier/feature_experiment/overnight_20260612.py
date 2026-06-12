"""Overnight 2026-06-12 autonomous experiment driver.

Subcommands:
- ``prep``: build matrix splits = hist-peer hot splits + hall dummies (+ optional txn_lite).
- ``matrix``: train arms x seeds (skip_optuna, alert-band objective, cooldown 60) resumable.

All outputs live under ``trainer_hightier/artifacts/feature_experiment/overnight_20260612/``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import time
from pathlib import Path
from typing import Any, Final

import duckdb
import pandas as pd

from trainer_hightier.config import (
    HighTierObjectiveConfig,
    Step5TrainConfig,
    configs_from_run_profile,
    get_run_profile,
    txn_lite_feature_columns,
)
from trainer_hightier.feature_experiment.feature_registry import MODEL_FEATURE_COLUMNS
from trainer_hightier.feature_experiment.hall_probe_smoke import (
    _safe_dummy_name,
    load_canonical_table_hall_map,
    select_top_hall_dummy_columns,
)
from trainer_hightier.feature_experiment.materialize_txn_lite import (
    default_cleaned_casino_txn_root,
    materialize_txn_lite_parquet,
)

_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
OUT_ROOT: Final[Path] = _REPO_ROOT / "trainer_hightier/artifacts/feature_experiment/overnight_20260612"
SRC_SPLITS: Final[Path] = (
    _REPO_ROOT
    / "trainer_hightier/artifacts/feature_experiment/hot_patron_hist_peer_smoke/splits_hot_hist_peer"
)
MATRIX_SPLITS: Final[Path] = OUT_ROOT / "splits_matrix"
_MAPPING_XLS: Final[Path] = _REPO_ROOT / "data/gmwds_data_GM_hall_table_mapping.xls"
_SPLIT_NAMES: Final[tuple[str, ...]] = ("train", "val", "test")

HOT_COMBO_COLS: Final[tuple[str, ...]] = (
    "fe__hot__peer_wager_z__adt_decile",
    "fe__hot__avg_wager_today_over_peer_p95__adt_decile",
    "fe__hot__mid_term_history_sparse_flag",
)
TXN_COLS: Final[tuple[str, ...]] = tuple(txn_lite_feature_columns())

_METRIC_EXTRA_KEYS: Final[tuple[str, ...]] = (
    "val_ap",
    "test_ap",
    "test_operational_simulated_precision",
    "test_operational_simulated_alerts_per_hour",
    "test_operational_simulated_true_positives",
    "test_operational_simulated_false_positives",
    "step5_threshold",
    "step5_val_pick_feasible",
    "step5_alert_band_scalar_score",
)


def _hall_dim_frame(hall_map: dict[int, str], top_halls: tuple[str, ...]) -> pd.DataFrame:
    """Build a table_id-keyed dim frame with int8 hall dummy columns."""

    if not hall_map:
        raise ValueError("hall_map is empty; cannot build hall dim frame")
    top_set = frozenset(top_halls)
    rows: list[dict[str, int]] = []
    for tid, hall in hall_map.items():
        row: dict[str, int] = {"table_id": int(tid)}
        for h in top_halls:
            row[_safe_dummy_name(h)] = int(hall == h)
        row["hall__is_other"] = int(hall not in top_set)
        rows.append(row)
    dim = pd.DataFrame(rows)
    for col in dim.columns:
        if col != "table_id":
            dim[col] = dim[col].astype("int8")
    return dim


def _matrix_copy_sql(
    *,
    src_parquet: Path,
    hall_dummy_cols: tuple[str, ...],
    txn_parquet: Path | None,
) -> str:
    """COPY SQL joining hall dummies (and optional txn_lite) onto one split."""

    src = str(src_parquet.resolve()).replace("\\", "/")
    hall_selects = [
        f'COALESCE(h."{c}", 0)::TINYINT AS "{c}"'
        for c in hall_dummy_cols
        if c not in ("hall__is_unknown",)
    ]
    hall_selects.append(
        "(CASE WHEN h.table_id IS NULL THEN 1 ELSE 0 END)::TINYINT AS hall__is_unknown",
    )
    txn_join = ""
    txn_selects: list[str] = []
    if txn_parquet is not None:
        txn = str(txn_parquet.resolve()).replace("\\", "/")
        txn_join = f"LEFT JOIN read_parquet('{txn}') t ON TRY_CAST(b.bet_id AS DOUBLE) = t.bet_id"
        txn_selects = [f'COALESCE(t."{c}", 0) AS "{c}"' for c in TXN_COLS]
    select_cols = ",\n  ".join(["b.*", *hall_selects, *txn_selects])
    return (
        f"SELECT\n  {select_cols}\n"
        f"FROM read_parquet('{src}') b\n"
        f"LEFT JOIN hall_dim h ON TRY_CAST(b.table_id AS BIGINT) = h.table_id\n"
        f"{txn_join}"
    )


def run_prep(*, with_txn: bool) -> dict[str, Any]:
    """Materialize matrix splits with hall dummies and optional txn_lite columns."""

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    MATRIX_SPLITS.mkdir(parents=True, exist_ok=True)
    hall_map, map_audit = load_canonical_table_hall_map(_MAPPING_XLS)
    hall_cols, hall_meta = select_top_hall_dummy_columns(
        hall_map,
        train_parquet=SRC_SPLITS / "train.parquet",
    )
    duck, _, _ = configs_from_run_profile(get_run_profile("default"))

    txn_meta: dict[str, Any] = {}
    txn_paths: dict[str, Path] = {}
    if with_txn:
        for split in _SPLIT_NAMES:
            out_p = OUT_ROOT / f"txn_lite_{split}.parquet"
            if not out_p.is_file():
                t0 = time.perf_counter()
                meta = materialize_txn_lite_parquet(
                    cleaned_casino_txn_root=default_cleaned_casino_txn_root(),
                    training_parquet_for_bet_ids=SRC_SPLITS / f"{split}.parquet",
                    out_parquet=out_p,
                    duckdb_runtime=duck,
                )
                meta["elapsed_sec"] = round(time.perf_counter() - t0, 1)
                txn_meta[split] = meta
                print(f"[prep] txn_lite {split} done in {meta['elapsed_sec']}s", flush=True)
            txn_paths[split] = out_p

    dim = _hall_dim_frame(hall_map, tuple(hall_meta["top_halls"]))
    coverage: dict[str, Any] = {}
    con = duckdb.connect(database=":memory:")
    try:
        con.register("hall_dim", dim)
        for split in _SPLIT_NAMES:
            out_p = MATRIX_SPLITS / f"{split}.parquet"
            if out_p.is_file():
                print(f"[prep] {split} already materialized; skip", flush=True)
                continue
            t0 = time.perf_counter()
            sql = _matrix_copy_sql(
                src_parquet=SRC_SPLITS / f"{split}.parquet",
                hall_dummy_cols=hall_cols,
                txn_parquet=txn_paths.get(split),
            )
            dst = str(out_p.resolve()).replace("\\", "/")
            con.execute(f"COPY ({sql}) TO '{dst}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
            stats = con.execute(
                f"SELECT COUNT(*), AVG(hall__is_unknown) FROM read_parquet('{dst}')",
            ).fetchone()
            coverage[split] = {
                "rows": int(stats[0]),
                "hall_unknown_rate": float(stats[1]),
                "elapsed_sec": round(time.perf_counter() - t0, 1),
            }
            print(f"[prep] matrix split {split}: {coverage[split]}", flush=True)
    finally:
        con.close()

    manifest = {
        "src_splits": str(SRC_SPLITS),
        "mapping_audit": map_audit,
        "hall_feature_meta": hall_meta,
        "with_txn": bool(with_txn),
        "txn_materialization": txn_meta,
        "txn_feature_columns": list(TXN_COLS) if with_txn else [],
        "matrix_coverage": coverage,
    }
    (OUT_ROOT / "prep_manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str),
        encoding="utf-8",
    )
    return manifest


def _arm_feature_columns(arm: str, hall_cols: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve feature columns for one named arm."""

    base = tuple(MODEL_FEATURE_COLUMNS)
    groups: dict[str, tuple[str, ...]] = {
        "baseline": base,
        "add_hall": base + hall_cols,
        "add_hot": base + HOT_COMBO_COLS,
        "add_hall_hot": base + hall_cols + HOT_COMBO_COLS,
        "add_txn": base + TXN_COLS,
        "add_txn_hot": base + TXN_COLS + HOT_COMBO_COLS,
        "add_all": base + hall_cols + HOT_COMBO_COLS + TXN_COLS,
    }
    if arm not in groups:
        raise ValueError(f"unknown arm {arm!r}; expected one of {sorted(groups)}")
    return tuple(dict.fromkeys(groups[arm]))


def _metric_slice(report: dict[str, Any]) -> dict[str, Any]:
    """Compact metric subset for the matrix report."""

    out = {k: v for k, v in report.items() if "op_precision_at" in k or "op_alerts" in k}
    for k in _METRIC_EXTRA_KEYS:
        out[k] = report.get(k)
    return out


def run_matrix(*, seeds: tuple[int, ...], arms: tuple[str, ...]) -> dict[str, Any]:
    """Train arms x seeds on the matrix splits; resumable via training_metrics.json."""

    manifest = json.loads((OUT_ROOT / "prep_manifest.json").read_text(encoding="utf-8"))
    hall_cols = tuple(manifest["hall_feature_meta"]["hall_dummy_columns"])
    duck, _, _ = configs_from_run_profile(get_run_profile("default"))
    min_prec = HighTierObjectiveConfig().min_precision
    report_path = OUT_ROOT / "matrix_report.json"
    results: dict[str, Any] = (
        json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    )
    for arm in arms:
        cols = _arm_feature_columns(arm, hall_cols)
        for seed in seeds:
            key = f"{arm}_seed{seed}"
            out_dir = OUT_ROOT / key
            metrics_p = out_dir / "training_metrics.json"
            if key in results and metrics_p.is_file():
                print(f"[matrix] {key} already done; skip", flush=True)
                continue
            t0 = time.perf_counter()
            print(f"[matrix] start {key} n_features={len(cols)}", flush=True)
            res = _b5.train_lgbm_from_splits(
                splits_dir=MATRIX_SPLITS,
                duckdb_runtime=duck,
                objective_min_precision=min_prec,
                random_seed=int(seed),
                step5=Step5TrainConfig(run_step5=True, skip_optuna=True),
                output_dir=out_dir,
                feature_columns=cols,
            )
            row = _metric_slice(dict(res.report))
            row["elapsed_sec"] = round(time.perf_counter() - t0, 1)
            row["n_features"] = len(cols)
            results[key] = row
            report_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
            print(f"[matrix] done {key} in {row['elapsed_sec']}s", flush=True)
    return results


_WALKAWAY_GAP_MIN: Final[int] = 30
_EP_BUDGET_RATES: Final[tuple[float, ...]] = (1.0, 2.0)
_EP_COOLDOWN_GRID: Final[tuple[int, ...]] = (60, 90, 120, 180, 240)


def _episode_spans(split_parquet: Path) -> pd.DataFrame:
    """Per (player, episode) bet-activity spans split at >30min bet gaps."""

    src = str(Path(split_parquet).resolve()).replace("\\", "/")
    con = duckdb.connect(database=":memory:")
    try:
        return con.execute(
            f"""
            WITH b AS (
              SELECT TRY_CAST(player_id AS BIGINT) AS pid,
                     CAST(payout_complete_dtm AS TIMESTAMP) AS ts
              FROM read_parquet('{src}')
              WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
                AND payout_complete_dtm IS NOT NULL
            ),
            m AS (
              SELECT pid, ts,
                CASE WHEN LAG(ts) OVER w IS NULL
                       OR ts - LAG(ts) OVER w > INTERVAL {_WALKAWAY_GAP_MIN} MINUTE
                     THEN 1 ELSE 0 END AS brk
              FROM b WINDOW w AS (PARTITION BY pid ORDER BY ts)
            ),
            e AS (
              SELECT pid, ts,
                SUM(brk) OVER (PARTITION BY pid ORDER BY ts ROWS UNBOUNDED PRECEDING) AS ep
              FROM m
            )
            SELECT pid, ep, MIN(ts) AS start_ts, MAX(ts) AS end_ts, COUNT(*) AS n_bets
            FROM e GROUP BY pid, ep ORDER BY pid, start_ts
            """,
        ).fetchdf()
    finally:
        con.close()


def _candidates_with_episode(candidates: pd.DataFrame, spans: pd.DataFrame) -> pd.DataFrame:
    """Attach episode ids to player-game candidates via backward asof on episode start."""

    cand = candidates.copy()
    cand["pid"] = pd.to_numeric(cand["player_id"], errors="coerce").astype("int64")
    cand["_ts"] = pd.to_datetime(cand["alert_ts"], errors="coerce").dt.tz_localize(None)
    cand = cand.dropna(subset=["_ts"]).sort_values("_ts")
    sp = spans.sort_values("start_ts").copy()
    sp["start_ts"] = pd.to_datetime(sp["start_ts"]).dt.tz_localize(None)
    merged = pd.merge_asof(
        cand,
        sp[["pid", "ep", "start_ts"]],
        left_on="_ts",
        right_on="start_ts",
        by="pid",
        direction="backward",
    )
    n_miss = int(merged["ep"].isna().sum())
    if n_miss > 0:
        raise ValueError(f"episode mapping failed for {n_miss} candidates")
    return merged


def _simulate_episode_cap(cand_ep: pd.DataFrame, threshold: float) -> dict[str, Any]:
    """At most one raised alert per (player, episode); first above-threshold wins."""

    above = cand_ep.loc[
        pd.to_numeric(cand_ep["player_game_score"], errors="coerce") >= float(threshold)
    ].sort_values(["pid", "_ts"], kind="mergesort")
    raised = above.drop_duplicates(subset=["pid", "ep"], keep="first")
    y = pd.to_numeric(raised["player_game_label"], errors="coerce").fillna(0).astype(int)
    tp = int((y == 1).sum())
    fp = int((y == 0).sum())
    return {
        "alerts": int(len(raised)),
        "candidate_alerts": int(len(above)),
        "true_positives": tp,
        "false_positives": fp,
        "precision": tp / max(tp + fp, 1),
        "events_detected": int(
            raised.loc[y.to_numpy() == 1, ["pid", "ep"]].drop_duplicates().shape[0],
        ),
    }


def _bisect_episode_threshold(cand_ep: pd.DataFrame, target_alerts: int) -> float:
    """Binary-search threshold so episode-capped raised alerts hit ``target_alerts``."""

    scores = pd.to_numeric(cand_ep["player_game_score"], errors="coerce")
    lo = float(scores.min() - 1e-9)
    hi = float(scores.max() + 1e-9)
    best_thr, best_err = hi, float("inf")
    for _ in range(48):
        mid = (lo + hi) / 2.0
        alerts = _simulate_episode_cap(cand_ep, mid)["alerts"]
        err = abs(alerts - int(target_alerts))
        if err < best_err:
            best_thr, best_err = mid, err
        if alerts > int(target_alerts):
            lo = mid
        else:
            hi = mid
    return best_thr


def _score_candidates(model_dir: Path, split_parquet: Path, split_name: str) -> pd.DataFrame:
    """Score one split with a saved bundle and return player-game candidates."""

    import pickle

    from trainer_hightier.serving.feature_builder import prepare_lgbm_feature_matrix

    bundle = pickle.loads((Path(model_dir) / "model.pkl").read_bytes())
    df = pd.read_parquet(split_parquet)
    x = prepare_lgbm_feature_matrix(
        df,
        feature_columns=tuple(bundle["feature_columns"]),
        categorical_columns=tuple(bundle.get("categorical_columns", ())),
        category_categories=dict(bundle.get("category_categories", {})),
    )
    scores = bundle["model"].predict_proba(x)[:, 1]
    pg = _b5.aggregate_bets_to_player_game(df, scores, split_name=split_name)
    return pg.candidates.copy()


def _window_hours(split_parquet: Path) -> float:
    """Observation window hours from payout timestamps of one split."""

    con = duckdb.connect(database=":memory:")
    src = str(Path(split_parquet).resolve()).replace("\\", "/")
    try:
        row = con.execute(
            f"SELECT MIN(CAST(payout_complete_dtm AS TIMESTAMP)),"
            f" MAX(CAST(payout_complete_dtm AS TIMESTAMP)) FROM read_parquet('{src}')",
        ).fetchone()
    finally:
        con.close()
    return float((row[1] - row[0]).total_seconds() / 3600.0)


def _walkaway_gap_distribution(cand_ep: pd.DataFrame) -> dict[str, Any]:
    """Distribution of time between consecutive walkaway events per player (minutes)."""

    pos = cand_ep.loc[
        pd.to_numeric(cand_ep["player_game_label"], errors="coerce") == 1
    ]
    ev = (
        pos.groupby(["pid", "ep"], as_index=False)["_ts"]
        .max()
        .sort_values(["pid", "_ts"], kind="mergesort")
    )
    gaps = ev.groupby("pid")["_ts"].diff().dropna().dt.total_seconds() / 60.0
    if gaps.empty:
        return {"n_gaps": 0}
    qs = gaps.quantile([0.1, 0.25, 0.5, 0.75, 0.9]).to_dict()
    out: dict[str, Any] = {
        "n_events": int(len(ev)),
        "n_gaps": int(len(gaps)),
        "gap_minutes_quantiles": {f"p{int(k * 100)}": round(float(v), 1) for k, v in qs.items()},
    }
    for m in (60, 90, 120, 180, 240, 480):
        out[f"share_gap_le_{m}m"] = round(float((gaps <= m).mean()), 4)
    return out


def _episode_policies_for_seed(
    *,
    model_dir: Path,
    val_parquet: Path,
    test_parquet: Path,
) -> dict[str, Any]:
    """Cooldown grid + episode-cap policy comparison for one trained baseline."""

    from trainer_hightier.evaluation.alert_band_objective import (
        target_alert_count,
        threshold_for_target_operational_alerts,
    )
    from trainer_hightier.evaluation.player_alert_policy import (
        operational_simulated_metrics_block,
    )

    cand_val = _score_candidates(model_dir, val_parquet, "val")
    cand_test = _score_candidates(model_dir, test_parquet, "test")
    wh_val = _window_hours(val_parquet)
    wh_test = _window_hours(test_parquet)
    val_ep = _candidates_with_episode(cand_val, _episode_spans(val_parquet))
    test_ep = _candidates_with_episode(cand_test, _episode_spans(test_parquet))

    out: dict[str, Any] = {
        "window_hours": {"val": wh_val, "test": wh_test},
        "test_walkaway_gap_distribution": _walkaway_gap_distribution(test_ep),
        "policies": {},
    }
    for rate in _EP_BUDGET_RATES:
        budget = target_alert_count(wh_val, rate)
        for cd in _EP_COOLDOWN_GRID:
            pick = threshold_for_target_operational_alerts(
                cand_val,
                budget,
                window_hours=wh_val,
                requested_alerts_per_hour=rate,
                cooldown_min=cd,
            )
            blk = operational_simulated_metrics_block(
                "test",
                cand_test,
                pick.threshold,
                cooldown_min=cd,
                window_hours=wh_test,
            )
            tp = int(blk["test_operational_simulated_true_positives"])
            fp = int(blk["test_operational_simulated_false_positives"])
            out["policies"][f"cooldown_{cd}m_at_{rate:g}hr"] = {
                "val_threshold": pick.threshold,
                "val_precision": pick.precision,
                "test_alerts": int(blk["test_operational_simulated_alerts"]),
                "test_alerts_per_hour": blk["test_operational_simulated_alerts_per_hour"],
                "test_tp": tp,
                "test_fp": fp,
                "test_precision": tp / max(tp + fp, 1),
            }
        thr_ep = _bisect_episode_threshold(val_ep, budget)
        sim_val = _simulate_episode_cap(val_ep, thr_ep)
        sim_test = _simulate_episode_cap(test_ep, thr_ep)
        out["policies"][f"episode_cap_at_{rate:g}hr"] = {
            "val_threshold": thr_ep,
            "val_precision": sim_val["precision"],
            "test_alerts": sim_test["alerts"],
            "test_alerts_per_hour": sim_test["alerts"] / wh_test,
            "test_tp": sim_test["true_positives"],
            "test_fp": sim_test["false_positives"],
            "test_precision": sim_test["precision"],
            "test_events_detected": sim_test["events_detected"],
        }
    return out


def run_episode_study(*, model_dirs: tuple[Path, ...]) -> dict[str, Any]:
    """Episode-level alert policy study across baseline seeds."""

    val_p = MATRIX_SPLITS / "val.parquet"
    test_p = MATRIX_SPLITS / "test.parquet"
    results: dict[str, Any] = {
        "design": {
            "walkaway_gap_min": _WALKAWAY_GAP_MIN,
            "protocol": (
                "per-policy val threshold re-pick at fixed alerts/hr budget, then test eval; "
                "episode_cap = at most 1 raised alert per (player, >30min-gap betting episode)"
            ),
            "cooldown_grid_min": list(_EP_COOLDOWN_GRID),
            "budget_rates": list(_EP_BUDGET_RATES),
        },
        "seeds": {},
    }
    for md in model_dirs:
        seed_key = Path(md).name
        t0 = time.perf_counter()
        results["seeds"][seed_key] = _episode_policies_for_seed(
            model_dir=Path(md),
            val_parquet=val_p,
            test_parquet=test_p,
        )
        print(f"[episode] {seed_key} done in {time.perf_counter() - t0:.0f}s", flush=True)
        (OUT_ROOT / "episode_policy_study.json").write_text(
            json.dumps(results, indent=2, default=str),
            encoding="utf-8",
        )
    return results


def run_dq_audit(*, partitions: tuple[str, ...]) -> dict[str, Any]:
    """Quantify t_bet CDC dedup risks: orphan updates and full-tie duplicate groups."""

    root = _REPO_ROOT / "data/t_bet"
    out: dict[str, Any] = {}
    con = duckdb.connect(database=":memory:")
    con.execute("PRAGMA threads=8")
    try:
        for part in partitions:
            glob = str((root / f"partition_{part}").resolve()).replace("\\", "/") + "/*.parquet"
            row = con.execute(
                f"""
                WITH b AS (
                  SELECT TRY_CAST(bet_id AS DOUBLE) AS bid,
                         UPPER(COALESCE(CAST(__op AS VARCHAR), '')) AS op,
                         CAST(__etl_insert_Dtm AS VARCHAR) AS etl,
                         CAST(payout_complete_dtm AS VARCHAR) AS pcd,
                         CAST(__ts_ms AS VARCHAR) AS tsms
                  FROM read_parquet('{glob}')
                ),
                per_bet AS (
                  SELECT bid,
                    COUNT(*) AS n,
                    SUM(CASE WHEN op = 'C' THEN 1 ELSE 0 END) AS n_c,
                    SUM(CASE WHEN op = 'U' THEN 1 ELSE 0 END) AS n_u,
                    COUNT(DISTINCT (etl, pcd, tsms)) AS n_distinct_keys
                  FROM b WHERE bid IS NOT NULL GROUP BY bid
                )
                SELECT
                  (SELECT COUNT(*) FROM b) AS rows_total,
                  (SELECT SUM(CASE WHEN op = 'U' THEN 1 ELSE 0 END) FROM b) AS rows_u,
                  COUNT(*) AS bets,
                  SUM(CASE WHEN n_u > 0 AND n_c = 0 THEN 1 ELSE 0 END) AS orphan_u_bets,
                  SUM(CASE WHEN n_c > 1 THEN 1 ELSE 0 END) AS multi_c_bets,
                  SUM(CASE WHEN n > 1 AND n_distinct_keys = 1 THEN 1 ELSE 0 END) AS full_tie_bets
                FROM per_bet
                """,
            ).fetchone()
            out[part] = {
                "rows_total": int(row[0]),
                "rows_op_u": int(row[1] or 0),
                "distinct_bets": int(row[2]),
                "orphan_update_bets": int(row[3] or 0),
                "multi_create_bets": int(row[4] or 0),
                "full_tie_nondeterministic_bets": int(row[5] or 0),
            }
            print(f"[dq] {part}: {out[part]}", flush=True)
    finally:
        con.close()
    (OUT_ROOT / "bet_cdc_dq_audit.json").write_text(
        json.dumps(out, indent=2),
        encoding="utf-8",
    )
    return out


def run_pg_grain_diag() -> dict[str, Any]:
    """Player-game grain facts: bets per game, side-bet mix, in-game time spread."""

    out: dict[str, Any] = {}
    con = duckdb.connect(database=":memory:")
    con.execute("PRAGMA threads=8")
    try:
        for split in ("val", "test"):
            src = str((MATRIX_SPLITS / f"{split}.parquet").resolve()).replace("\\", "/")
            row = con.execute(
                f"""
                WITH g AS (
                  SELECT TRY_CAST(player_id AS BIGINT) AS pid, game_id,
                    COUNT(*) AS n_bets,
                    COUNT(DISTINCT type_of_bet) AS n_bet_types,
                    SUM(CASE WHEN is_back_bet THEN 1 ELSE 0 END) AS n_back,
                    EXTRACT(EPOCH FROM (
                      MAX(CAST(payout_complete_dtm AS TIMESTAMP))
                      - MIN(CAST(payout_complete_dtm AS TIMESTAMP)))) AS span_sec
                  FROM read_parquet('{src}')
                  GROUP BY pid, game_id
                )
                SELECT COUNT(*),
                  AVG(CASE WHEN n_bets > 1 THEN 1 ELSE 0 END),
                  AVG(n_bets),
                  QUANTILE_CONT(n_bets, 0.9),
                  QUANTILE_CONT(n_bets, 0.99),
                  AVG(CASE WHEN n_bet_types > 1 THEN 1 ELSE 0 END),
                  AVG(CASE WHEN n_back > 0 AND n_back < n_bets THEN 1 ELSE 0 END),
                  AVG(CASE WHEN span_sec > 0 THEN 1 ELSE 0 END),
                  QUANTILE_CONT(span_sec, 0.5) FILTER (WHERE n_bets > 1),
                  QUANTILE_CONT(span_sec, 0.9) FILTER (WHERE n_bets > 1)
                FROM g
                """,
            ).fetchone()
            type_mix = con.execute(
                f"""
                SELECT type_of_bet, COUNT(*) AS n
                FROM read_parquet('{src}') GROUP BY 1 ORDER BY n DESC LIMIT 12
                """,
            ).fetchall()
            out[split] = {
                "player_games": int(row[0]),
                "share_multi_bet_games": round(float(row[1]), 4),
                "avg_bets_per_game": round(float(row[2]), 3),
                "bets_per_game_p90": float(row[3]),
                "bets_per_game_p99": float(row[4]),
                "share_games_multi_bet_types": round(float(row[5]), 4),
                "share_games_mixed_back_nonback": round(float(row[6]), 4),
                "share_games_nonzero_payout_span": round(float(row[7]), 4),
                "payout_span_sec_p50_multibet": row[8],
                "payout_span_sec_p90_multibet": row[9],
                "type_of_bet_mix": {str(t): int(n) for t, n in type_mix},
            }
            print(f"[pgdiag] {split}: {out[split]}", flush=True)
    finally:
        con.close()
    (OUT_ROOT / "player_game_grain_diag.json").write_text(
        json.dumps(out, indent=2),
        encoding="utf-8",
    )
    return out


def run_controlled_optuna(*, arm: str, seed: int, timeout_sec: float) -> dict[str, Any]:
    """Time-boxed Optuna on one arm with the guarded default search space."""

    manifest = json.loads((OUT_ROOT / "prep_manifest.json").read_text(encoding="utf-8"))
    hall_cols = tuple(manifest["hall_feature_meta"]["hall_dummy_columns"])
    cols = _arm_feature_columns(arm, hall_cols)
    duck, _, _ = configs_from_run_profile(get_run_profile("default"))
    out_dir = OUT_ROOT / f"optuna_{arm}_seed{seed}"
    t0 = time.perf_counter()
    res = _b5.train_lgbm_from_splits(
        splits_dir=MATRIX_SPLITS,
        duckdb_runtime=duck,
        objective_min_precision=HighTierObjectiveConfig().min_precision,
        random_seed=int(seed),
        step5=Step5TrainConfig(
            run_step5=True,
            skip_optuna=False,
            optuna_timeout_sec=float(timeout_sec),
        ),
        output_dir=out_dir,
        feature_columns=cols,
    )
    row = _metric_slice(dict(res.report))
    row["elapsed_sec"] = round(time.perf_counter() - t0, 1)
    baseline_key = f"{arm}_seed{seed}"
    matrix = json.loads((OUT_ROOT / "matrix_report.json").read_text(encoding="utf-8"))
    out = {
        "arm": arm,
        "seed": int(seed),
        "optuna_timeout_sec": float(timeout_sec),
        "optuna_result": row,
        "skip_optuna_reference": matrix.get(baseline_key),
    }
    (OUT_ROOT / "optuna_controlled_report.json").write_text(
        json.dumps(out, indent=2, default=str),
        encoding="utf-8",
    )
    return out


def main() -> None:
    """CLI entry for the overnight driver."""

    parser = argparse.ArgumentParser(description="Overnight 2026-06-12 experiment driver")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_prep = sub.add_parser("prep", help="materialize matrix splits")
    p_prep.add_argument("--with-txn", action="store_true")
    p_matrix = sub.add_parser("matrix", help="train arms x seeds")
    p_matrix.add_argument("--seeds", type=int, nargs="+", default=[42, 43, 44])
    p_matrix.add_argument(
        "--arms",
        nargs="+",
        default=["baseline", "add_hall", "add_hot", "add_hall_hot", "add_txn", "add_all"],
    )
    p_ep = sub.add_parser("episode", help="episode-level alert policy study")
    p_ep.add_argument("--model-dirs", type=Path, nargs="+", required=True)
    p_dq = sub.add_parser("dq", help="t_bet CDC dedup risk audit")
    p_dq.add_argument(
        "--partitions",
        nargs="+",
        default=["202512", "202601", "202602", "202603", "202604", "202605", "202606"],
    )
    sub.add_parser("pgdiag", help="player-game grain diagnostics")
    p_opt = sub.add_parser("optuna", help="controlled Optuna on one arm")
    p_opt.add_argument("--arm", default="add_txn")
    p_opt.add_argument("--seed", type=int, default=42)
    p_opt.add_argument("--timeout-sec", type=float, default=2700.0)
    args = parser.parse_args()
    if args.cmd == "prep":
        out = run_prep(with_txn=bool(args.with_txn))
    elif args.cmd == "matrix":
        out = run_matrix(seeds=tuple(args.seeds), arms=tuple(args.arms))
    elif args.cmd == "episode":
        out = run_episode_study(model_dirs=tuple(args.model_dirs))
    elif args.cmd == "dq":
        out = run_dq_audit(partitions=tuple(args.partitions))
    elif args.cmd == "optuna":
        out = run_controlled_optuna(
            arm=str(args.arm),
            seed=int(args.seed),
            timeout_sec=float(args.timeout_sec),
        )
    else:
        out = run_pg_grain_diag()
    print(json.dumps({"ok": True, "keys": sorted(out)}, default=str))


if __name__ == "__main__":
    main()
