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
    _txn_valid_cte,
    default_cleaned_casino_txn_root,
    materialize_txn_lite_parquet,
    resolve_cleaned_casino_txn_read_sql,
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


_CEILING_RATES: Final[tuple[float, ...]] = (0.1, 0.25, 0.5, 1.0, 2.0)


def run_ceiling(*, model_dirs: tuple[Path, ...], cooldown_min: int) -> dict[str, Any]:
    """Precision-vs-alert-budget curve: how strict must the budget be to reach 75%?"""

    from trainer_hightier.evaluation.alert_band_objective import (
        target_alert_count,
        threshold_for_target_operational_alerts,
    )
    from trainer_hightier.evaluation.player_alert_policy import (
        operational_simulated_metrics_block,
    )

    val_p = MATRIX_SPLITS / "val.parquet"
    test_p = MATRIX_SPLITS / "test.parquet"
    wh_val = _window_hours(val_p)
    wh_test = _window_hours(test_p)
    results: dict[str, Any] = {
        "design": {
            "protocol": (
                "per-budget val threshold pick under cooldown sim, then test eval; "
                "rates in alerts/hour"
            ),
            "cooldown_min": int(cooldown_min),
            "rates": list(_CEILING_RATES),
            "window_hours": {"val": wh_val, "test": wh_test},
        },
        "models": {},
    }
    for md in model_dirs:
        key = Path(md).name
        t0 = time.perf_counter()
        cand_val = _score_candidates(Path(md), val_p, "val")
        cand_test = _score_candidates(Path(md), test_p, "test")
        rows: dict[str, Any] = {}
        for rate in _CEILING_RATES:
            budget = target_alert_count(wh_val, rate)
            pick = threshold_for_target_operational_alerts(
                cand_val,
                budget,
                window_hours=wh_val,
                requested_alerts_per_hour=rate,
                cooldown_min=int(cooldown_min),
            )
            blk = operational_simulated_metrics_block(
                "test",
                cand_test,
                pick.threshold,
                cooldown_min=int(cooldown_min),
                window_hours=wh_test,
            )
            tp = int(blk["test_operational_simulated_true_positives"])
            fp = int(blk["test_operational_simulated_false_positives"])
            rows[f"{rate:g}_per_hr"] = {
                "val_budget_alerts": int(budget),
                "val_threshold": pick.threshold,
                "val_precision": pick.precision,
                "test_alerts": int(blk["test_operational_simulated_alerts"]),
                "test_alerts_per_hour": blk["test_operational_simulated_alerts_per_hour"],
                "test_tp": tp,
                "test_fp": fp,
                "test_precision": tp / max(tp + fp, 1),
            }
        results["models"][key] = rows
        print(f"[ceiling] {key} done in {time.perf_counter() - t0:.0f}s", flush=True)
        (OUT_ROOT / "ceiling_report.json").write_text(
            json.dumps(results, indent=2, default=str),
            encoding="utf-8",
        )
    return results


BANKROLL_COLS: Final[tuple[str, ...]] = (
    "fe__bankroll__cum_loss__today",
    "fe__bankroll__cum_loss_over_wager__today",
    "fe__bankroll__cum_loss_over_theo__today",
)
BANKROLL_SPLITS: Final[Path] = OUT_ROOT / "splits_bankroll"


def _bankroll_copy_sql(src_parquet: Path) -> str:
    """Augment one matrix split with PIT (exclusive, gaming-day) bankroll columns.

    Convention matches existing ``fe__canonical__*__today``: cumulative over prior
    bets of the same ``canonical_id`` within ``gaming_day_event``, ordered by
    ``(payout_complete_dtm, bet_id)``, EXCLUDING the current row.
    """

    src = str(Path(src_parquet).resolve()).replace("\\", "/")
    win = (
        "PARTITION BY canonical_id, gaming_day_event "
        "ORDER BY payout_complete_dtm, bet_id "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING"
    )
    return f"""
    WITH c AS (
      SELECT *,
        SUM(casino_win) OVER w AS _cum_loss,
        SUM(wager) OVER w AS _cum_wager,
        SUM(theo_win) OVER w AS _cum_theo
      FROM read_parquet('{src}')
      WINDOW w AS ({win})
    )
    SELECT * EXCLUDE (_cum_loss, _cum_wager, _cum_theo),
      COALESCE(_cum_loss, 0.0) AS fe__bankroll__cum_loss__today,
      CASE WHEN _cum_wager > 0 THEN _cum_loss / _cum_wager END
        AS fe__bankroll__cum_loss_over_wager__today,
      CASE WHEN _cum_theo > 0 THEN _cum_loss / _cum_theo END
        AS fe__bankroll__cum_loss_over_theo__today
    FROM c
    """.strip()


def run_bankroll(*, seeds: tuple[int, ...]) -> dict[str, Any]:
    """Materialize bankroll-augmented splits, then train add_txn vs add_txn+bankroll."""

    duck, _, _ = configs_from_run_profile(get_run_profile("default"))
    BANKROLL_SPLITS.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    con.execute("PRAGMA threads=8")
    try:
        for split in _SPLIT_NAMES:
            out_p = BANKROLL_SPLITS / f"{split}.parquet"
            if out_p.is_file():
                print(f"[bankroll] {split} already materialized; skip", flush=True)
                continue
            sql = _bankroll_copy_sql(MATRIX_SPLITS / f"{split}.parquet")
            dst = str(out_p.resolve()).replace("\\", "/")
            con.execute(f"COPY ({sql}) TO '{dst}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
            print(f"[bankroll] materialized {split}", flush=True)
    finally:
        con.close()

    base_txn = tuple(dict.fromkeys(tuple(MODEL_FEATURE_COLUMNS) + TXN_COLS))
    arms = {
        "add_txn": base_txn,
        "add_txn_bankroll": tuple(dict.fromkeys(base_txn + BANKROLL_COLS)),
    }
    min_prec = HighTierObjectiveConfig().min_precision
    report_path = OUT_ROOT / "bankroll_report.json"
    results: dict[str, Any] = (
        json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    )
    for arm, cols in arms.items():
        for seed in seeds:
            key = f"{arm}_seed{seed}"
            if key in results:
                print(f"[bankroll] {key} already done; skip", flush=True)
                continue
            t0 = time.perf_counter()
            print(f"[bankroll] start {key} n_features={len(cols)}", flush=True)
            res = _b5.train_lgbm_from_splits(
                splits_dir=BANKROLL_SPLITS,
                duckdb_runtime=duck,
                objective_min_precision=min_prec,
                random_seed=int(seed),
                step5=Step5TrainConfig(run_step5=True, skip_optuna=True),
                output_dir=OUT_ROOT / f"bankroll_{key}",
                feature_columns=cols,
            )
            row = _metric_slice(dict(res.report))
            row["elapsed_sec"] = round(time.perf_counter() - t0, 1)
            row["n_features"] = len(cols)
            results[key] = row
            report_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
            print(f"[bankroll] done {key} in {row['elapsed_sec']}s", flush=True)
    return results


CASHTIMING_COLS: Final[tuple[str, ...]] = (
    "txn__min_since_last_cashout",
    "txn__net_cash_flow__w30m",
)
CASHTIMING_SPLITS: Final[Path] = OUT_ROOT / "splits_cashtiming"
_CASHTIMING_LOOKBACK_H: Final[int] = 6


def _cashtiming_feature_sql(*, split_parquet: Path, cleaned_read: str) -> str:
    """PIT bet-grain cash-out timing features (event_ts < pcd, available_ts <= pcd)."""

    src = str(Path(split_parquet).resolve()).replace("\\", "/")
    return f"""
    WITH {_txn_valid_cte(cleaned_read)},
    train_rows AS (
      SELECT TRY_CAST(bet_id AS DOUBLE) AS bet_id,
             TRY_CAST(player_id AS BIGINT) AS player_id,
             CAST(payout_complete_dtm AS TIMESTAMPTZ) AS pcd
      FROM read_parquet('{src}')
      WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
        AND TRY_CAST(player_id AS BIGINT) IS NOT NULL
        AND payout_complete_dtm IS NOT NULL
    ),
    joined AS (
      SELECT tr.bet_id, tr.pcd, txn.type, txn.sub_type, txn.txn_value, txn.event_ts
      FROM train_rows AS tr
      LEFT JOIN txn_valid AS txn
        ON tr.player_id = txn.player_id
       AND txn.event_ts < tr.pcd
       AND txn.available_ts <= tr.pcd
       AND txn.event_ts >= tr.pcd - INTERVAL {_CASHTIMING_LOOKBACK_H} HOUR
    )
    SELECT bet_id,
      MIN(CASE WHEN type = 'CASHOUT'
          THEN date_diff('second', event_ts, pcd) END) / 60.0
        AS txn__min_since_last_cashout,
      CAST(
        SUM(CASE WHEN type = 'CASHOUT' AND event_ts >= pcd - INTERVAL 30 MINUTE
                 THEN txn_value ELSE 0 END)
        - SUM(CASE WHEN type = 'BUYIN' AND sub_type = 'CASH'
                   AND event_ts >= pcd - INTERVAL 30 MINUTE THEN txn_value ELSE 0 END)
      AS DOUBLE) AS txn__net_cash_flow__w30m
    FROM joined
    GROUP BY bet_id
    """.strip()


def run_cashtiming(*, seeds: tuple[int, ...]) -> dict[str, Any]:
    """Materialize cash-out timing onto momentum splits; train add_txn_momentum +/- cashtiming."""

    duck, _, _ = configs_from_run_profile(get_run_profile("default"))
    cleaned_read, _, _ = resolve_cleaned_casino_txn_read_sql(default_cleaned_casino_txn_root())
    CASHTIMING_SPLITS.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    con.execute("PRAGMA threads=8")
    try:
        for split in _SPLIT_NAMES:
            out_p = CASHTIMING_SPLITS / f"{split}.parquet"
            if out_p.is_file():
                print(f"[cashtiming] {split} already materialized; skip", flush=True)
                continue
            feat_sql = _cashtiming_feature_sql(
                split_parquet=MOMENTUM_SPLITS / f"{split}.parquet",
                cleaned_read=cleaned_read,
            )
            mom = str((MOMENTUM_SPLITS / f"{split}.parquet").resolve()).replace("\\", "/")
            dst = str(out_p.resolve()).replace("\\", "/")
            sel = ", ".join(f'COALESCE(c."{x}", NULL) AS "{x}"' for x in CASHTIMING_COLS)
            con.execute(
                f"""
                COPY (
                  SELECT m.*, {sel}
                  FROM read_parquet('{mom}') AS m
                  LEFT JOIN ({feat_sql}) AS c ON TRY_CAST(m.bet_id AS DOUBLE) = c.bet_id
                ) TO '{dst}' (FORMAT PARQUET, COMPRESSION SNAPPY)
                """,
            )
            print(f"[cashtiming] materialized {split}", flush=True)
    finally:
        con.close()

    base = tuple(dict.fromkeys(tuple(MODEL_FEATURE_COLUMNS) + TXN_COLS + MOMENTUM_COLS))
    arms = {
        "add_txn_momentum": base,
        "add_txn_momentum_cashtiming": tuple(dict.fromkeys(base + CASHTIMING_COLS)),
    }
    min_prec = HighTierObjectiveConfig().min_precision
    report_path = OUT_ROOT / "cashtiming_report.json"
    results: dict[str, Any] = (
        json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    )
    for arm, cols in arms.items():
        for seed in seeds:
            key = f"{arm}_seed{seed}"
            if key in results:
                print(f"[cashtiming] {key} already done; skip", flush=True)
                continue
            t0 = time.perf_counter()
            print(f"[cashtiming] start {key} n_features={len(cols)}", flush=True)
            res = _b5.train_lgbm_from_splits(
                splits_dir=CASHTIMING_SPLITS,
                duckdb_runtime=duck,
                objective_min_precision=min_prec,
                random_seed=int(seed),
                step5=Step5TrainConfig(run_step5=True, skip_optuna=True),
                output_dir=OUT_ROOT / f"cashtiming_{key}",
                feature_columns=cols,
            )
            row = _metric_slice(dict(res.report))
            row["elapsed_sec"] = round(time.perf_counter() - t0, 1)
            row["n_features"] = len(cols)
            results[key] = row
            report_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
            print(f"[cashtiming] done {key} in {row['elapsed_sec']}s", flush=True)
    return results


MOMENTUM_COLS: Final[tuple[str, ...]] = (
    "fe__lossmom__casino_win_sum__w15m",
    "fe__lossmom__casino_win_sum__w1h",
    "fe__lossmom__loss_over_theo__w1h",
    "fe__lossmom__loss_frac__w1h",
    "fe__decel__bets_w15m_share_w1h",
)
MOMENTUM_SPLITS: Final[Path] = OUT_ROOT / "splits_momentum"


def _momentum_copy_sql(src_parquet: Path) -> str:
    """Augment a matrix split with recent loss-momentum + deceleration columns.

    All windows are PIT and exclude the current bet (inclusive RANGE over
    ``canonical_id`` real-time, then current-row value subtracted out).
    """

    src = str(Path(src_parquet).resolve()).replace("\\", "/")

    def _rng(minutes: int) -> str:
        return (
            "PARTITION BY canonical_id ORDER BY _ts "
            f"RANGE BETWEEN INTERVAL {minutes} MINUTE PRECEDING AND CURRENT ROW"
        )

    return f"""
    WITH base AS (
      SELECT *, CAST(payout_complete_dtm AS TIMESTAMP) AS _ts
      FROM read_parquet('{src}')
    ),
    w AS (
      SELECT *,
        SUM(casino_win) OVER ({_rng(15)}) AS _cw15,
        SUM(casino_win) OVER ({_rng(60)}) AS _cw60,
        SUM(theo_win) OVER ({_rng(60)}) AS _th60,
        COUNT(*) OVER ({_rng(15)}) AS _n15,
        COUNT(*) OVER ({_rng(60)}) AS _n60,
        SUM(CASE WHEN casino_win > 0 THEN 1 ELSE 0 END) OVER ({_rng(60)}) AS _ln60
      FROM base
    )
    SELECT * EXCLUDE (_ts, _cw15, _cw60, _th60, _n15, _n60, _ln60),
      (_cw15 - casino_win) AS fe__lossmom__casino_win_sum__w15m,
      (_cw60 - casino_win) AS fe__lossmom__casino_win_sum__w1h,
      CASE WHEN (_th60 - theo_win) > 0
           THEN (_cw60 - casino_win) / (_th60 - theo_win) END
        AS fe__lossmom__loss_over_theo__w1h,
      CASE WHEN (_n60 - 1) > 0
           THEN (_ln60 - CASE WHEN casino_win > 0 THEN 1 ELSE 0 END) * 1.0 / (_n60 - 1) END
        AS fe__lossmom__loss_frac__w1h,
      CASE WHEN (_n60 - 1) > 0 THEN (_n15 - 1) * 1.0 / (_n60 - 1) END
        AS fe__decel__bets_w15m_share_w1h
    FROM w
    """.strip()


def run_momentum(*, seeds: tuple[int, ...]) -> dict[str, Any]:
    """Materialize momentum-augmented splits, then train add_txn vs add_txn+momentum."""

    duck, _, _ = configs_from_run_profile(get_run_profile("default"))
    MOMENTUM_SPLITS.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    con.execute("PRAGMA threads=8")
    try:
        for split in _SPLIT_NAMES:
            out_p = MOMENTUM_SPLITS / f"{split}.parquet"
            if out_p.is_file():
                print(f"[momentum] {split} already materialized; skip", flush=True)
                continue
            sql = _momentum_copy_sql(MATRIX_SPLITS / f"{split}.parquet")
            dst = str(out_p.resolve()).replace("\\", "/")
            con.execute(f"COPY ({sql}) TO '{dst}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
            print(f"[momentum] materialized {split}", flush=True)
    finally:
        con.close()

    base_txn = tuple(dict.fromkeys(tuple(MODEL_FEATURE_COLUMNS) + TXN_COLS))
    arms = {
        "add_txn": base_txn,
        "add_txn_momentum": tuple(dict.fromkeys(base_txn + MOMENTUM_COLS)),
    }
    min_prec = HighTierObjectiveConfig().min_precision
    report_path = OUT_ROOT / "momentum_report.json"
    results: dict[str, Any] = (
        json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    )
    for arm, cols in arms.items():
        for seed in seeds:
            key = f"{arm}_seed{seed}"
            if key in results:
                print(f"[momentum] {key} already done; skip", flush=True)
                continue
            t0 = time.perf_counter()
            print(f"[momentum] start {key} n_features={len(cols)}", flush=True)
            res = _b5.train_lgbm_from_splits(
                splits_dir=MOMENTUM_SPLITS,
                duckdb_runtime=duck,
                objective_min_precision=min_prec,
                random_seed=int(seed),
                step5=Step5TrainConfig(run_step5=True, skip_optuna=True),
                output_dir=OUT_ROOT / f"momentum_{key}",
                feature_columns=cols,
            )
            row = _metric_slice(dict(res.report))
            row["elapsed_sec"] = round(time.perf_counter() - t0, 1)
            row["n_features"] = len(cols)
            results[key] = row
            report_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
            print(f"[momentum] done {key} in {row['elapsed_sec']}s", flush=True)
    return results


PROD3_COLS: Final[tuple[str, ...]] = (
    "fe__outcome__casino_win_sum__w15m",
    "fe__outcome__casino_win_sum__w1h",
    "fe__outcome__casino_win_to_theo_ratio__w1h",
)
PRODMOM_SPLITS: Final[Path] = OUT_ROOT / "splits_prodmom"


def _prodmom_copy_sql(src_parquet: Path) -> str:
    """Augment a matrix split with the 3 PROMOTED fe__outcome__ features using the
    EXACT production materialize_fe_derived semantics: ``PARTITION BY canonical_id``,
    ``RANGE ... PRECEDING AND CURRENT ROW`` minus the scored bet (peer-inclusive semantics).
    """

    src = str(Path(src_parquet).resolve()).replace("\\", "/")

    def _rng(minutes: int) -> str:
        return (
            "PARTITION BY canonical_id ORDER BY _ts "
            f"RANGE BETWEEN INTERVAL {minutes} MINUTE PRECEDING AND CURRENT ROW"
        )

    return f"""
    WITH base AS (
      SELECT *, CAST(payout_complete_dtm AS TIMESTAMP) AS _ts
      FROM read_parquet('{src}')
    ),
    w AS (
      SELECT *,
        SUM(casino_win) OVER ({_rng(15)}) AS _cw15,
        SUM(casino_win) OVER ({_rng(60)}) AS _cw60,
        SUM(theo_win) OVER ({_rng(60)}) AS _th60
      FROM base
    )
    SELECT * EXCLUDE (_ts, _cw15, _cw60, _th60),
      CAST(_cw15 - casino_win AS DOUBLE) AS fe__outcome__casino_win_sum__w15m,
      CAST(_cw60 - casino_win AS DOUBLE) AS fe__outcome__casino_win_sum__w1h,
      CASE WHEN (_th60 - theo_win) > 1e-9
           THEN CAST((_cw60 - casino_win) / (_th60 - theo_win) AS DOUBLE) END
        AS fe__outcome__casino_win_to_theo_ratio__w1h
    FROM w
    """.strip()


def run_prodmom(*, seeds: tuple[int, ...]) -> dict[str, Any]:
    """Validate the PROMOTED strict-PIT fe__outcome__ trio (same-pcd peers excluded) vs
    the leakier experiment momentum feature that produced +3.4pp.

    Arm A = base + txn (promotion-free baseline). Arm B = arm A + the 3 strict features.
    Reports test operational P@1hr to measure the true production gain.
    """

    duck, _, _ = configs_from_run_profile(get_run_profile("default"))
    PRODMOM_SPLITS.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    con.execute("PRAGMA threads=8")
    try:
        for split in _SPLIT_NAMES:
            dst = PRODMOM_SPLITS / f"{split}.parquet"
            if dst.is_file():
                print(f"[prodmom] {split} already materialized; skip", flush=True)
                continue
            sql = _prodmom_copy_sql(MATRIX_SPLITS / f"{split}.parquet")
            dst_s = str(dst.resolve()).replace("\\", "/")
            con.execute(f"COPY ({sql}) TO '{dst_s}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
            print(f"[prodmom] materialized {split}", flush=True)
    finally:
        con.close()

    base32 = tuple(c for c in MODEL_FEATURE_COLUMNS if c not in PROD3_COLS)
    base_txn = tuple(dict.fromkeys(base32 + TXN_COLS))
    arms = {
        "base_txn": base_txn,
        "base_txn_prodmom3": tuple(dict.fromkeys(base_txn + PROD3_COLS)),
    }
    min_prec = HighTierObjectiveConfig().min_precision
    report_path = OUT_ROOT / "prodmom_report.json"
    results: dict[str, Any] = (
        json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    )
    for arm, cols in arms.items():
        for seed in seeds:
            key = f"{arm}_seed{seed}"
            if key in results:
                print(f"[prodmom] {key} already done; skip", flush=True)
                continue
            t0 = time.perf_counter()
            print(f"[prodmom] start {key} n_features={len(cols)}", flush=True)
            res = _b5.train_lgbm_from_splits(
                splits_dir=PRODMOM_SPLITS,
                duckdb_runtime=duck,
                objective_min_precision=min_prec,
                random_seed=int(seed),
                step5=Step5TrainConfig(run_step5=True, skip_optuna=True),
                output_dir=OUT_ROOT / f"prodmom_{key}",
                feature_columns=cols,
            )
            row = _metric_slice(dict(res.report))
            row["elapsed_sec"] = round(time.perf_counter() - t0, 1)
            row["n_features"] = len(cols)
            results[key] = row
            report_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
            print(f"[prodmom] done {key} in {row['elapsed_sec']}s", flush=True)
    return results


_FP_CREDIT_HORIZONS_MIN: Final[tuple[int, ...]] = (15, 30, 45, 60, 90, 120, 180)


def _player_walkaway_moments(split_parquet: Path) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Per-player walkaway moments (start of >30min gap or terminal) from one split."""

    src = str(Path(split_parquet).resolve()).replace("\\", "/")
    con = duckdb.connect(database=":memory:")
    con.execute("PRAGMA threads=8")
    try:
        df = con.execute(
            f"""
            WITH b AS (
              SELECT TRY_CAST(player_id AS BIGINT) AS pid,
                     CAST(payout_complete_dtm AS TIMESTAMP) AS ts
              FROM read_parquet('{src}')
              WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
                AND payout_complete_dtm IS NOT NULL
            ),
            m AS (
              SELECT pid, ts, LEAD(ts) OVER (PARTITION BY pid ORDER BY ts) AS next_ts
              FROM b
            )
            SELECT pid, ts AS walk_ts,
                   (next_ts IS NULL) AS is_terminal
            FROM m
            WHERE next_ts IS NULL
               OR next_ts - ts > INTERVAL {_WALKAWAY_GAP_MIN} MINUTE
            ORDER BY pid, walk_ts
            """,
        ).fetchdf()
        wmax = con.execute(
            f"SELECT MAX(CAST(payout_complete_dtm AS TIMESTAMP)) FROM read_parquet('{src}')",
        ).fetchone()[0]
    finally:
        con.close()
    df["walk_ts"] = pd.to_datetime(df["walk_ts"])
    return df, pd.Timestamp(wmax)


def _time_to_next_walkaway(
    candidates: pd.DataFrame,
    moments: pd.DataFrame,
) -> pd.DataFrame:
    """Attach minutes from each candidate alert_ts to the next walkaway moment at/after it."""

    cand = candidates.copy()
    cand["pid"] = pd.to_numeric(cand["player_id"], errors="coerce").astype("int64")
    cand["_ts"] = pd.to_datetime(cand["alert_ts"], errors="coerce").dt.tz_localize(None)
    cand = cand.dropna(subset=["_ts"]).sort_values("_ts", kind="mergesort")
    mm = moments.dropna(subset=["walk_ts"]).sort_values("walk_ts", kind="mergesort").copy()
    merged = pd.merge_asof(
        cand,
        mm[["pid", "walk_ts", "is_terminal"]],
        left_on="_ts",
        right_on="walk_ts",
        by="pid",
        direction="forward",
        allow_exact_matches=True,
    )
    merged["minutes_to_walkaway"] = (
        (merged["walk_ts"] - merged["_ts"]).dt.total_seconds() / 60.0
    )
    return merged


def run_fp_error_profile(*, model_dirs: tuple[Path, ...], rate: float) -> dict[str, Any]:
    """Profile high-score false positives: how soon do alerted players actually walk away?"""

    from trainer_hightier.evaluation.alert_band_objective import (
        target_alert_count,
        threshold_for_target_operational_alerts,
    )

    val_p = MATRIX_SPLITS / "val.parquet"
    test_p = MATRIX_SPLITS / "test.parquet"
    wh_val = _window_hours(val_p)
    moments, test_wmax = _player_walkaway_moments(test_p)
    results: dict[str, Any] = {
        "design": {
            "protocol": (
                "raised = test player-game with score >= val-picked threshold at target rate; "
                "minutes_to_walkaway = time from alert_ts to next per-player >30min-gap moment; "
                "credit@X = alert counted correct if player walks away within X min of alert_ts"
            ),
            "budget_rate_per_hour": float(rate),
            "credit_horizons_min": list(_FP_CREDIT_HORIZONS_MIN),
            "label_horizon_min": 15,
            "walkaway_gap_min": _WALKAWAY_GAP_MIN,
            "caveat": "walkaway moments rebuilt at player_id grain (labels use canonical_id)",
            "test_window_end": str(test_wmax),
        },
        "models": {},
    }
    for md in model_dirs:
        key = Path(md).name
        t0 = time.perf_counter()
        cand_val = _score_candidates(Path(md), val_p, "val")
        cand_test = _score_candidates(Path(md), test_p, "test")
        budget = target_alert_count(wh_val, rate)
        pick = threshold_for_target_operational_alerts(
            cand_val,
            budget,
            window_hours=wh_val,
            requested_alerts_per_hour=rate,
            cooldown_min=120,
        )
        raised = cand_test.loc[
            pd.to_numeric(cand_test["player_game_score"], errors="coerce") >= pick.threshold
        ].copy()
        prof = _time_to_next_walkaway(raised, moments)
        lbl = pd.to_numeric(prof["player_game_label"], errors="coerce").fillna(0).astype(int)
        mins = prof["minutes_to_walkaway"]
        n = int(len(prof))
        n_label_pos = int((lbl == 1).sum())
        fp = prof.loc[lbl == 0].copy()
        fp_mins = fp["minutes_to_walkaway"]
        credit_curve = {}
        for x in _FP_CREDIT_HORIZONS_MIN:
            credited = int(((mins <= float(x)) & mins.notna()).sum())
            credit_curve[f"credit_le_{x}m"] = {
                "credited_alerts": credited,
                "precision": credited / max(n, 1),
            }
        fp_buckets = {
            "le_15m_anomaly": int((fp_mins <= 15).sum()),
            "15_30m": int(((fp_mins > 15) & (fp_mins <= 30)).sum()),
            "30_60m": int(((fp_mins > 30) & (fp_mins <= 60)).sum()),
            "60_90m": int(((fp_mins > 60) & (fp_mins <= 90)).sum()),
            "90_120m": int(((fp_mins > 90) & (fp_mins <= 120)).sum()),
            "120_180m": int(((fp_mins > 120) & (fp_mins <= 180)).sum()),
            "180_360m": int(((fp_mins > 180) & (fp_mins <= 360)).sum()),
            "gt_360m": int((fp_mins > 360).sum()),
            "no_walkaway_in_window": int(fp_mins.isna().sum()),
        }
        fp_with_future_walk = fp.loc[fp_mins.notna()]
        fp_term = int(
            fp_with_future_walk["is_terminal"].fillna(False).astype(bool).sum(),
        )
        results["models"][key] = {
            "threshold": pick.threshold,
            "raised_alerts": n,
            "label_positive": n_label_pos,
            "label_negative": int((lbl == 0).sum()),
            "precision_label_15m": n_label_pos / max(n, 1),
            "fp_minutes_to_walkaway_quantiles": {
                f"p{int(q * 100)}": (
                    round(float(fp_mins.quantile(q)), 1) if fp_mins.notna().any() else None
                )
                for q in (0.1, 0.25, 0.5, 0.75, 0.9)
            },
            "fp_buckets": fp_buckets,
            "fp_eventually_walks_away_share": round(
                float(int(fp_mins.notna().sum()) / max(int((lbl == 0).sum()), 1)), 4
            ),
            "fp_terminal_moment_share_of_future_walk": round(
                float(fp_term / max(len(fp_with_future_walk), 1)), 4
            ),
            "credit_horizon_precision": credit_curve,
        }
        print(f"[fperr] {key} done in {time.perf_counter() - t0:.0f}s", flush=True)
        (OUT_ROOT / "fp_error_profile.json").write_text(
            json.dumps(results, indent=2, default=str),
            encoding="utf-8",
        )
    return results


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
    p_ceil = sub.add_parser("ceiling", help="precision vs alert-budget curve")
    p_ceil.add_argument("--model-dirs", type=Path, nargs="+", required=True)
    p_ceil.add_argument("--cooldown-min", type=int, default=120)
    p_fp = sub.add_parser("fperr", help="high-score false-positive error profile")
    p_fp.add_argument("--model-dirs", type=Path, nargs="+", required=True)
    p_fp.add_argument("--rate", type=float, default=1.0)
    p_bank = sub.add_parser("bankroll", help="train add_txn vs add_txn+bankroll")
    p_bank.add_argument("--seeds", type=int, nargs="+", default=[42, 43, 44])
    p_mom = sub.add_parser("momentum", help="train add_txn vs add_txn+momentum")
    p_mom.add_argument("--seeds", type=int, nargs="+", default=[42, 43, 44])
    p_ct = sub.add_parser("cashtiming", help="train add_txn_momentum +/- cash-out timing")
    p_ct.add_argument("--seeds", type=int, nargs="+", default=[42, 43, 44])
    p_pm = sub.add_parser("prodmom", help="validate promoted strict-PIT fe__outcome trio")
    p_pm.add_argument("--seeds", type=int, nargs="+", default=[42, 43, 44])
    args = parser.parse_args()
    if args.cmd == "prep":
        out = run_prep(with_txn=bool(args.with_txn))
    elif args.cmd == "matrix":
        out = run_matrix(seeds=tuple(args.seeds), arms=tuple(args.arms))
    elif args.cmd == "episode":
        out = run_episode_study(model_dirs=tuple(args.model_dirs))
    elif args.cmd == "dq":
        out = run_dq_audit(partitions=tuple(args.partitions))
    elif args.cmd == "ceiling":
        out = run_ceiling(
            model_dirs=tuple(args.model_dirs),
            cooldown_min=int(args.cooldown_min),
        )
    elif args.cmd == "fperr":
        out = run_fp_error_profile(
            model_dirs=tuple(args.model_dirs),
            rate=float(args.rate),
        )
    elif args.cmd == "bankroll":
        out = run_bankroll(seeds=tuple(args.seeds))
    elif args.cmd == "momentum":
        out = run_momentum(seeds=tuple(args.seeds))
    elif args.cmd == "cashtiming":
        out = run_cashtiming(seeds=tuple(args.seeds))
    elif args.cmd == "prodmom":
        out = run_prodmom(seeds=tuple(args.seeds))
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
