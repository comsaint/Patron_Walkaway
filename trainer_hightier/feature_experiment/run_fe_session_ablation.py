"""Ablation for fe__session__* features."""

import argparse
import json
import logging
from pathlib import Path

import pandas as pd

from trainer_hightier.config import SESSION_PIT_FEATURE_COLUMNS, DuckDbRuntimeConfig
from trainer_hightier.feature_experiment.materialize_fe_derived import materialize_fe_derived_parquet
from trainer_hightier.feature_experiment.session_pit_ablation import enrich_split_parquet_with_session_pit, run_ablation

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("fe_session_ablation")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--train-split", default="train_sampled")
    args = parser.parse_args()

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. We will use the existing splits
    splits_dir = Path("trainer_hightier/artifacts/training_data/splits").resolve()
    enriched_dir = out_dir / "splits_with_fe_session"
    enriched_dir.mkdir(parents=True, exist_ok=True)
    
    duckdb_runtime = DuckDbRuntimeConfig()
    
    materialization_paths = {}
    
    # We will generate a sidecar of fe_derived for each split and then just select the fe__session__ columns to join
    import duckdb
    from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas
    
    for split_name in [args.train_split, "val", "test"]:
        split_pqt = splits_dir / f"{split_name}.parquet"
        if not split_pqt.exists():
            continue
            
        sidecar_pqt = enriched_dir / f"{split_name}.fe_derived_sidecar.parquet"
        logger.info(f"Generating fe_derived for {split_name}...")
        from trainer_hightier.utils.bet_l0_preprocess import default_cleaned_bet_parquet_path
        materialize_fe_derived_parquet(
            cleaned_bet_parquet=default_cleaned_bet_parquet_path(),
            training_parquet_for_bet_ids=split_pqt,
            out_parquet=sidecar_pqt,
            duckdb_runtime=duckdb_runtime,
        )
        
        # Now left join
        enriched_pqt = enriched_dir / f"{split_name}.fe_session.parquet"
        cols = ",\n  ".join(f's."{c}" AS "{c}"' for c in SESSION_PIT_FEATURE_COLUMNS)
        sql = f"""
        SELECT b.*, {cols}
        FROM read_parquet('{split_pqt}') AS b
        LEFT JOIN read_parquet('{sidecar_pqt}') AS s
        ON TRY_CAST(b.bet_id AS DOUBLE) = s.bet_id
        """
        
        con = duckdb.connect(database=":memory:")
        try:
            apply_duckdb_runtime_pragmas(con, duckdb_runtime)
            con.execute(f"COPY ({sql}) TO '{str(enriched_pqt)}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
        finally:
            con.close()
            
        logger.info(f"Enriched {split_name}")
        materialization_paths[split_name] = str(enriched_pqt)
        
    # Run ablation
    logger.info("Running ablation...")
    from trainer_hightier.serving.candidate_registry_loader import load_candidate_registry
    registry = load_candidate_registry()
    
    from trainer_hightier.config import TRAIN_CANDIDATE_REGISTRY_PATH
    
    baseline_cols = [
        "wager", "casino_win", "is_back_bet", "bet_type", "type_of_bet",
        "bet__bets_cnt__w1h", "bet__wager_sum__w1h", "bet__back_bet_ratio__w1h", "bet__payout_odds_avg__w1h",
        "patron__theo_win_sum__w180d_m1snap", "patron__gaming_days_cnt__w180d_m1snap", "patron__adt__w180d_m1snap",
        "fe__wager_sum__w15m", "fe__bets_cnt__w15m",
        "mid_term_snapshot_age_days", "mid_term_snapshot_missing_flag",
        "fe__bets_cnt__w1d", "fe__wager_sum__w15m_over_w1d", "fe__wager_cv_w7d", "fe__payout_odds_z_prior_w30d",
        "fe__canonical__bets_cnt__today", "fe__canonical__wager_sum__today", "fe__canonical__avg_wager__today", "fe__canonical__elapsed_sec_since_first_bet__today",
        "fe__interarrival__lag2_sec", "fe__interarrival__last_gap_z__w7d", "fe__interarrival__last_gap_to_recent_mean_ratio__w1h", "fe__interarrival__cv__w1h",
        "fe__odds__payout_odds_z__w1h", "fe__odds__payout_odds_z__w7d", "fe__odds__payout_odds_to_recent_max_ratio__w1h", "fe__odds__payout_odds_step_ratio"
    ]
    
    # Check if fe_session columns exist
    df_check = pd.read_parquet(materialization_paths[args.train_split], columns=list(SESSION_PIT_FEATURE_COLUMNS))
    for c in SESSION_PIT_FEATURE_COLUMNS:
        assert c in df_check.columns, f"Missing {c}"
        
    add_one_cols = baseline_cols + list(SESSION_PIT_FEATURE_COLUMNS)
    
    res = run_ablation(
        train_split=args.train_split,
        source_splits_dir=splits_dir,
        enriched_splits_dir=enriched_dir,
        out_dir=out_dir,
        baseline_feature_columns=baseline_cols,
        add_one_feature_columns=add_one_cols,
        session_feature_columns=list(SESSION_PIT_FEATURE_COLUMNS),
        suffix=".fe_session",
    )
    
    res["session_materialization"] = materialization_paths
    res_path = out_dir / "fe_session_ablation_report.json"
    res_path.write_text(json.dumps(res, indent=2), encoding="utf-8")
    logger.info(f"Done. Wrote {res_path}")

if __name__ == "__main__":
    main()
