"""CLI entry for independent feature-group exploration (Step 3 onward, read-only L0).

Orchestrates: Feast training Parquet → DuckDB ``fe__*`` materialization → join →
:class:`~trainer_hightier.config.Step4SplitConfig` split → optional training-day
floor → **Feature Quality Gate (FQG v0)** → baseline vs full-candidate LightGBM (no Optuna)
→ JSON gate summary. Optional ``--ablation``: per ``group_*`` add-one vs baseline +
leave-one-out vs full, with KEEP/DROP/REVIEW summaries (WORKING_PLAN Gate 1 / C2).
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import pickle
import time
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import yaml
from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    FeatureQualityGateConfig,
    HighTierObjectiveConfig,
    Step4SplitConfig,
    Step5TrainConfig,
    configs_from_run_profile,
    get_run_profile,
)
from trainer_hightier.feature_experiment.feature_quality_gate import (
    FeatureQualityGateResult,
    parse_warn_approvals_yaml,
    run_feature_quality_gate,
    write_fqg_json_bundle,
)
from trainer_hightier.feature_experiment.dataset_enrich import enrich_training_parquet
from trainer_hightier.feature_experiment.ablation import (
    compute_gate1_vs_baseline,
    delta_full_minus_loo,
    experimental_group_ids,
    feature_columns_add_one,
    feature_columns_leave_one_out_minus,
    synthesize_group_decision_v0,
)
from trainer_hightier.feature_experiment.feature_registry import (
    candidate_registry_snapshot,
    set_candidate_registry_path,
)
import trainer_hightier.feature_experiment.feature_registry as _feat_registry
from trainer_hightier.feature_experiment.materialize_fe_derived import materialize_fe_derived_parquet
from trainer_hightier.feature_experiment.materialize_txn_lite import (
    default_raw_casino_txn_parquet,
    materialize_txn_lite_parquet,
    write_txn_lite_sidecars,
)
from trainer_hightier.feature_experiment.val_slices import (
    contiguous_val_day_masks,
    median_p25,
    per_slice_average_precision,
    per_slice_recall_at_threshold,
)
from trainer_hightier.utils.bet_l0_preprocess import default_cleaned_bet_parquet_path
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas
from trainer_hightier.utils.walkaway_labels import default_walkaway_labels_parquet_path

logger = logging.getLogger(__name__)

_b3 = importlib.import_module("trainer_hightier.03_build_training_data")
_b4 = importlib.import_module("trainer_hightier.04_split_dataset")
_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")
_TRAINER_HIGHTIER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _TRAINER_HIGHTIER_ROOT.parent
_DEFAULT_FEATURE_EXP_ROOT = _TRAINER_HIGHTIER_ROOT / "artifacts" / "feature_experiment"


@dataclass(frozen=True)
class FeatureExperimentPaths:
    """Resolved artifact locations for one experiment run."""

    run_dir: Path
    step3_training_parquet: Path
    fe_derived_parquet: Path
    txn_lite_parquet: Path | None
    enriched_training_parquet: Path
    splits_dir: Path
    report_json: Path


def _feature_columns_intersect_allowlist(columns: tuple[str, ...], allow: frozenset[str]) -> tuple[str, ...]:
    """Keep column order from ``columns`` but drop anything not in ``allow``."""

    return tuple(c for c in columns if c in allow)


def _feature_columns_present_in_splits(
    splits_dir: Path,
    columns: tuple[str, ...],
) -> tuple[str, ...]:
    """Drop registry columns that are absent from split Parquet (legacy enrich gaps)."""

    train_p = Path(splits_dir) / "train.parquet"
    names = frozenset(pq.read_schema(train_p).names)
    present = tuple(c for c in columns if c in names)
    missing = [c for c in columns if c not in names]
    if missing:
        logger.warning(
            "[FE] Registry columns absent from splits (dropped for training): %s",
            missing,
        )
    return present


def _repo_root() -> Path:
    return _REPO_ROOT


def _duckdb_from_profile(name: str) -> DuckDbRuntimeConfig:
    profile = get_run_profile(name)
    duck, _sess, _bet = configs_from_run_profile(profile)
    return duck


def _load_yaml_config(path: Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Config must be a mapping, got {type(data)} from {path}")
    return data


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "trainer_hightier feature experiment runner: Step 3 (Feast join) + FE materialize + "
            "Step 4 split + dual LightGBM baseline/candidate."
        ),
    )
    p.add_argument(
        "--config",
        type=Path,
        default=_TRAINER_HIGHTIER_ROOT / "feature_experiment" / "experiment_config.yaml",
        help="YAML with training_sample_min_gaming_day, split fractions, budgets, run_profile.",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Experiment output root (default trainer_hightier/artifacts/feature_experiment/run_<ts>).",
    )
    p.add_argument(
        "--skip-step3",
        action="store_true",
        help=(
            "Reuse an existing Step-3 parquet (--step3-output, else trainer default "
            "artifacts/training_data/training_set.parquet) instead of rebuilding."
        ),
    )
    p.add_argument(
        "--step3-output",
        type=Path,
        default=None,
        help="Override Step-3 training parquet path used as FE join input / tid filter.",
    )
    p.add_argument(
        "--cleaned-bet",
        type=Path,
        default=None,
        help="Override cleaned bet Parquet root for FE materialization.",
    )
    p.add_argument(
        "--labels-parquet",
        type=Path,
        default=None,
        help="Walkaway labels parquet for Step 3 (only if Step 3 enabled).",
    )
    p.add_argument(
        "--min-precision",
        type=float,
        default=HighTierObjectiveConfig().min_precision,
        help="Recall@Pmin evaluation uses this precision floor (aligns with Step 5 picker).",
    )
    p.add_argument(
        "--ablation",
        action="store_true",
        help=(
            "Train each experimental group add-one vs baseline and leave-one-out vs full candidate; "
            "writes gate1_ablation_report.json and embeds ablation_v0 in the main JSON."
        ),
    )
    p.add_argument(
        "--skip-fqg",
        action="store_true",
        help="Skip Feature Quality Gate (FQG); for bring-up/debug only.",
    )
    p.add_argument(
        "--fqg-warn-approvals",
        type=Path,
        default=None,
        help="YAML with approved_warn_features: [feature_name, ...] for FQG WARN overrides.",
    )
    p.add_argument(
        "--disable-auto-feast-apply",
        action="store_true",
        dest="disable_auto_feast_apply",
        help=(
            "Before Step 3: fail when feast_repo/data/registry.db is missing instead of running `feast apply`. "
            "Default is auto-apply when registry is absent."
        ),
    )
    return p.parse_args()


def _resolve_paths(cfg: Mapping[str, Any], output_dir: Path | None) -> FeatureExperimentPaths:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_root = (
        Path(output_dir).resolve()
        if output_dir is not None
        else (_DEFAULT_FEATURE_EXP_ROOT / f"run_{ts}").resolve()
    )
    run_root.mkdir(parents=True, exist_ok=True)
    step3_out = run_root / "training_set_step3.parquet"
    fe = run_root / "fe_derived.parquet"
    enriched = run_root / "training_set_enriched.parquet"
    splits = run_root / "splits"
    rep = run_root / "feature_experiment_report.json"
    return FeatureExperimentPaths(
        run_dir=run_root,
        step3_training_parquet=step3_out,
        fe_derived_parquet=fe,
        txn_lite_parquet=None,
        enriched_training_parquet=enriched,
        splits_dir=splits,
        report_json=rep,
    )


def _run_step3(
    *,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    feature_service_name: str,
    cleaned_bet: Path,
    labels: Path,
    materialize_derived: bool,
    auto_feast_apply: bool = True,
) -> None:
    feast_repo = (_TRAINER_HIGHTIER_ROOT / "feast_repo").resolve()
    bcfg = _b3.BuildTrainingDataArgs(
        feast_repo=feast_repo,
        cleaned_bet_parquet=cleaned_bet.resolve(),
        labels_parquet=labels.resolve(),
        output_parquet=out_parquet.resolve(),
        feature_service_name=feature_service_name,
        materialize_derived_features=materialize_derived,
        max_entity_rows=None,
        duckdb_runtime=duckdb_runtime,
        feast_entity_batch_by_calendar_month=True,
        training_set_keep_last_n_versions=10,
        feast_retrieval_cache_enabled=True,
        auto_feast_apply=bool(auto_feast_apply),
    )
    _b3.build_training_data(bcfg)


def _filter_train_floor(*, train_parquet: Path, min_day: date, duckdb_runtime: DuckDbRuntimeConfig) -> None:
    mq = str(Path(train_parquet).resolve()).replace("\\", "/").replace("'", "''")
    tmp = Path(train_parquet).with_suffix(".tmp_filter.parquet")
    tq = str(tmp.resolve()).replace("\\", "/").replace("'", "''")
    day_s = min_day.isoformat()
    inner = f"""
SELECT * FROM read_parquet('{mq}')
WHERE TRY_CAST(gaming_day_event AS DATE) >= DATE '{day_s}'
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(f"COPY ({inner}) TO '{tq}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
    finally:
        con.close()
    tmp.replace(Path(train_parquet))


def _slice_training_parquet_by_gaming_day(
    *,
    in_parquet: Path,
    out_parquet: Path,
    min_day: date | None,
    max_day: date | None,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> Path:
    """Filter training rows by ``gaming_day_event`` inclusive range (experiment subset)."""

    if min_day is None and max_day is None:
        return Path(in_parquet).resolve()
    src = Path(in_parquet).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"training parquet missing: {src}")
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    iq = str(src).replace("\\", "/").replace("'", "''")
    oq = str(dst).replace("\\", "/").replace("'", "''")
    clauses: list[str] = ["TRY_CAST(gaming_day_event AS DATE) IS NOT NULL"]
    if min_day is not None:
        clauses.append(f"TRY_CAST(gaming_day_event AS DATE) >= DATE '{min_day.isoformat()}'")
    if max_day is not None:
        clauses.append(f"TRY_CAST(gaming_day_event AS DATE) <= DATE '{max_day.isoformat()}'")
    where = " AND ".join(clauses)
    inner = f"SELECT * FROM read_parquet('{iq}') WHERE {where}"
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        n_before = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{iq}')").fetchone()[0])
        con.execute(f"COPY ({inner}) TO '{oq}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
        n_after = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{oq}')").fetchone()[0])
    finally:
        con.close()
    if n_after <= 0:
        raise ValueError(
            f"gaming_day_event slice [{min_day}, {max_day}] yielded 0 rows from {src} "
            f"(before={n_before})",
        )
    logger.info(
        "[FE] sliced training rows by gaming_day_event [%s, %s]: %d → %d → %s",
        min_day,
        max_day,
        n_before,
        n_after,
        dst,
    )
    return dst


def prepare_matrix_from_val_split(val_df: pd.DataFrame, pkt: dict[str, Any]) -> pd.DataFrame:
    """Cast validation rows to dtypes expected by pickles written in Step 5."""

    cols: list[str] = list(pkt["feature_columns"])
    cats = pkt.get("category_categories") or {}
    xf = val_df.loc[:, cols].copy()
    for c in cols:
        if c in cats:
            xf[c] = pd.Categorical(xf[c].astype(str), categories=list(cats[c]))
        else:
            xf[c] = pd.to_numeric(xf[c], errors="coerce")
    return xf


def predicted_positive_class_probability(model: Any, xf: pd.DataFrame) -> np.ndarray:
    """Return calibrated ``p(y=1)`` vector from LightGBM ``predict_proba``."""

    out = np.asarray(model.predict_proba(xf), dtype=np.float64)
    return out[:, 1]


def _build_report(
    *,
    baseline_report: dict[str, Any],
    cand_report: dict[str, Any],
    timing: dict[str, float],
    cfg: Mapping[str, Any],
    paths: FeatureExperimentPaths,
    val_slice_block: dict[str, Any],
    budget_sec: float,
    capacity_alerts_per_hour_cap: float,
    ablation_v0: dict[str, Any] | None = None,
    feature_quality: dict[str, Any] | None = None,
    candidate_registry: dict[str, Any] | None = None,
    feast_auto_apply: Mapping[str, Any] | None = None,
    external_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    gate1_inner = compute_gate1_vs_baseline(
        baseline_report,
        cand_report,
        capacity_alerts_per_hour_cap=capacity_alerts_per_hour_cap,
        arm_side_key_prefix="candidate",
    )
    gate1_pass = bool(gate1_inner.get("pass_v0_thresholds"))
    reason_codes = list(gate1_inner.get("reason_codes_if_fail") or [])

    feat_group = _feat_registry.FEATURE_GROUP_TAGS
    baseline_cols_registry = _feat_registry.MODEL_FEATURE_COLUMNS
    cand_full_registry = _feat_registry.FULL_CANDIDATE_FEATURE_COLUMNS
    experimental_num = _feat_registry.EXPERIMENTAL_NUMERIC_COLUMNS

    out: dict[str, Any] = {
        "experiment_paths": {k: str(v) if isinstance(v, Path) else v for k, v in paths.__dict__.items()},
        "config_echo": dict(cfg),
        "timing_sec": timing,
        "budget_sec": budget_sec,
        "baseline_metrics": baseline_report,
        "candidate_metrics": cand_report,
        "gate1": {
            **gate1_inner,
            "reason_codes_if_fail": reason_codes if not gate1_pass else [],
        },
        "feature_groups_fe": {k: list(v) for k, v in feat_group.items()},
        "experimental_numeric_columns": list(experimental_num),
        "candidate_feature_columns_registry_full": list(cand_full_registry),
        "baseline_feature_columns_registry": list(baseline_cols_registry),
        "val_slice_robustness_v0": val_slice_block,
    }
    if candidate_registry is not None:
        out["candidate_registry"] = candidate_registry
    if feast_auto_apply is not None:
        out["feast_auto_apply"] = dict(feast_auto_apply)
    if feature_quality is not None:
        out["feature_quality"] = feature_quality
    if ablation_v0 is not None:
        out["ablation_v0"] = ablation_v0
    if external_sources is not None:
        out["external_sources"] = external_sources
    return out


def _safe_ablation_dir_name(group_id: str) -> str:
    """Filesystem-safe subdirectory name for ``group_id``."""

    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in group_id)


def _ablation_train_add_one_arms(
    *,
    splits_dir: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    objective_min_precision: float,
    random_seed: int,
    step5: Step5TrainConfig,
    run_dir: Path,
    baseline_report: Mapping[str, Any],
    capacity_alerts_per_hour_cap: float,
    budget_deadline_perf: float,
    groups: tuple[str, ...],
    allow: frozenset[str],
) -> tuple[dict[str, Any], dict[str, float]]:
    """Train baseline+single-group models; return arm payloads and timing."""

    add_one_block: dict[str, Any] = {}
    timing_ablation: dict[str, float] = {}
    for gid in groups:
        if time.perf_counter() > budget_deadline_perf:
            raise RuntimeError("Exceeded single_round_budget_sec during ablation add-one arms")
        cols = tuple(c for c in feature_columns_add_one(gid) if c in allow)
        sub = _safe_ablation_dir_name(gid)
        out_a = run_dir / f"ablation_add_one_{sub}_lgbm"
        logger.info("[FE] ablation add-one %s (%d features) → %s", gid, len(cols), out_a)
        t0 = time.perf_counter()
        res_a = _b5.train_lgbm_from_splits(
            splits_dir=splits_dir,
            duckdb_runtime=duckdb_runtime,
            objective_min_precision=float(objective_min_precision),
            random_seed=random_seed,
            step5=step5,
            output_dir=out_a,
            feature_columns=cols,
        )
        timing_ablation[f"train_ablation_add_one_{sub}_sec"] = round(time.perf_counter() - t0, 3)
        g1 = compute_gate1_vs_baseline(
            dict(baseline_report),
            res_a.report,
            capacity_alerts_per_hour_cap=capacity_alerts_per_hour_cap,
            arm_side_key_prefix="arm",
        )
        add_one_block[gid] = {
            "experiment_kind": "add_one",
            "feature_columns": list(cols),
            "model_dir": str(out_a.resolve()),
            "metrics": res_a.report,
            "gate1_vs_baseline": g1,
        }
        if bool(g1.get("capacity_alarm")):
            logger.warning(
                "[FE] ablation add-one %s: arm val_alerts_per_hour=%s exceeds cap=%s",
                gid,
                g1.get("arm_val_alerts_per_hour"),
                capacity_alerts_per_hour_cap,
            )
    return add_one_block, timing_ablation


def _ablation_train_loo_arms(
    *,
    splits_dir: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    objective_min_precision: float,
    random_seed: int,
    step5: Step5TrainConfig,
    run_dir: Path,
    full_candidate_report: Mapping[str, Any],
    capacity_alerts_per_hour_cap: float,
    budget_deadline_perf: float,
    groups: tuple[str, ...],
    allow: frozenset[str],
) -> tuple[dict[str, Any], dict[str, float]]:
    """Train full-minus-group models; return LOO payloads and timing."""

    loo_block: dict[str, Any] = {}
    timing_ablation: dict[str, float] = {}
    for gid in groups:
        if time.perf_counter() > budget_deadline_perf:
            raise RuntimeError("Exceeded single_round_budget_sec during ablation leave-one-out arms")
        cols = tuple(c for c in feature_columns_leave_one_out_minus(gid) if c in allow)
        sub = _safe_ablation_dir_name(gid)
        out_l = run_dir / f"ablation_loo_minus_{sub}_lgbm"
        logger.info("[FE] ablation leave-one-out minus %s (%d features) → %s", gid, len(cols), out_l)
        t0 = time.perf_counter()
        res_l = _b5.train_lgbm_from_splits(
            splits_dir=splits_dir,
            duckdb_runtime=duckdb_runtime,
            objective_min_precision=float(objective_min_precision),
            random_seed=random_seed,
            step5=step5,
            output_dir=out_l,
            feature_columns=cols,
        )
        timing_ablation[f"train_ablation_loo_minus_{sub}_sec"] = round(time.perf_counter() - t0, 3)
        dloo = delta_full_minus_loo(dict(full_candidate_report), res_l.report)
        loo_block[gid] = {
            "experiment_kind": "leave_one_out_minus_group",
            "removed_group_id": gid,
            "feature_columns": list(cols),
            "model_dir": str(out_l.resolve()),
            "metrics": res_l.report,
            "delta_full_candidate_minus_loo": dloo,
        }
        vlo = res_l.report.get("val_alerts_per_hour")
        if vlo is not None and float(vlo) > capacity_alerts_per_hour_cap:
            logger.warning(
                "[FE] ablation LOO minus %s: val_alerts_per_hour=%s exceeds cap=%s",
                gid,
                vlo,
                capacity_alerts_per_hour_cap,
            )
    return loo_block, timing_ablation


def _ablation_group_decisions(
    *,
    add_one_block: Mapping[str, Any],
    loo_block: Mapping[str, Any],
    groups: tuple[str, ...],
) -> dict[str, Any]:
    """Synthesize KEEP/DROP/REVIEW per experimental group."""

    decisions: dict[str, Any] = {}
    for gid in groups:
        add_pass = bool(add_one_block[gid]["gate1_vs_baseline"].get("pass_v0_thresholds"))
        dloo = loo_block[gid]["delta_full_candidate_minus_loo"]
        d_ap = float(dloo["delta_val_ap_full_minus_loo"])
        d_rec = float(dloo["delta_val_recall_full_minus_loo"])
        dec, reason = synthesize_group_decision_v0(
            group_id=gid,
            add_one_gate_pass=add_pass,
            delta_full_minus_loo_ap=d_ap,
            delta_full_minus_loo_rec=d_rec,
        )
        decisions[gid] = {
            "decision": dec,
            "reason_code": reason,
            "add_one_gate1_pass": add_pass,
            "delta_val_ap_full_minus_loo": d_ap,
            "delta_val_recall_full_minus_loo": d_rec,
        }
    return decisions


def _run_ablation_training_phase(
    *,
    splits_dir: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    objective_min_precision: float,
    random_seed: int,
    step5: Step5TrainConfig,
    run_dir: Path,
    baseline_report: Mapping[str, Any],
    full_candidate_report: Mapping[str, Any],
    capacity_alerts_per_hour_cap: float,
    budget_deadline_perf: float,
    allow: frozenset[str],
) -> dict[str, Any]:
    """Train add-one and leave-one-out arms; return ``ablation_v0`` blob for the main report."""

    groups = experimental_group_ids()
    t_ab0 = time.perf_counter()
    add_one_block, t_add = _ablation_train_add_one_arms(
        splits_dir=splits_dir,
        duckdb_runtime=duckdb_runtime,
        objective_min_precision=objective_min_precision,
        random_seed=random_seed,
        step5=step5,
        run_dir=run_dir,
        baseline_report=baseline_report,
        capacity_alerts_per_hour_cap=capacity_alerts_per_hour_cap,
        budget_deadline_perf=budget_deadline_perf,
        groups=groups,
        allow=allow,
    )
    loo_block, t_loo = _ablation_train_loo_arms(
        splits_dir=splits_dir,
        duckdb_runtime=duckdb_runtime,
        objective_min_precision=objective_min_precision,
        random_seed=random_seed,
        step5=step5,
        run_dir=run_dir,
        full_candidate_report=full_candidate_report,
        capacity_alerts_per_hour_cap=capacity_alerts_per_hour_cap,
        budget_deadline_perf=budget_deadline_perf,
        groups=groups,
        allow=allow,
    )
    timing_ablation = {**t_add, **t_loo}
    timing_ablation["train_ablation_total_sec"] = round(time.perf_counter() - t_ab0, 3)
    decisions = _ablation_group_decisions(add_one_block=add_one_block, loo_block=loo_block, groups=groups)
    return {
        "experimental_group_ids": list(groups),
        "add_one": add_one_block,
        "leave_one_out": loo_block,
        "group_decisions_v0": decisions,
        "decision_rule_note": (
            "KEEP if add-one passes Gate1 vs baseline; else KEEP if full candidate beats LOO on both "
            "ΔAP≥0.003 and ΔRecall>0; DROP if add-one fails and LOO shows removal not harmful; else REVIEW."
        ),
        "timing_sec": timing_ablation,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ns = _parse_args()
    cfg_yaml = _load_yaml_config(Path(ns.config))
    reg_yaml = cfg_yaml.get("feature_candidate_registry")
    set_candidate_registry_path(
        Path(str(reg_yaml)).resolve() if reg_yaml not in (None, "") else None,
    )
    reg_snap = candidate_registry_snapshot()
    registry_echo = {
        "registry_version": reg_snap.registry_version,
        "updated_at": reg_snap.updated_at,
        "resolved_path": str(reg_snap.path),
        "n_features_documented": len(reg_snap.rows),
        "n_selectable_candidate_fe": len(reg_snap.experimental_numeric_columns),
    }

    profile_name = str(cfg_yaml.get("run_profile", "default"))
    duck = _duckdb_from_profile(profile_name)

    paths = _resolve_paths(cfg_yaml, Path(ns.output_dir) if ns.output_dir is not None else None)

    cleaned = Path(ns.cleaned_bet).resolve() if ns.cleaned_bet else default_cleaned_bet_parquet_path()
    labels_path = (
        Path(ns.labels_parquet).resolve()
        if ns.labels_parquet
        else default_walkaway_labels_parquet_path(repo_root=_repo_root())
    )

    step3_dest = paths.step3_training_parquet
    if ns.step3_output is not None:
        step3_dest = Path(ns.step3_output).resolve()
    elif ns.skip_step3:
        step3_dest = Path(_b3.DEFAULT_OUTPUT).resolve()

    t_wall0 = time.perf_counter()
    budget = float(cfg_yaml.get("single_round_budget_sec", 3600))
    cap_alerts_hr = float(cfg_yaml.get("capacity_alerts_per_hour_cap", 120))

    tp_train = float(cfg_yaml.get("train_day_fraction", 0.70))
    tp_val = float(cfg_yaml.get("val_day_fraction", 0.15))
    min_day_raw = cfg_yaml.get("training_sample_min_gaming_day", "2025-01-01")
    min_day = date.fromisoformat(str(min_day_raw))
    max_day_raw = cfg_yaml.get("training_sample_max_gaming_day")
    max_day = date.fromisoformat(str(max_day_raw)) if max_day_raw not in (None, "") else None
    if max_day is not None and max_day < min_day:
        raise ValueError(
            f"training_sample_max_gaming_day={max_day} must be >= training_sample_min_gaming_day={min_day}",
        )
    svc = str(cfg_yaml.get("feature_service_name", "walkaway_bet_trial_v1"))
    rnd = int(cfg_yaml.get("random_seed", 42))

    timing: dict[str, float] = {}

    feast_repo_fe = (_TRAINER_HIGHTIER_ROOT / "feast_repo").resolve()
    if ns.skip_step3:
        feast_apply_echo: dict[str, Any] = {"skipped": True, "reason": "skip_step3"}
    else:
        _er_fe = _b3.ensure_feast_registry_ready(
            feast_repo_fe,
            auto_apply=not bool(ns.disable_auto_feast_apply),
        )
        feast_apply_echo = dict(_b3.feast_registry_ensure_result_to_metrics(_er_fe))

    step3_in: Path
    if ns.skip_step3:
        step3_in = step3_dest
        logger.info("[FE] skip Step 3; using %s", step3_in)
        if not step3_in.is_file():
            raise FileNotFoundError(
                f"--skip-step3 expects an existing parquet at {step3_in}; "
                f"also try --step3-output {_b3.DEFAULT_OUTPUT.as_posix()}",
            )
    else:
        logger.info("[FE] Step 3 → %s", step3_dest)
        t0 = time.perf_counter()
        _run_step3(
            out_parquet=step3_dest,
            duckdb_runtime=duck,
            feature_service_name=svc,
            cleaned_bet=cleaned,
            labels=labels_path,
            materialize_derived=True,
            auto_feast_apply=not bool(ns.disable_auto_feast_apply),
        )
        timing["step3_sec"] = round(time.perf_counter() - t0, 3)
        step3_in = step3_dest

    if min_day is not None or max_day is not None:
        sliced_path = (paths.run_dir / "training_set_sliced.parquet").resolve()
        step3_in = _slice_training_parquet_by_gaming_day(
            in_parquet=step3_in,
            out_parquet=sliced_path,
            min_day=min_day,
            max_day=max_day,
            duckdb_runtime=duck,
        )

    if time.perf_counter() - t_wall0 > budget:
        raise RuntimeError(f"Exceeded single_round_budget_sec={budget} after Step 3")

    # FE materialize
    logger.info("[FE] materializing DuckDB window features → %s", paths.fe_derived_parquet)
    t0 = time.perf_counter()
    materialize_fe_derived_parquet(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=step3_in,
        out_parquet=paths.fe_derived_parquet,
        duckdb_runtime=duck,
    )
    timing["fe_materialize_sec"] = round(time.perf_counter() - t0, 3)

    external_sources_echo: list[dict[str, Any]] = []
    txn_lite_path: Path | None = None
    ext_root = cfg_yaml.get("external_sources")
    txn_cfg = ext_root.get("t_casino_txn") if isinstance(ext_root, dict) else None
    if isinstance(txn_cfg, dict) and bool(txn_cfg.get("enabled")):
        raw_txn = Path(str(txn_cfg.get("raw_parquet", default_raw_casino_txn_parquet())))
        txn_lite_path = (paths.run_dir / "txn_lite.parquet").resolve()
        logger.info("[FE] materializing txn_lite (BUYIN/CASHOUT) → %s", txn_lite_path)
        t_txn = time.perf_counter()
        txn_meta = materialize_txn_lite_parquet(
            raw_casino_txn_parquet=raw_txn,
            training_parquet_for_bet_ids=step3_in,
            out_parquet=txn_lite_path,
            duckdb_runtime=duck,
        )
        mat_report_path, _src_meta_path = write_txn_lite_sidecars(
            run_dir=paths.run_dir,
            materialization_meta=txn_meta,
            out_parquet=txn_lite_path,
        )
        timing["txn_lite_materialize_sec"] = round(time.perf_counter() - t_txn, 3)
        paths = replace(paths, txn_lite_parquet=txn_lite_path)
        external_sources_echo.append(
            {
                **txn_meta,
                "materialization_report_path": str(mat_report_path.resolve()),
                "enabled": True,
            },
        )

    # Enrich
    logger.info("[FE] joining FE columns → %s", paths.enriched_training_parquet)
    t0 = time.perf_counter()
    enrich_training_parquet(
        base_training_parquet=step3_in,
        fe_derived_parquet=paths.fe_derived_parquet,
        out_parquet=paths.enriched_training_parquet,
        duckdb_runtime=duck,
        txn_lite_parquet=txn_lite_path,
    )
    timing["enrich_sec"] = round(time.perf_counter() - t0, 3)

    # Step 4
    logger.info("[FE] Step 4 splits → %s", paths.splits_dir)
    t0 = time.perf_counter()
    step4_cfg = Step4SplitConfig(
        train_day_fraction=tp_train,
        val_day_fraction=tp_val,
        splits_output_dir=paths.splits_dir,
    )
    _b4.arrange_and_split_training_data(
        features_parquet=paths.enriched_training_parquet,
        duckdb_runtime=duck,
        step4=step4_cfg,
    )
    timing["step4_sec"] = round(time.perf_counter() - t0, 3)

    # Training sample floor (train rows only)
    train_p = paths.splits_dir / "train.parquet"
    logger.info("[FE] applying training_sample_min_gaming_day=%s on %s", min_day_raw, train_p)
    t0 = time.perf_counter()
    _filter_train_floor(train_parquet=train_p, min_day=min_day, duckdb_runtime=duck)
    timing["train_floor_filter_sec"] = round(time.perf_counter() - t0, 3)

    if time.perf_counter() - t_wall0 > budget:
        raise RuntimeError(f"Exceeded single_round_budget_sec={budget} before model training")

    fqg_yaml = cfg_yaml.get("feature_quality_gate") or {}
    skip_fqg = bool(ns.skip_fqg or fqg_yaml.get("skip", False))
    warn_ap_path = ns.fqg_warn_approvals
    if warn_ap_path is None and fqg_yaml.get("warn_approvals_path"):
        warn_ap_path = Path(str(fqg_yaml["warn_approvals_path"])).resolve()

    fqg_result: FeatureQualityGateResult | None = None
    fqg_report_str: str | None = None
    fqg_allow_str: str | None = None
    fqg_block_str: str | None = None
    approved_warn_ct = 0

    if skip_fqg:
        logger.warning("[FE] FQG disabled (--skip-fqg or feature_quality_gate.skip); training uses full registry.")
        allow_f = frozenset(_feat_registry.FULL_CANDIDATE_FEATURE_COLUMNS)
    else:
        t_fq = time.perf_counter()
        fqg_cfg = FeatureQualityGateConfig()
        appr_sets: frozenset[str] = frozenset()
        if warn_ap_path is not None:
            appr_sets = parse_warn_approvals_yaml(Path(warn_ap_path))
            approved_warn_ct = len(appr_sets)
        qdir = paths.run_dir / "quality" / "fqg_v0"
        fqg_result = run_feature_quality_gate(
            splits_dir=paths.splits_dir,
            candidate_feature_columns=_feat_registry.FULL_CANDIDATE_FEATURE_COLUMNS,
            cfg=fqg_cfg,
            duckdb_runtime=duck,
            approved_warn_features=appr_sets,
        )
        rp_q, ap_q, bp_q = write_fqg_json_bundle(qdir, result=fqg_result)
        fqg_report_str, fqg_allow_str, fqg_block_str = str(rp_q.resolve()), str(ap_q.resolve()), str(bp_q.resolve())
        timing["fqg_sec"] = round(time.perf_counter() - t_fq, 3)
        if fqg_result.warn_pending:
            logger.warning("[FE] FQG WARN excluded unless approved: %s", ", ".join(fqg_result.warn_pending))
        if not fqg_result.fqg_pass:
            bl = ", ".join(f"{b.get('feature')}:{b.get('reason_code')}" for b in fqg_result.blocklist)
            raise RuntimeError(
                f"FQG fail-fast (blocked: {bl}); inspect feature_quality_report.json under {qdir}",
            )
        allow_f = frozenset(fqg_result.allowlist)

    baseline_target = _feature_columns_present_in_splits(
        paths.splits_dir,
        _feat_registry.MODEL_FEATURE_COLUMNS,
    )
    baseline_cols = _feature_columns_intersect_allowlist(baseline_target, allow_f)
    if not baseline_cols:
        raise RuntimeError(
            "No baseline feature columns remain after FQG allowlist and split schema filter.",
        )
    full_target = _feature_columns_present_in_splits(
        paths.splits_dir,
        _feat_registry.FULL_CANDIDATE_FEATURE_COLUMNS,
    )
    full_cols = _feature_columns_intersect_allowlist(full_target, allow_f)
    allow_train = frozenset(full_cols)
    fq_quality_echo = {
        "fqg_version": FeatureQualityGateConfig().fqg_version,
        "fqg_status": ("skipped" if skip_fqg else (fqg_result.fqg_status if fqg_result is not None else "fail")),
        "feature_quality_report_path": fqg_report_str,
        "feature_allowlist_path": fqg_allow_str,
        "feature_blocklist_path": fqg_block_str,
        "blocked_feature_count": len(fqg_result.blocklist) if fqg_result else 0,
        "warn_approved_feature_count": approved_warn_ct if not skip_fqg else 0,
        "warn_pending_features_after_fqg": list(fqg_result.warn_pending) if fqg_result else [],
        "n_baseline_features_used": len(baseline_cols),
        "n_candidate_features_used": len(full_cols),
        "baseline_feature_columns_used": list(baseline_cols),
        "candidate_feature_columns_used": list(full_cols),
        "registry_columns_dropped_not_in_splits": sorted(
            set(_feat_registry.FULL_CANDIDATE_FEATURE_COLUMNS) - set(full_cols),
        ),
    }

    step5 = Step5TrainConfig(run_step5=True, skip_optuna=True)
    out_base = paths.run_dir / "baseline_lgbm"
    out_cand = paths.run_dir / "candidate_lgbm"
    out_base.mkdir(parents=True, exist_ok=True)
    out_cand.mkdir(parents=True, exist_ok=True)

    logger.info("[FE] training baseline model (%d features)", len(baseline_cols))
    t0 = time.perf_counter()
    res_base = _b5.train_lgbm_from_splits(
        splits_dir=paths.splits_dir,
        duckdb_runtime=duck,
        objective_min_precision=float(ns.min_precision),
        random_seed=rnd,
        step5=step5,
        output_dir=out_base,
        feature_columns=baseline_cols,
    )
    timing["train_baseline_sec"] = round(time.perf_counter() - t0, 3)

    logger.info("[FE] training candidate model (%d features)", len(full_cols))
    t0 = time.perf_counter()
    res_cand = _b5.train_lgbm_from_splits(
        splits_dir=paths.splits_dir,
        duckdb_runtime=duck,
        objective_min_precision=float(ns.min_precision),
        random_seed=rnd,
        step5=step5,
        output_dir=out_cand,
        feature_columns=full_cols,
    )
    timing["train_candidate_sec"] = round(time.perf_counter() - t0, 3)

    budget_deadline = t_wall0 + budget
    ablation_v0: dict[str, Any] | None = None
    if ns.ablation:
        ablation_v0 = _run_ablation_training_phase(
            splits_dir=paths.splits_dir,
            duckdb_runtime=duck,
            objective_min_precision=float(ns.min_precision),
            random_seed=rnd,
            step5=step5,
            run_dir=paths.run_dir,
            baseline_report=res_base.report,
            full_candidate_report=res_cand.report,
            capacity_alerts_per_hour_cap=cap_alerts_hr,
            budget_deadline_perf=budget_deadline,
            allow=allow_train,
        )
        for ak, av in ablation_v0["timing_sec"].items():
            timing[ak] = av

    if time.perf_counter() - t_wall0 > budget:
        raise RuntimeError(f"Exceeded single_round_budget_sec={budget} before val slices / report")

    # Val slices — AP by contiguous val-day quartiles
    val_df = pd.read_parquet(paths.splits_dir / "val.parquet")
    masks = contiguous_val_day_masks(val_df["gaming_day_event"], k=4)
    y_va = pd.to_numeric(val_df[_b5.LABEL_COLUMN], errors="raise").astype(np.int8).to_numpy()

    with open(res_base.model_path, "rb") as f:
        pkt_b = pickle.load(f)
    with open(res_cand.model_path, "rb") as f:
        pkt_c = pickle.load(f)

    xf_b = prepare_matrix_from_val_split(val_df, pkt_b)
    xf_c = prepare_matrix_from_val_split(val_df, pkt_c)
    scores_b = predicted_positive_class_probability(pkt_b["model"], xf_b)
    scores_c = predicted_positive_class_probability(pkt_c["model"], xf_c)

    aps_b = per_slice_average_precision(y_va, scores_b, masks)
    aps_c = per_slice_average_precision(y_va, scores_c, masks)
    deltas_ap = [float(c - b) for b, c in zip(aps_b, aps_c, strict=True)]
    med_d_ap, p25_d_ap = median_p25(deltas_ap)

    thr_b = float(pkt_b["threshold"])
    thr_c = float(pkt_c["threshold"])
    r_b = per_slice_recall_at_threshold(y_va, scores_b, thr_b, masks)
    r_c = per_slice_recall_at_threshold(y_va, scores_c, thr_c, masks)
    deltas_r = [float(c - b) for b, c in zip(r_b, r_c, strict=True)]
    med_d_r, p25_d_r = median_p25(deltas_r)
    k_sl = len(masks)
    recall_robust_ok: bool | None
    if k_sl >= 2 and med_d_r is not None and p25_d_r is not None:
        recall_robust_ok = med_d_r > 0.0 and p25_d_r > 0.0
    else:
        recall_robust_ok = None

    slice_block: dict[str, Any] = {
        "k_requested": 4,
        "k_slices": k_sl,
        "baseline_ap_per_slice": aps_b,
        "candidate_ap_per_slice": aps_c,
        "delta_ap_per_slice": deltas_ap,
        "median_delta_ap": med_d_ap,
        "p25_delta_ap": p25_d_ap,
        "baseline_r_at_pmin_threshold_per_slice": r_b,
        "candidate_r_at_pmin_threshold_per_slice": r_c,
        "delta_r_at_pmin_per_slice": deltas_r,
        "median_delta_r_at_pmin_per_slice": med_d_r,
        "p25_delta_r_at_pmin_per_slice": p25_d_r,
        "recall_slice_robustness_v0_pass": recall_robust_ok,
        "notes": (
            "Per-slice R uses each model's own val-picked threshold (Step 5 pickle). "
            "gate1 pass_v0_thresholds uses aggregate val metrics only; see recall_slice_robustness_v0_pass for K-slice ΔR summary."
        ),
    }

    timing["total_wall_sec"] = round(time.perf_counter() - t_wall0, 3)
    blob = _build_report(
        baseline_report=res_base.report,
        cand_report=res_cand.report,
        timing=timing,
        cfg=cfg_yaml,
        paths=paths,
        val_slice_block=slice_block,
        budget_sec=budget,
        capacity_alerts_per_hour_cap=cap_alerts_hr,
        ablation_v0=ablation_v0,
        feature_quality=fq_quality_echo,
        candidate_registry=registry_echo,
        feast_auto_apply=feast_apply_echo,
        external_sources=external_sources_echo or None,
    )
    if bool(blob["gate1"].get("capacity_alarm")):
        logger.warning(
            "[FE] CAPACITY ALARM: candidate val_alerts_per_hour=%s exceeds cap=%s (review before promote).",
            blob["gate1"].get("candidate_val_alerts_per_hour"),
            cap_alerts_hr,
        )
    paths.report_json.write_text(json.dumps(blob, indent=2, default=str), encoding="utf-8")
    logger.info("[FE] wrote %s (total_wall_sec=%s)", paths.report_json, timing["total_wall_sec"])
    if ns.ablation and ablation_v0 is not None:
        ga_path = paths.run_dir / "gate1_ablation_report.json"
        ga_path.write_text(json.dumps(ablation_v0, indent=2, default=str), encoding="utf-8")
        logger.info("[FE] wrote %s", ga_path)


if __name__ == "__main__":
    main()
