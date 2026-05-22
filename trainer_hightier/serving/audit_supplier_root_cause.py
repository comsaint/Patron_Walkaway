"""Deterministic row-level root-cause audit for scorer v2 feature suppliers.

Splits missing ``fe__*`` values into exactly one primary cause per row/feature:

- ``short_term_pit_builder``
- ``feast_online_mid_entity_missing``
- ``feast_online_mid_value_missing``
- ``join_failure`` (Feast raw value present, joined row null)
- ``composite_formula_failure`` (inputs present, composite output null)
- ``canonical_mapping_missing`` (empty ``canonical_id`` before Feast)

Run on the deploy host (same bundle layout as ``deploy/main.py``):

    python -m trainer_hightier.serving.audit_supplier_root_cause \\
        --bundle-dir /path/to/deploy_bundle \\
        --prediction-log /path/to/gmwds_deploy_predict_log.csv \\
        --output-json ./artifacts/feast/supplier_root_cause.json

Or sample recent allowlist bets from ClickHouse (no log required):

    python -m trainer_hightier.serving.audit_supplier_root_cause \\
        --bundle-dir /path/to/deploy_bundle \\
        --max-bets 2000 \\
        --lookback-hours 6
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from trainer_hightier.config import (
    HightierServingConfig,
    apply_hightier_serving_environ_overrides,
    set_hightier_serving_deploy_override,
)
from trainer_hightier.feature_experiment.feature_cadence import (
    feast_mid_columns_with_composite_dependencies,
    runtime_inputs_from_registry,
    short_term_enrich_columns_with_dependencies,
)
from trainer_hightier.serving.adt_allowlist import load_adt_allowlist_ids
from trainer_hightier.serving.audit_production_readiness import (
    _load_bundle_rel,
    _load_dotenv,
    _serving_config_for_bundle,
)
from trainer_hightier.serving.ch_adapter import get_clickhouse_client
from trainer_hightier.serving.feast_online_adapter import (
    FeastSdkOnlineAdapter,
    join_feast_lookup,
)
from trainer_hightier.serving.feature_builder import (
    attach_canonical_id,
    attach_mid_term_composite_columns,
    attach_short_term_pit_features,
    attach_synthetic_etl_and_prediction_visible,
)
from trainer_hightier.serving.feature_supply import (
    ScorerSupplierPlan,
    build_scorer_supplier_plan,
    load_frozen_registry_for_bundle,
)
from trainer_hightier.serving.model_bundle import load_hightier_model_bundle
from trainer_hightier.serving.runtime_config import HK_TZ
from trainer_hightier.serving.scorer import (
    _TBET_CASINO_WIN_SELECT,
    _TBET_PAYOUT_ODDS_SELECT,
    _TBET_WAGER_SELECT,
    _postprocess_incremental_bets_timestamps,
    fetch_bet_pool_window,
    fetch_bets_incremental,
)

logger = logging.getLogger(__name__)

ROOT_OK: Final[str] = "ok"
ROOT_SHORT_TERM: Final[str] = "short_term_pit_builder"
ROOT_FEAST_ENTITY: Final[str] = "feast_online_mid_entity_missing"
ROOT_FEAST_VALUE: Final[str] = "feast_online_mid_value_missing"
ROOT_JOIN: Final[str] = "join_failure"
ROOT_COMPOSITE: Final[str] = "composite_formula_failure"
ROOT_CANONICAL: Final[str] = "canonical_mapping_missing"

_ALL_ROOTS: Final[tuple[str, ...]] = (
    ROOT_OK,
    ROOT_SHORT_TERM,
    ROOT_FEAST_ENTITY,
    ROOT_FEAST_VALUE,
    ROOT_JOIN,
    ROOT_COMPOSITE,
    ROOT_CANONICAL,
)


def _is_null(value: Any) -> bool:
    """True when a feature cell is missing for audit purposes."""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _fetch_bets_by_bet_ids(bet_ids: list[int], *, cfg: HightierServingConfig) -> pd.DataFrame:
    """Fetch settled bet rows for explicit ``bet_id`` list (chunked IN query)."""
    if not bet_ids:
        return pd.DataFrame()
    client = get_clickhouse_client()
    placeholder = int(cfg.placeholder_player_id)
    chunk_sz = max(1, int(cfg.hightier_scorer_player_id_chunk_size))
    frames: list[pd.DataFrame] = []
    for i in range(0, len(bet_ids), chunk_sz):
        chunk = bet_ids[i : i + chunk_sz]
        in_list = ",".join(str(int(x)) for x in chunk)
        q = f"""
            SELECT
                bet_id,
                __etl_insert_Dtm,
                payout_complete_dtm,
                gaming_day,
                session_id,
                player_id,
                table_id,
                {_TBET_WAGER_SELECT},
                {_TBET_CASINO_WIN_SELECT},
                {_TBET_PAYOUT_ODDS_SELECT}
            FROM {cfg.source_db}.{cfg.tbet} FINAL
            WHERE bet_id IN ({in_list})
              AND payout_complete_dtm IS NOT NULL
              AND gaming_day IS NOT NULL
              AND wager > 0
              AND player_id IS NOT NULL
              AND player_id != {placeholder}
        """
        frames.append(client.query_df(q))
    out = pd.concat([f for f in frames if not f.empty], ignore_index=True) if frames else pd.DataFrame()
    if not out.empty:
        _postprocess_incremental_bets_timestamps(out)
    return out.drop_duplicates(subset=["bet_id"], keep="first")


def _load_bet_ids_from_prediction_log(path: Path, *, max_bets: int | None) -> list[int]:
    """Distinct ``bet_id`` values from exported prediction log CSV."""
    usecols = ("bet_id", "model_features_missing")
    df = pd.read_csv(path, usecols=lambda c: c in usecols)
    if "model_features_missing" in df.columns:
        miss = pd.to_numeric(df["model_features_missing"], errors="coerce").fillna(0)
        df = df.loc[miss > 0]
    bids = pd.to_numeric(df["bet_id"], errors="coerce").dropna().astype(np.int64).unique().tolist()
    if max_bets is not None and len(bids) > max_bets:
        bids = bids[: int(max_bets)]
    return [int(x) for x in bids]


def _load_audit_bets(
    *,
    cfg: HightierServingConfig,
    allowlist_ids: frozenset[int],
    prediction_log: Path | None,
    max_bets: int | None,
    lookback_hours: float,
) -> pd.DataFrame:
    """Load bet rows to diagnose (from log bet_ids or incremental allowlist fetch)."""
    cap = int(max_bets) if max_bets is not None else 5000
    if prediction_log is not None:
        bet_ids = _load_bet_ids_from_prediction_log(prediction_log, max_bets=max_bets)
        if not bet_ids:
            raise ValueError(f"[root-cause] no bet_id rows in prediction log {prediction_log}")
        bets = _fetch_bets_by_bet_ids(bet_ids, cfg=cfg)
        if bets.empty:
            raise ValueError(
                f"[root-cause] ClickHouse returned 0 bets for {len(bet_ids)} log bet_id(s)"
            )
        return bets.head(cap).reset_index(drop=True)
    bets = fetch_bets_incremental(
        None,
        lookback_hours=float(lookback_hours),
        limit_rows=cap,
        allowlist_player_ids=allowlist_ids,
    )
    if bets.empty:
        raise ValueError(
            "[root-cause] incremental fetch returned 0 bets; widen --lookback-hours or check allowlist"
        )
    return bets


def _build_scoring_pool(bets: pd.DataFrame, *, cfg: HightierServingConfig) -> pd.DataFrame:
    """Bounded hot pool matching ``scorer._fetch_scoring_batch`` semantics."""
    p_min = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce").min()
    p_max = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce").max()
    pool_start = (p_min - timedelta(hours=int(cfg.hot_feature_pool_lookback_hours))).to_pydatetime()
    pool_end = p_max.to_pydatetime()
    pids = sorted({int(x) for x in bets["player_id"].dropna().unique().tolist()})
    fan_cap = int(cfg.hightier_scorer_pool_player_fanout_cap)
    if len(pids) > fan_cap:
        logger.warning(
            "[root-cause] truncating pool fanout %d -> %d (OOM cap)",
            len(pids),
            fan_cap,
        )
        pids = pids[:fan_cap]
    pool = fetch_bet_pool_window(player_ids=pids, window_start=pool_start, window_end=pool_end)
    return attach_synthetic_etl_and_prediction_visible(pool)


@dataclass(frozen=True)
class PipelineSnapshots:
    """Intermediate frames for provenance (aligned row index = staged bets)."""

    staged_base: pd.DataFrame
    after_short: pd.DataFrame
    feast_raw: pd.DataFrame
    after_join: pd.DataFrame
    after_composite: pd.DataFrame
    present_canonical_ids: frozenset[str]


def _run_supplier_pipeline(
    bets: pd.DataFrame,
    pool: pd.DataFrame,
    *,
    mapping_parquet: Path,
    plan: ScorerSupplierPlan,
    feast_repo: Path,
    batch_size: int,
) -> PipelineSnapshots:
    """Replay scorer supplier stages and capture before/after frames."""
    short_cols = short_term_enrich_columns_with_dependencies(
        plan.short_term_cols,
        plan.mid_composite_cols,
    )
    feast_cols = feast_mid_columns_with_composite_dependencies(
        plan.feast_mid_cols,
        plan.mid_composite_cols,
    )
    staged_base = attach_canonical_id(bets, mapping_parquet=mapping_parquet)
    after_short = attach_short_term_pit_features(staged_base, pool, columns=short_cols)
    cids = sorted(
        {str(x).strip() for x in after_short["canonical_id"].tolist() if str(x).strip()}
    )
    adapter = FeastSdkOnlineAdapter(feast_repo=feast_repo)
    feast_frames: list[pd.DataFrame] = []
    bs = max(1, int(batch_size))
    for i in range(0, len(cids), bs):
        batch = cids[i : i + bs]
        feast_frames.append(
            adapter.lookup_mid_slow(
                batch,
                mid_columns=feast_cols,
                slow_columns=(),
            )
        )
    feast_raw = (
        pd.concat(feast_frames, ignore_index=True)
        if feast_frames
        else pd.DataFrame(columns=["canonical_id", *feast_cols])
    )
    lookup = join_feast_lookup(
        after_short,
        feast_raw,
        feature_columns=feast_cols,
        mid_columns=feast_cols,
        slow_columns=(),
    )
    after_join = lookup.values
    after_composite = attach_mid_term_composite_columns(
        after_join,
        plan.mid_composite_cols,
    )
    present: set[str] = set()
    if not feast_raw.empty and "canonical_id" in feast_raw.columns:
        present = set(feast_raw["canonical_id"].astype(str).str.strip().tolist())
    return PipelineSnapshots(
        staged_base=staged_base,
        after_short=after_short,
        feast_raw=feast_raw,
        after_join=after_join,
        after_composite=after_composite,
        present_canonical_ids=frozenset(present),
    )


def _feast_raw_by_canonical(feast_raw: pd.DataFrame) -> dict[str, pd.Series]:
    """Last row per ``canonical_id`` from Feast lookup frame."""
    if feast_raw.empty or "canonical_id" not in feast_raw.columns:
        return {}
    lk = feast_raw.copy()
    lk["canonical_id"] = lk["canonical_id"].astype(str).str.strip()
    lk = lk.drop_duplicates(subset=["canonical_id"], keep="last")
    return {str(r["canonical_id"]): r for _, r in lk.iterrows()}


def _classify_feast_dep(
    *,
    canonical_id: str,
    dep: str,
    present_ids: frozenset[str],
    raw_by_cid: dict[str, pd.Series],
    joined_row: pd.Series,
) -> str:
    """Classify one Feast mid dependency column for a single bet row."""
    cid = canonical_id.strip()
    if not cid:
        return ROOT_CANONICAL
    if cid not in present_ids:
        return ROOT_FEAST_ENTITY
    raw_row = raw_by_cid.get(cid)
    if raw_row is None:
        return ROOT_FEAST_ENTITY
    if _is_null(raw_row.get(dep)):
        return ROOT_FEAST_VALUE
    if _is_null(joined_row.get(dep)):
        return ROOT_JOIN
    return ROOT_OK


def _classify_composite_target(
    *,
    target: str,
    short_deps: tuple[str, ...],
    feast_deps: tuple[str, ...],
    canonical_id: str,
    short_row: pd.Series,
    joined_row: pd.Series,
    final_row: pd.Series,
    present_ids: frozenset[str],
    raw_by_cid: dict[str, pd.Series],
) -> str:
    """Primary root cause when *target* is null at final composite stage."""
    if not canonical_id.strip():
        return ROOT_CANONICAL
    for dep in short_deps:
        if _is_null(short_row.get(dep)):
            return ROOT_SHORT_TERM
    for dep in feast_deps:
        cause = _classify_feast_dep(
            canonical_id=canonical_id,
            dep=dep,
            present_ids=present_ids,
            raw_by_cid=raw_by_cid,
            joined_row=joined_row,
        )
        if cause != ROOT_OK:
            return cause
    if _is_null(final_row.get(target)):
        return ROOT_COMPOSITE
    return ROOT_OK


def _deps_for_feature(
    feature_id: str,
    registry_by_id: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (short_term_deps, feast_mid_deps) from registry ``runtime_inputs``."""
    row = registry_by_id.get(feature_id)
    short_deps: list[str] = []
    feast_deps: list[str] = []
    for supplier, cols in runtime_inputs_from_registry(row, feature_id):
        if supplier == "short_term_pit_builder":
            short_deps.extend(cols)
        elif supplier == "feast_online_mid":
            feast_deps.extend(cols)
    return tuple(dict.fromkeys(short_deps)), tuple(dict.fromkeys(feast_deps))


def _features_to_audit(plan: ScorerSupplierPlan, model_feats: tuple[str, ...]) -> tuple[str, ...]:
    """Model ``fe__*`` columns supplied by short-term, Feast, or composite paths."""
    wanted = set(plan.short_term_cols) | set(plan.feast_mid_cols) | set(plan.mid_composite_cols)
    return tuple(f for f in model_feats if f in wanted)


def _audit_feature_column(
    feature_id: str,
    snaps: PipelineSnapshots,
    *,
    plan: ScorerSupplierPlan,
    registry_by_id: dict[str, Any],
    only_null_rows: bool,
    max_examples: int,
) -> dict[str, Any]:
    """Row-level root-cause counts for one model feature."""
    final = snaps.after_composite
    short_frame = snaps.after_short
    joined = snaps.after_join
    raw_by_cid = _feast_raw_by_canonical(snaps.feast_raw)
    short_deps, feast_deps = _deps_for_feature(feature_id, registry_by_id)

    is_composite = feature_id in plan.mid_composite_cols
    is_short_only = feature_id in plan.short_term_cols and feature_id not in plan.feast_mid_cols
    is_feast_only = feature_id in plan.feast_mid_cols and feature_id not in plan.mid_composite_cols

    counts: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    n_null_final = 0

    for idx in final.index:
        final_row = final.loc[idx]
        if only_null_rows and not _is_null(final_row.get(feature_id)):
            continue
        if _is_null(final_row.get(feature_id)):
            n_null_final += 1

        cid = str(final_row.get("canonical_id", "")).strip()
        short_row = short_frame.loc[idx]
        joined_row = joined.loc[idx]

        if is_composite:
            cause = _classify_composite_target(
                target=feature_id,
                short_deps=short_deps,
                feast_deps=feast_deps,
                canonical_id=cid,
                short_row=short_row,
                joined_row=joined_row,
                final_row=final_row,
                present_ids=snaps.present_canonical_ids,
                raw_by_cid=raw_by_cid,
            )
        elif is_short_only:
            cause = ROOT_SHORT_TERM if _is_null(short_row.get(feature_id)) else ROOT_OK
        elif is_feast_only:
            cause = _classify_feast_dep(
                canonical_id=cid,
                dep=feature_id,
                present_ids=snaps.present_canonical_ids,
                raw_by_cid=raw_by_cid,
                joined_row=joined_row,
            )
            if cause == ROOT_OK and _is_null(final_row.get(feature_id)):
                cause = ROOT_FEAST_VALUE
        else:
            cause = ROOT_OK
            if _is_null(final_row.get(feature_id)):
                cause = ROOT_SHORT_TERM if _is_null(short_row.get(feature_id)) else ROOT_FEAST_VALUE

        counts[cause] += 1
        if cause != ROOT_OK and len(examples) < max_examples:
            examples.append(
                {
                    "bet_id": final_row.get("bet_id"),
                    "canonical_id": cid or None,
                    "root_cause": cause,
                    "short_deps": {d: short_row.get(d) for d in short_deps},
                    "feast_deps": {d: joined_row.get(d) for d in feast_deps or (feature_id,)},
                }
            )

    return {
        "feature_id": feature_id,
        "supplier": (
            "composite"
            if is_composite
            else "short_term_pit_builder"
            if is_short_only
            else "feast_online_mid"
        ),
        "n_rows_audited": int(sum(counts.values())),
        "n_null_at_final": n_null_final,
        "root_cause_counts": dict(counts),
        "runtime_inputs": {
            "short_term_pit_builder": list(short_deps),
            "feast_online_mid": list(feast_deps),
        },
        "examples": examples,
    }


def run_supplier_root_cause_audit(
    *,
    bundle_dir: Path,
    prediction_log: Path | None = None,
    max_bets: int | None = None,
    lookback_hours: float = 6.0,
    batch_size: int = 500,
    only_null_features: bool = True,
    max_examples_per_feature: int = 20,
) -> dict[str, Any]:
    """Execute full provenance audit and return JSON-serializable report."""
    bundle_root = Path(bundle_dir).resolve()
    rel = _load_bundle_rel(bundle_root)
    _load_dotenv(bundle_root)
    cfg = apply_hightier_serving_environ_overrides(_serving_config_for_bundle(bundle_root, rel))
    set_hightier_serving_deploy_override(cfg)

    model_dir = bundle_root / rel.get("model_bundle_dir", "models")
    mapping = bundle_root / rel.get("canonical_mapping_parquet", "mapping/canonical_player_mapping.parquet")
    feast_repo = cfg.scorer_feast_repo_path
    if feast_repo is None or not Path(feast_repo).is_dir():
        raise FileNotFoundError(f"feast_repo missing under bundle: {feast_repo}")
    if not mapping.is_file():
        raise FileNotFoundError(f"canonical mapping missing: {mapping}")

    allowlist_path = cfg.adt_allowed_players_parquet
    if allowlist_path is None or not Path(allowlist_path).is_file():
        raise FileNotFoundError(f"adt allowlist parquet missing: {allowlist_path}")
    allowlist_ids = frozenset(load_adt_allowlist_ids(Path(allowlist_path)))

    bundle = load_hightier_model_bundle(bundle_dir=model_dir)
    snap = load_frozen_registry_for_bundle(model_dir)
    plan = build_scorer_supplier_plan(snap, bundle.feature_columns)
    registry_by_id = {r.feature_id: r for r in snap.rows}

    bets = _load_audit_bets(
        cfg=cfg,
        allowlist_ids=allowlist_ids,
        prediction_log=prediction_log,
        max_bets=max_bets,
        lookback_hours=lookback_hours,
    )
    pool = _build_scoring_pool(bets, cfg=cfg)
    pool = attach_canonical_id(pool, mapping_parquet=mapping)

    snaps = _run_supplier_pipeline(
        bets,
        pool,
        mapping_parquet=mapping,
        plan=plan,
        feast_repo=Path(feast_repo),
        batch_size=batch_size,
    )

    feats = _features_to_audit(plan, bundle.feature_columns)
    per_feature: list[dict[str, Any]] = []
    global_counts: Counter[str] = Counter()
    for feat in feats:
        block = _audit_feature_column(
            feat,
            snaps,
            plan=plan,
            registry_by_id=registry_by_id,
            only_null_rows=only_null_features,
            max_examples=max_examples_per_feature,
        )
        per_feature.append(block)
        for cause, n in block["root_cause_counts"].items():
            if cause != ROOT_OK:
                global_counts[cause] += int(n)

    return {
        "generated_at": datetime.now(ZoneInfo(HK_TZ)).isoformat(),
        "bundle_dir": str(bundle_root),
        "model_version": bundle.model_version,
        "n_bets_diagnosed": len(bets),
        "n_pool_rows": len(pool),
        "bet_source": "prediction_log" if prediction_log is not None else "clickhouse_incremental",
        "supplier_plan": {
            "short_term_cols": list(plan.short_term_cols),
            "feast_mid_cols": list(plan.feast_mid_cols),
            "mid_composite_cols": list(plan.mid_composite_cols),
        },
        "config": {
            "hot_feature_pool_lookback_hours": int(cfg.hot_feature_pool_lookback_hours),
            "hightier_scorer_pool_player_fanout_cap": int(cfg.hightier_scorer_pool_player_fanout_cap),
        },
        "global_root_cause_counts": dict(global_counts),
        "per_feature": per_feature,
        "methodology": (
            "Deterministic replay of scorer stages: short_term PIT -> Feast lookup "
            "-> join_feast_lookup -> composite. Each null row gets one primary cause "
            "via fixed precedence (short deps, then feast entity/value/join, then composite)."
        ),
    }


def run_audit(argv: list[str] | None = None) -> int:
    """CLI entry: print JSON report and optional exit code on critical null rates."""
    pr = argparse.ArgumentParser(
        description="Deterministic supplier root-cause audit (short-term vs Feast vs join)",
    )
    pr.add_argument("--bundle-dir", type=Path, required=True, help="deploy bundle root")
    pr.add_argument("--prediction-log", type=Path, default=None, help="exported prediction_log CSV")
    pr.add_argument("--output-json", type=Path, default=None, help="write machine-readable report")
    pr.add_argument("--max-bets", type=int, default=None, help="cap diagnosed bets")
    pr.add_argument("--lookback-hours", type=float, default=6.0, help="CH incremental lookback")
    pr.add_argument("--batch-size", type=int, default=500, help="Feast lookup batch size")
    pr.add_argument(
        "--audit-all-rows",
        action="store_true",
        help="classify every row, not only rows where the feature is null at final",
    )
    pr.add_argument("--max-examples", type=int, default=20, help="examples per feature per cause")
    args = pr.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    report = run_supplier_root_cause_audit(
        bundle_dir=args.bundle_dir,
        prediction_log=args.prediction_log,
        max_bets=args.max_bets,
        lookback_hours=float(args.lookback_hours),
        batch_size=int(args.batch_size),
        only_null_features=not bool(args.audit_all_rows),
        max_examples_per_feature=int(args.max_examples),
    )
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.output_json is not None:
        out = Path(args.output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        logger.info("[root-cause] wrote %s", out)

    critical = sum(
        int(v)
        for v in report.get("global_root_cause_counts", {}).values()
    )
    return 1 if critical > 0 else 0


if __name__ == "__main__":
    raise SystemExit(run_audit())
