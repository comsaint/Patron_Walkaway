"""Quick hall uplift smoke: baseline vs add-one hall dummy probes.

Mapping conflict assumption (prototype):
- Multiple hall names for the same ``table_id`` are rename artifacts for the same table
  grouping, not distinct halls.
- Canonical hall per ``table_id``: prefer numeric hall codes (``1``..``6``) when present,
  else lexicographically smallest name.
- Unmapped ``table_id`` → ``hall__is_unknown``; mapped but not in train top-K → ``hall__is_other``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import time
from pathlib import Path
from typing import Any, Final

import duckdb
import pandas as pd

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    HighTierObjectiveConfig,
    Step5TrainConfig,
    configs_from_run_profile,
    get_run_profile,
)
from trainer_hightier.feature_experiment.ablation import compute_gate1_vs_baseline
from trainer_hightier.feature_experiment.feature_registry import MODEL_FEATURE_COLUMNS

_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MAPPING = _REPO_ROOT / "data/gmwds_data_GM_hall_table_mapping.xls"
_DEFAULT_SPLITS = _REPO_ROOT / "trainer_hightier/artifacts/training_data/splits"
_DEFAULT_OUT = _REPO_ROOT / "trainer_hightier/artifacts/feature_experiment/hall_probe_smoke"
_TOP_K_HALLS: Final[int] = 10
_CAPACITY_ALERTS_HR: Final[float] = 120.0


def _safe_dummy_name(hall: str) -> str:
    """Return a model-safe dummy column name for one canonical hall label."""

    slug = re.sub(r"[^0-9A-Za-z]+", "_", str(hall).strip()).strip("_")
    if not slug:
        slug = "blank"
    return f"hall__is_{slug}"


def resolve_canonical_hall(hall_labels: frozenset[str]) -> str:
    """Pick one canonical hall label when a table_id has rename duplicates."""

    cleaned = frozenset(str(h).strip() for h in hall_labels if str(h).strip())
    if not cleaned:
        raise ValueError(f"resolve_canonical_hall: empty hall_labels={hall_labels!r}")
    numeric = sorted((h for h in cleaned if h.isdigit()), key=lambda x: (len(x), x))
    if numeric:
        return numeric[0]
    return min(cleaned)


def load_canonical_table_hall_map(mapping_path: Path) -> tuple[dict[int, str], dict[str, Any]]:
    """Load table_id→canonical_hall map and conflict-resolution audit metadata."""

    src = Path(mapping_path).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"hall mapping not found: {src}")
    raw = pd.read_csv(src)
    expected = {"hall", "property", "table_id"}
    missing = expected.difference(raw.columns)
    if missing:
        raise ValueError(f"hall mapping missing columns {sorted(missing)}; got {list(raw.columns)!r}")
    work = raw.copy()
    work["table_id"] = pd.to_numeric(work["table_id"], errors="coerce")
    work["hall"] = work["hall"].astype(str).str.strip()
    work = work.loc[work["table_id"].notna()].copy()
    work["table_id"] = work["table_id"].astype("int64")

    grouped = work.groupby("table_id", as_index=False)["hall"].agg(lambda s: resolve_canonical_hall(frozenset(s)))
    mapping = {int(r.table_id): str(r.hall) for r in grouped.itertuples(index=False)}

    conflict_ids = (
        work.groupby("table_id")["hall"]
        .nunique()
        .loc[lambda s: s > 1]
    )
    audit: dict[str, Any] = {
        "mapping_path": str(src),
        "mapping_rows": int(len(work)),
        "distinct_table_id": int(work["table_id"].nunique()),
        "conflict_table_id_count": int(conflict_ids.shape[0]),
        "conflict_resolution_rule": (
            "same table_id with multiple hall names treated as rename; "
            "prefer numeric hall code else lexicographic min"
        ),
        "distinct_canonical_hall": int(len(set(mapping.values()))),
        "sample_conflicts": [
            {
                "table_id": int(tid),
                "raw_halls": sorted(work.loc[work["table_id"] == tid, "hall"].unique().tolist()),
                "canonical_hall": mapping[int(tid)],
            }
            for tid in conflict_ids.index[:8]
        ],
    }
    return mapping, audit


def _hall_counts_on_split(split_parquet: Path, hall_map: dict[int, str]) -> pd.Series:
    """Count canonical hall labels on one split via DuckDB."""

    con = duckdb.connect(database=":memory:")
    try:
        rows = con.execute(
            f"""
            SELECT TRY_CAST(table_id AS BIGINT) AS table_id
            FROM read_parquet('{Path(split_parquet).resolve().as_posix()}')
            WHERE TRY_CAST(table_id AS BIGINT) IS NOT NULL
            """,
        ).fetchdf()
    finally:
        con.close()
    halls = rows["table_id"].map(hall_map).fillna("__unknown__")
    return halls.value_counts()


def select_top_hall_dummy_columns(
    hall_map: dict[int, str],
    *,
    train_parquet: Path,
    top_k: int = _TOP_K_HALLS,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Choose train-frequency top-K hall dummies plus other/unknown flags."""

    counts = _hall_counts_on_split(train_parquet, hall_map)
    known = counts.drop(labels=["__unknown__"], errors="ignore")
    top_halls = tuple(str(h) for h in known.head(int(top_k)).index.tolist())
    cols = tuple(_safe_dummy_name(h) for h in top_halls) + ("hall__is_other", "hall__is_unknown")
    meta = {
        "top_k": int(top_k),
        "top_halls": list(top_halls),
        "hall_dummy_columns": list(cols),
        "train_hall_counts_top15": {
            str(k): int(v) for k, v in counts.head(15).items()
        },
    }
    return cols, meta


def materialize_hall_probe_splits(
    *,
    splits_dir: Path,
    out_splits_dir: Path,
    hall_map: dict[int, str],
    top_halls: tuple[str, ...],
    hall_dummy_columns: tuple[str, ...],
) -> dict[str, dict[str, float]]:
    """Write split parquets with hall dummy columns joined on ``table_id``."""

    src = Path(splits_dir).resolve()
    dst = Path(out_splits_dir).resolve()
    dst.mkdir(parents=True, exist_ok=True)
    top_set = frozenset(top_halls)
    coverage: dict[str, dict[str, float]] = {}

    for split in ("train", "val", "test"):
        in_p = src / f"{split}.parquet"
        out_p = dst / f"{split}.parquet"
        if not in_p.is_file():
            raise FileNotFoundError(f"missing split parquet: {in_p}")
        df = pd.read_parquet(in_p)
        if "table_id" not in df.columns:
            raise ValueError(f"{in_p} missing table_id column")
        tbl = pd.to_numeric(df["table_id"], errors="coerce").astype("Int64")
        canon = tbl.map(hall_map)
        for hall in top_halls:
            col = _safe_dummy_name(hall)
            df[col] = (canon == hall).fillna(False).astype("int8")
        mapped = canon.notna()
        in_top = canon.isin(top_set)
        df["hall__is_other"] = (mapped & ~in_top).astype("int8")
        df["hall__is_unknown"] = (~mapped).astype("int8")
        miss_cols = [c for c in hall_dummy_columns if c not in df.columns]
        if miss_cols:
            raise ValueError(f"materialize_hall_probe_splits missing columns {miss_cols}")
        df.to_parquet(out_p, index=False)
        n = max(int(len(df)), 1)
        coverage[split] = {
            "unknown_rate": float(df["hall__is_unknown"].mean()),
            "other_rate": float(df["hall__is_other"].mean()),
            "mapped_rate": float(1.0 - df["hall__is_unknown"].mean()),
        }
    sampled_src = src / "train_sampled.parquet"
    if sampled_src.is_file():
        df_s = pd.read_parquet(sampled_src)
        tbl_s = pd.to_numeric(df_s["table_id"], errors="coerce").astype("Int64")
        canon_s = tbl_s.map(hall_map)
        for hall in top_halls:
            col = _safe_dummy_name(hall)
            df_s[col] = (canon_s == hall).fillna(False).astype("int8")
        mapped_s = canon_s.notna()
        in_top_s = canon_s.isin(top_set)
        df_s["hall__is_other"] = (mapped_s & ~in_top_s).astype("int8")
        df_s["hall__is_unknown"] = (~mapped_s).astype("int8")
        df_s.to_parquet(dst / "train_sampled.parquet", index=False)
        coverage["train_sampled"] = {
            "unknown_rate": float(df_s["hall__is_unknown"].mean()),
            "other_rate": float(df_s["hall__is_other"].mean()),
            "mapped_rate": float(1.0 - df_s["hall__is_unknown"].mean()),
        }
    return coverage


def _train_arm(
    *,
    splits_dir: Path,
    feature_columns: tuple[str, ...],
    output_dir: Path,
    duck: DuckDbRuntimeConfig,
    min_prec: float,
    train_parquet: Path | None,
) -> dict[str, Any]:
    """Train one arm with Step 5 defaults (no Optuna)."""

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
    return dict(res.report)


def run_smoke(
    *,
    mapping_path: Path,
    splits_dir: Path,
    out_dir: Path,
    top_k: int = _TOP_K_HALLS,
) -> dict[str, Any]:
    """Run baseline vs baseline+hall dummy add-one smoke and write JSON report."""

    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    hall_map, map_audit = load_canonical_table_hall_map(mapping_path)
    train_p = Path(splits_dir).resolve() / "train.parquet"
    hall_cols, hall_meta = select_top_hall_dummy_columns(
        hall_map,
        train_parquet=train_p,
        top_k=top_k,
    )
    hall_splits = out_dir / "splits_hall"
    coverage = materialize_hall_probe_splits(
        splits_dir=splits_dir,
        out_splits_dir=hall_splits,
        hall_map=hall_map,
        top_halls=tuple(hall_meta["top_halls"]),
        hall_dummy_columns=hall_cols,
    )
    duck, _, _ = configs_from_run_profile(get_run_profile("default"))
    min_prec = HighTierObjectiveConfig().min_precision
    baseline_cols = tuple(MODEL_FEATURE_COLUMNS)
    hall_arm_cols = tuple(dict.fromkeys(baseline_cols + hall_cols))
    sampled_train = hall_splits / "train_sampled.parquet"
    train_sample = sampled_train if sampled_train.is_file() else None

    t0 = time.perf_counter()
    baseline_report = _train_arm(
        splits_dir=hall_splits,
        feature_columns=baseline_cols,
        output_dir=out_dir / "baseline",
        duck=duck,
        min_prec=min_prec,
        train_parquet=train_sample,
    )
    hall_report = _train_arm(
        splits_dir=hall_splits,
        feature_columns=hall_arm_cols,
        output_dir=out_dir / "add_hall_probe",
        duck=duck,
        min_prec=min_prec,
        train_parquet=train_sample,
    )
    gate1 = compute_gate1_vs_baseline(
        baseline_report,
        hall_report,
        capacity_alerts_per_hour_cap=_CAPACITY_ALERTS_HR,
        arm_side_key_prefix="arm",
    )
    summary: dict[str, Any] = {
        "elapsed_sec": round(time.perf_counter() - t0, 1),
        "mapping_audit": map_audit,
        "hall_feature_meta": hall_meta,
        "hall_coverage": coverage,
        "baseline": _metric_slice(baseline_report),
        "add_hall_probe": _metric_slice(hall_report),
        "gate1_vs_baseline": gate1,
        "verdict": _smoke_verdict(gate1),
    }
    report_path = out_dir / "hall_probe_smoke_report.json"
    report_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def _metric_slice(report: dict[str, Any]) -> dict[str, Any]:
    """Keep a compact metric subset for the smoke JSON."""

    keys = (
        "val_ap",
        "val_recall",
        "val_alerts_per_hour",
        "test_ap",
        "test_recall",
        "test_alerts_per_hour",
        "test_operational_simulated_precision",
        "test_operational_simulated_alerts_per_hour",
        "step5_val_pick_feasible",
        "step5_threshold",
    )
    return {k: report.get(k) for k in keys}


def _smoke_verdict(gate1: dict[str, Any]) -> str:
    """Human-readable smoke outcome from Gate 1 thresholds."""

    if bool(gate1.get("pass_v0_thresholds")):
        return "FAVORABLE: hall probe passes Gate1 vs baseline (prototype mapping assumption)."
    reasons = gate1.get("reason_codes_if_fail") or []
    d_ap = float(gate1.get("delta_val_ap", 0.0))
    d_rec = float(gate1.get("delta_val_recall_at_pmin_pick", 0.0))
    if d_ap >= 0.001 and d_rec > 0.0:
        return (
            "MIXED: recall improved but Gate1 not fully met; worth fixing mapping quality "
            f"and re-testing. reasons={reasons}"
        )
    return f"UNFAVORABLE: no meaningful uplift under Gate1. reasons={reasons}"


def main() -> None:
    """CLI entry for hall probe smoke."""

    parser = argparse.ArgumentParser(description="Hall feature quick smoke (baseline vs add-one)")
    parser.add_argument("--mapping", type=Path, default=_DEFAULT_MAPPING)
    parser.add_argument("--splits-dir", type=Path, default=_DEFAULT_SPLITS)
    parser.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--top-k", type=int, default=_TOP_K_HALLS)
    args = parser.parse_args()
    rep = run_smoke(
        mapping_path=args.mapping,
        splits_dir=args.splits_dir,
        out_dir=args.out_dir,
        top_k=int(args.top_k),
    )
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
