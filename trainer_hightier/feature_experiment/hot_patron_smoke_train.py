"""Phase 1 smoke: baseline vs hot-patron feature arms (skip Optuna)."""

from __future__ import annotations

import argparse
import importlib
import json
import pickle
import time
from pathlib import Path
from typing import Any

import pandas as pd

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    HighTierObjectiveConfig,
    Step5TrainConfig,
    configs_from_run_profile,
    get_run_profile,
)
from trainer_hightier.feature_experiment.hot_patron_features import (
    HOT_PATRON_FEATURE_COLUMNS,
    validate_hot_feature_coverage,
)
from trainer_hightier.feature_experiment.feature_registry import MODEL_FEATURE_COLUMNS

_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")
aggregate_bets_to_player_game = _b5.aggregate_bets_to_player_game

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SPLITS = _REPO_ROOT / "trainer_hightier/artifacts/training_data/splits"
_DEFAULT_ENRICHED = _REPO_ROOT / "trainer_hightier/artifacts/training_data/training_set_fe_enriched.parquet"


def _materialize_hot_splits(
    *,
    splits_dir: Path,
    out_splits_dir: Path,
) -> dict[str, dict[str, float]]:
    """Materialize hot features on each split using train-only peer lookup."""

    from trainer_hightier.feature_experiment.hot_patron_features import (
        _peer_lookup_from_train_parquet,
        materialize_hot_patron_features,
    )

    need = [
        "bet_id",
        "gaming_day_event",
        "player_id",
        "game_id",
        "patron__adt__w180d_m1snap",
        "fe__canonical__wager_sum__today",
        "fe__canonical__avg_wager__today",
        "fe__wager_sum__w15m",
        "fe__canonical__elapsed_sec_since_first_bet__today",
        "mid_term_snapshot_missing_flag",
        "patron__gaming_days_cnt__w180d_m1snap",
        "patron__theo_win_sum__w180d_m1snap",
    ]
    out = Path(out_splits_dir)
    out.mkdir(parents=True, exist_ok=True)
    peer_lookup = _peer_lookup_from_train_parquet(Path(splits_dir) / "train.parquet")
    coverage: dict[str, dict[str, float]] = {}
    for split in ("train", "val", "test"):
        src = Path(splits_dir) / f"{split}.parquet"
        df = pd.read_parquet(src)
        hot_cols_df = materialize_hot_patron_features(df, peer_lookup=peer_lookup)
        hot_only = hot_cols_df[list(HOT_PATRON_FEATURE_COLUMNS)]
        merged = pd.concat([df.reset_index(drop=True), hot_only.reset_index(drop=True)], axis=1)
        coverage[split] = validate_hot_feature_coverage(merged, split_name=split)
        merged.to_parquet(out / f"{split}.parquet", index=False)
    sampled_src = Path(splits_dir) / "train_sampled.parquet"
    if sampled_src.is_file():
        df_s = pd.read_parquet(sampled_src)
        hot_s = materialize_hot_patron_features(df_s, peer_lookup=peer_lookup)
        merged_s = pd.concat(
            [df_s.reset_index(drop=True), hot_s[list(HOT_PATRON_FEATURE_COLUMNS)].reset_index(drop=True)],
            axis=1,
        )
        coverage["train_sampled"] = validate_hot_feature_coverage(
            merged_s,
            split_name="train_sampled",
        )
        merged_s.to_parquet(out / "train_sampled.parquet", index=False)
    return coverage


def _hot_fp_concentration(model_dir: Path, splits_dir: Path) -> dict[str, Any]:
    """Measure top-10 player-day FP share on test split."""

    bundle = pickle.loads((Path(model_dir) / "model.pkl").read_bytes())
    test = pd.read_parquet(Path(splits_dir) / "test.parquet")
    feat = tuple(bundle["feature_columns"])
    from trainer_hightier.serving.feature_builder import prepare_lgbm_feature_matrix

    x = prepare_lgbm_feature_matrix(
        test,
        feature_columns=feat,
        categorical_columns=tuple(bundle.get("categorical_columns", ())),
        category_categories=dict(bundle.get("category_categories", {})),
    )
    scores = bundle["model"].predict_proba(x)[:, 1]
    thr = float(bundle["threshold"])
    pg = aggregate_bets_to_player_game(test, scores, split_name="test")
    cand = pg.candidates.copy()
    cand["alert"] = cand["player_game_score"] >= thr
    cand["fp"] = cand["alert"] & (cand["player_game_label"] == 0)
    rep = test.groupby(["player_id", "game_id"], as_index=False).first()[
        ["player_id", "game_id", "gaming_day_event"]
    ]
    cand = cand.merge(rep, on=["player_id", "game_id"])
    fp = cand.loc[cand["fp"] == 1]
    if fp.empty:
        return {"top10_player_day_fp_share": 0.0, "test_fp": 0}
    tbl = (
        fp.groupby(["player_id", "gaming_day_event"])
        .size()
        .reset_index(name="fp_count")
        .sort_values("fp_count", ascending=False)
    )
    total = int(len(fp))
    top10 = int(tbl.head(10)["fp_count"].sum())
    return {"top10_player_day_fp_share": top10 / total, "test_fp": total}


def _train_arm(
    *,
    splits_dir: Path,
    feature_columns: tuple[str, ...],
    output_dir: Path,
    duck: DuckDbRuntimeConfig,
    min_prec: float,
    train_parquet: Path | None = None,
) -> dict[str, Any]:
    """Train one feature arm and return metrics report."""

    step5 = Step5TrainConfig(run_step5=True, skip_optuna=True)
    res = _b5.train_lgbm_from_splits(
        splits_dir=splits_dir,
        duckdb_runtime=duck,
        objective_min_precision=min_prec,
        random_seed=42,
        step5=step5,
        output_dir=output_dir,
        feature_columns=feature_columns,
        train_parquet=train_parquet,
    )
    hot = _hot_fp_concentration(output_dir, splits_dir)
    rep = dict(res.report)
    rep.update(hot)
    return rep


def run_smoke(
    *,
    enriched_parquet: Path,
    splits_dir: Path,
    out_dir: Path,
) -> dict[str, Any]:
    """Run baseline + ablation arms for hot-patron features."""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    hot_splits = out_dir / "splits_hot"
    coverage = _materialize_hot_splits(splits_dir=splits_dir, out_splits_dir=hot_splits)
    duck, _, _ = configs_from_run_profile(get_run_profile("default"))
    min_prec = HighTierObjectiveConfig().min_precision
    baseline_cols = tuple(MODEL_FEATURE_COLUMNS)
    sampled_train = hot_splits / "train_sampled.parquet"
    train_p = sampled_train if sampled_train.is_file() else None
    arms: dict[str, Any] = {}
    t0 = time.perf_counter()
    arms["baseline"] = _train_arm(
        splits_dir=hot_splits,
        feature_columns=baseline_cols,
        output_dir=out_dir / "baseline",
        duck=duck,
        min_prec=min_prec,
        train_parquet=train_p,
    )
    ablation_feats = (
        "fe__hot__wager_today_over_peer_p95__adt_decile",
        "fe__hot__peer_wager_z__adt_decile",
        "fe__hot__mid_term_history_sparse_flag",
    )
    for feat in ablation_feats:
        cols = tuple(dict.fromkeys(baseline_cols + (feat,)))
        safe = feat.replace("__", "_")
        arms[f"add_{safe}"] = _train_arm(
            splits_dir=hot_splits,
            feature_columns=cols,
            output_dir=out_dir / f"add_{safe}",
            duck=duck,
            min_prec=min_prec,
            train_parquet=train_p,
        )
    all_cols = tuple(dict.fromkeys(baseline_cols + HOT_PATRON_FEATURE_COLUMNS))
    arms["add_all_hot"] = _train_arm(
        splits_dir=hot_splits,
        feature_columns=all_cols,
        output_dir=out_dir / "add_all_hot",
        duck=duck,
        min_prec=min_prec,
        train_parquet=train_p,
    )
    summary = {
        "elapsed_sec": round(time.perf_counter() - t0, 1),
        "hot_feature_null_rates": coverage,
        "arms": {
            k: {
                "test_operational_simulated_precision": v.get("test_operational_simulated_precision"),
                "test_operational_simulated_alerts_per_hour": v.get(
                    "test_operational_simulated_alerts_per_hour",
                ),
                "test_player_game_precision": v.get("test_player_game_precision"),
                "test_alerts_per_hour": v.get("test_alerts_per_hour"),
                "top10_player_day_fp_share": v.get("top10_player_day_fp_share"),
                "test_fp": v.get("test_fp"),
                "step5_min_precision": v.get("step5_min_precision"),
            }
            for k, v in arms.items()
        },
    }
    (out_dir / "smoke_report.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    """CLI entry for Phase 1 smoke training."""

    parser = argparse.ArgumentParser(description="Hot-patron feature smoke ablation")
    parser.add_argument("--enriched", type=Path, default=_DEFAULT_ENRICHED)
    parser.add_argument("--splits-dir", type=Path, default=_DEFAULT_SPLITS)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_REPO_ROOT / "trainer_hightier/artifacts/feature_experiment/hot_patron_smoke",
    )
    args = parser.parse_args()
    rep = run_smoke(
        enriched_parquet=args.enriched,
        splits_dir=args.splits_dir,
        out_dir=args.out_dir,
    )
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
