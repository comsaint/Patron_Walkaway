"""Feature Quality Gate (FQG v0): L1/L2 checks before Gate 1 training.

Produces ``feature_quality_report.json``, ``feature_allowlist.json``, and
``feature_blocklist.json`` per WORKING_PLAN §1.5.

Uses reproducible stratified-ish subsampling via PyArrow to keep RAM bounded.
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig, FeatureQualityGateConfig

logger = logging.getLogger(__name__)

_m5 = importlib.import_module("trainer_hightier.05_lgbm_train")

LABEL_COLUMN: str = _m5.LABEL_COLUMN
CAT_COLUMNS_FROZEN: frozenset[str] = _m5.CAT_COLUMNS

_SPLIT_NAMES: tuple[str, ...] = ("train", "val", "test")


def _parquet_cols(path: Path) -> frozenset[str]:
    pf = pq.ParquetFile(Path(path))
    return frozenset(pf.schema_arrow.names)


def _sample_parquet_to_dataframe(
    path: Path,
    columns: Sequence[str],
    *,
    seed: int,
    max_rows: int,
) -> pd.DataFrame:
    """Load up to ``max_rows`` reproducible pseudo-random rows and selected columns."""

    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"FQG: split parquet missing: {p}")
    table = pq.read_table(p, columns=list(columns))
    n = table.num_rows
    if n == 0:
        return pd.DataFrame({c: [] for c in columns})
    rng = np.random.default_rng(seed)
    if n <= max_rows:
        idx = np.arange(n, dtype=np.int64)
    else:
        idx = np.sort(rng.choice(n, size=max_rows, replace=False))
    sub = table.take(idx)
    return sub.to_pandas()


def _leak_heuristic_block(col: str, cfg: FeatureQualityGateConfig) -> bool:
    lc = col.lower()
    return any(sub in lc for sub in cfg.leakage_column_substrings)


def _infer_column_kind(series: pd.Series, cfg: FeatureQualityGateConfig) -> str:
    name = series.name or ""
    if name in CAT_COLUMNS_FROZEN:
        return "categorical_named"
    n_non_null = int(series.notna().sum())
    if n_non_null <= 1:
        return "all_missing_or_single"
    n_unique_val = series.nunique(dropna=True)
    if n_unique_val <= cfg.low_cardinality_categorical_cutoff:
        try:
            _ = pd.to_numeric(series.dropna(), errors="raise")  # type: ignore[func-returns-value]
        except (ValueError, TypeError):
            return "categorical"
        if pd.api.types.is_numeric_dtype(series):
            return "numeric_coerced"
        return "categorical"
    if pd.api.types.is_numeric_dtype(series):
        return "numeric"
    return "mixed_or_string_numeric"


def _top1_fraction(x: pd.Series) -> float:
    vc = x.value_counts(dropna=False)
    if vc.shape[0] == 0:
        return 1.0
    return float(vc.iloc[0] / vc.sum())


def _top1_fraction_non_null(x: pd.Series) -> float:
    """Top-1 share among **non-null** rows (for Gate0 constant; avoids all-NA → false constant)."""

    x2 = x.dropna()
    if x2.shape[0] == 0:
        return 0.0
    return float(_top1_fraction(x2))


def _missing_fraction(x: pd.Series) -> float:
    """Fraction rows that are NA or blank string/object."""

    if pd.api.types.is_numeric_dtype(x) or hasattr(x.dtype, "categories"):
        return float(pd.isna(x).mean())
    s = x.astype("string") if hasattr(x.astype, "__call__") else x.astype(str)
    nn = ~(pd.isna(s) | (s.astype(str).str.strip() == ""))
    return float(1.0 - nn.astype(float).mean())


def _numeric_stats_for_warnings(x: pd.Series) -> dict[str, float | None]:
    v = pd.to_numeric(x, errors="coerce")
    arr = v.to_numpy(dtype=np.float64, copy=False)
    finite = np.isfinite(arr)
    if not finite.any():
        return {"inf_frac": float(np.isinf(arr).mean()), "p50_abs": None, "p99_abs": None}
    abs_f = np.abs(arr[finite])
    try:
        p50 = float(np.quantile(abs_f, 0.50))
        p99 = float(np.quantile(abs_f, 0.99))
        return {"inf_frac": float(np.isinf(arr).mean()), "p50_abs": p50, "p99_abs": p99}
    except ValueError:
        return {"inf_frac": float(np.isinf(arr).mean()), "p50_abs": None, "p99_abs": None}


def _collect_l1_issues(
    col: str,
    frames: dict[str, pd.DataFrame],
    cfg: FeatureQualityGateConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (checks, diagnostics) where each check dict has severity 'BLOCK'|'WARN'."""

    issues: list[dict[str, Any]] = []
    diag: dict[str, Any] = {"per_split": {}}
    leak = _leak_heuristic_block(col, cfg)
    if leak:
        issues.append({"code": "leak_keyword_substring", "severity": "BLOCK", "detail": col})

    kinds: dict[str, str] = {}
    miss: dict[str, float] = {}
    for split in _SPLIT_NAMES:
        s = frames[split][col]
        kinds[split] = _infer_column_kind(s.rename(col), cfg)
        miss[split] = _missing_fraction(s)
        diag["per_split"][split] = {"kind": kinds[split], "missing_frac": miss[split]}
    uniq_kinds_non_empty = [kinds[s] for s in _SPLIT_NAMES if frames[s][col].notna().any()]
    base_kinds = sorted(set(k for k in uniq_kinds_non_empty if k))
    has_num = any(k in {"numeric", "numeric_coerced"} for k in base_kinds)
    has_cat = any(k in {"categorical", "categorical_named"} for k in base_kinds)
    incompatible = has_num and has_cat
    if incompatible:
        cross = {(s, kinds[s]) for s in _SPLIT_NAMES}
        issues.append({"code": "schema_kind_mismatch_cross_split", "severity": "BLOCK", "detail": str(cross)})

    misses = list(miss.values())
    diag["missing_fracs_all_splits"] = miss
    for m in misses:
        if m > cfg.missing_rate_block:
            issues.append({"code": "l1_missing_rate_block", "severity": "BLOCK", "detail": max(misses)})
    for m in misses:
        if cfg.missing_rate_warn_lo <= m <= cfg.missing_rate_block:
            issues.append({"code": "l1_missing_rate_warn_band", "severity": "WARN", "detail": m})
    rng_m = float(max(misses) - min(misses)) if misses else 0.0
    if rng_m > cfg.missing_rate_split_diff_warn:
        issues.append({"code": "l1_missing_rate_split_diff_warn", "severity": "WARN", "detail": rng_m})

    nunique_max = max((int(frames[s][col].nunique(dropna=True)) for s in _SPLIT_NAMES), default=0)
    any_nonnull = any(bool(frames[s][col].notna().any()) for s in _SPLIT_NAMES)
    if nunique_max == 1 and any_nonnull and frames["train"][col].shape[0] > 0:
        soft_const = col in cfg.l1_constant_unique_block_downgrade_to_warn_columns
        issues.append(
            {
                "code": "l1_constant_unique_soft_warn" if soft_const else "l1_constant_unique",
                "severity": "WARN" if soft_const else "BLOCK",
                "detail": nunique_max,
            },
        )

    top1_mx = float(max((_top1_fraction(frames[s][col]) for s in _SPLIT_NAMES), default=1.0))
    if top1_mx >= cfg.near_constant_top1_warn:
        issues.append({"code": "near_constant_top1", "severity": "WARN", "detail": top1_mx})

    for split in _SPLIT_NAMES:
        s = frames[split][col]
        k = kinds[split]
        if k in {"numeric", "numeric_coerced", "mixed_or_string_numeric"}:
            stats = _numeric_stats_for_warnings(s)
            diag["per_split"][split]["numeric"] = stats
            if stats.get("inf_frac", 0) and stats["inf_frac"] > 0:  # type: ignore[operator]
                issues.append(
                    {"code": "numeric_inf_frac_block", "severity": "BLOCK", "detail": f"{split}={stats['inf_frac']}"},
                )

    unseen_ok = {"categorical", "categorical_named", "mixed_or_string_numeric"}
    if kinds["train"] in unseen_ok:
        ts = set(pd.Series(frames["train"][col].dropna().astype(str).unique()))
        for tst in ("val", "test"):
            vs = pd.Series(frames[tst][col].dropna().astype(str)).value_counts(dropna=False)
            if vs.shape[0] == 0:
                continue
            is_seen = np.asarray(vs.index.isin(ts), dtype=bool)
            counts = vs.to_numpy(dtype=np.float64, copy=False)
            total = float(counts.sum())
            unseen_frac = float(np.sum(counts * (~is_seen)) / total) if total > 0.0 else 0.0
            diag["per_split"][tst]["categorical_unseen_frac"] = unseen_frac
            if unseen_frac > cfg.categorical_unseen_frac_warn:
                issues.append({"code": "categorical_unseen_frac_warn", "severity": "WARN", "detail": f"{tst}={unseen_frac}"})

    for split in _SPLIT_NAMES:
        s = frames[split][col]
        k = kinds[split]
        if k in {"numeric", "numeric_coerced"}:
            stats = diag["per_split"][split].get("numeric") or _numeric_stats_for_warnings(s)
            p50_abs = stats.get("p50_abs")
            p99_abs = stats.get("p99_abs")
            if (
                isinstance(p99_abs, (int, float))
                and isinstance(p50_abs, (int, float))
                and stats.get("inf_frac", 0) == 0
            ):
                denom = float(max(abs(p50_abs), cfg.numeric_long_tail_eps))
                ratio = abs(float(p99_abs)) / denom
                if ratio > cfg.numeric_long_tail_warn:
                    issues.append(
                        {"code": "numeric_long_tail_warn", "severity": "WARN", "detail": f"{split}={ratio:.4g}"},
                    )
    return issues, diag


def _overlay_gate0(
    issues: list[dict[str, Any]],
    *,
    feature: str,
    illegal_max: dict[str, float],
    top1_mx: dict[str, float],
    miss_mx: dict[str, float],
    cfg: FeatureQualityGateConfig,
) -> None:
    """Extend ``issues`` in-place with Gate 0 column rules."""

    mx_illegal = float(max(illegal_max.values()) if illegal_max else 0.0)
    if mx_illegal > cfg.gate0_illegal_max_frac:
        issues.append({"code": "gate0_illegal_max_frac_block", "severity": "BLOCK", "detail": mx_illegal})

    mx_top1 = float(max(top1_mx.values()) if top1_mx else 0.0)
    if mx_top1 >= cfg.gate0_constant_top1_block:
        soft_top1 = feature in cfg.l1_constant_unique_block_downgrade_to_warn_columns
        issues.append(
            {
                "code": "gate0_constant_top1_soft_warn" if soft_top1 else "gate0_constant_top1_block",
                "severity": "WARN" if soft_top1 else "BLOCK",
                "detail": mx_top1,
            },
        )

    mx_miss = float(max(miss_mx.values()) if miss_mx else 0.0)
    if mx_miss >= cfg.gate0_missing_max_frac:
        issues.append({"code": "gate0_missing_max_frac_block", "severity": "BLOCK", "detail": mx_miss})


def _psi_numeric(train: np.ndarray, other: np.ndarray, *, n_bins: int, eps: float) -> float:
    finite_t = train[np.isfinite(train)]
    finite_o = other[np.isfinite(other)]
    if finite_t.size == 0 or finite_o.size == 0:
        return 0.0
    edges = np.unique(np.percentile(finite_t, np.linspace(0, 100, n_bins + 1)))
    if edges.size <= 2:
        return 0.0
    ht, _ = np.histogram(finite_t, bins=edges)
    ho, _ = np.histogram(finite_o, bins=edges)
    pt = ht.astype(np.float64) / max(ht.sum(), 1)
    po = ho.astype(np.float64) / max(ho.sum(), 1)
    pt_s = pt + eps
    po_s = po + eps
    return float(np.sum((po - pt) * np.log(po_s / pt_s)))


def _psi_categorical(train: pd.Series, other: pd.Series, *, eps: float) -> float:
    vc_t = train.dropna().astype(str).value_counts(normalize=True)
    vc_o = other.dropna().astype(str).value_counts(normalize=True)
    all_cats = sorted(set(vc_t.index.astype(str)).union(set(vc_o.index.astype(str))))
    if len(all_cats) == 0:
        return 0.0
    pt = np.array([vc_t.get(c, 0.0) for c in all_cats], dtype=np.float64)
    po = np.array([vc_o.get(c, 0.0) for c in all_cats], dtype=np.float64)
    pt_s = pt + eps
    po_s = po + eps
    return float(np.sum((po - pt) * np.log(po_s / pt_s)))


def _gate0_illegal_frac_numeric(s: pd.Series) -> float:
    """Fraction of rows with non-finite numeric values (Inf only; NaN treated as missing elsewhere)."""

    v = pd.to_numeric(s, errors="coerce")
    arr = v.to_numpy(dtype=np.float64, copy=False)
    if arr.size == 0:
        return 0.0
    return float(np.isinf(arr).mean())


def _corr_nan_label(is_missing: np.ndarray, y_lab: np.ndarray) -> float:
    """Point-biserial corr between missingness indicator and numeric label."""

    if y_lab.size < 3 or is_missing.size != y_lab.size:
        return float("nan")
    y_arr = y_lab.astype(np.float64, copy=False)
    ok = np.isfinite(y_arr)
    if int(ok.sum()) < 3:
        return float("nan")
    miss_f = is_missing.astype(np.float64)[ok]
    y_f = y_arr[ok]
    if np.std(miss_f) <= 1e-12 or np.std(y_f) <= 1e-12:
        return float("nan")
    xm = np.corrcoef(miss_f, y_f)
    try:
        return float(xm[0, 1])
    except (IndexError, TypeError):
        return float("nan")


def _flip_rate_monthly_uplift(
    x: pd.Series,
    labs: pd.Series,
    gd: pd.Series,
    *,
    rng: np.random.Generator,
) -> tuple[float | None, int]:
    """Crude heuristic: uplift sign flips vs global direction across months with ``min_rows``."""

    _ = rng
    gd_dt = pd.to_datetime(gd, errors="coerce")
    m = gd_dt.dt.to_period("M")
    xv = pd.to_numeric(x, errors="coerce")
    y = pd.to_numeric(labs, errors="coerce")
    ok = xv.notna() & y.notna() & m.notna()
    if int(ok.sum()) < 100:
        return None, int(m.dropna().nunique())
    med = float(xv[ok].median())
    x_ok = xv[ok]
    gb = x_ok > med if x_ok.nunique(dropna=True) >= 5 else x_ok >= med
    ym = np.asarray(y[ok], dtype=np.float64)
    pos_m = 0.0
    if bool(gb.any()) and bool((~gb).any()):
        pos_m = float(ym[gb].mean() - ym[~np.asarray(gb, dtype=bool)].mean())
    if not np.isfinite(pos_m):
        pos_m = 0.0
    flipped: list[Any] = []
    uniq_m = sorted(m[ok].drop_duplicates().tolist())
    n_ok = 0
    min_rows = max(256, int(ok.sum()) // 100)
    for period in uniq_m:
        ix = ok & (m == period)
        if int(ix.sum()) < min_rows:
            continue
        xi = xv[ix]
        yi = y[ix].to_numpy(dtype=np.float64, copy=False)
        mdi = float(xi.median())
        bini = xi > mdi if xi.nunique(dropna=True) >= 5 else xi >= mdi
        bi = np.asarray(bini, dtype=bool)
        if not bi.any() or bi.all():
            continue
        yi_arr = yi.astype(np.float64, copy=False)
        pos_i = float(yi_arr[bi].mean() - yi_arr[~bi].mean())
        n_ok += 1
        if np.isfinite(pos_i) and np.isfinite(pos_m) and abs(pos_i) > 1e-8 and abs(pos_m) > 1e-8:
            if np.sign(pos_i) != np.sign(pos_m):
                flipped.append(period)
    if n_ok <= 2:
        return None, int(len(uniq_m))
    return float(len(flipped) / n_ok), n_ok


def _month_missing_stability(
    frames: Mapping[str, pd.DataFrame],
    col: str,
    *,
    gd_col: str,
    cfg: FeatureQualityGateConfig,
) -> tuple[bool, dict[str, Any]]:
    pooled_miss = [_missing_fraction(frames[s][col]) for s in ("train", "val", "test")]
    m0 = float(np.mean(pooled_miss)) if pooled_miss else 0.0
    if m0 <= 1e-8:
        return False, {}
    months_bad = 0
    months_tot = 0
    gd_tr = pd.to_datetime(frames["train"][gd_col], errors="coerce")
    m_tr = gd_tr.dt.to_period("M")
    x_tr_miss = pd.isna(pd.to_numeric(frames["train"][col], errors="coerce")).astype(np.float64).to_numpy()
    m_tr_np = m_tr.to_numpy()
    unique_periods = sorted({p for p in m_tr_np if pd.notna(p)})
    for pm in unique_periods:
        ix = m_tr_np == pm
        if int(ix.sum()) < cfg.min_rows_month_slice:
            continue
        rm = float(np.mean(x_tr_miss[ix]))
        months_tot += 1
        if abs(rm - m0) / max(m0, 1e-9) >= cfg.month_missing_rel_dev_warn:
            months_bad += 1
    if months_tot == 0:
        return False, {"months_checked": months_tot}
    bad = months_bad > months_tot / 2.0
    return bad, {"months_checked": months_tot, "months_over_dev": months_bad, "pooled_missing_mean_split_avg": m0}


def _l2_append_psi_issue(
    chk: list[dict[str, Any]],
    *,
    psi: float,
    name_prefix: str,
    col: str,
    cfg: FeatureQualityGateConfig,
    downgrade: frozenset[str],
) -> None:
    """Append PSI drift issue; BLOCK may become WARN for registry baseline columns."""

    if psi <= cfg.psi_pass_max:
        return
    if psi <= cfg.psi_warn_max:
        chk.append({"code": f"{name_prefix}_warn", "severity": "WARN", "detail": float(psi)})
        return
    if col in downgrade:
        chk.append({"code": f"{name_prefix}_high_drift_soft_warn", "severity": "WARN", "detail": float(psi)})
        return
    chk.append({"code": f"{name_prefix}_block", "severity": "BLOCK", "detail": float(psi)})


def collect_l2_checks(
    *,
    frames: Mapping[str, pd.DataFrame],
    split_names: Sequence[str],
    candidate_columns: Sequence[str],
    gd_col: str,
    rng: np.random.Generator,
    cfg: FeatureQualityGateConfig,
) -> dict[str, list[dict[str, Any]]]:
    """L2 PSI + stability summaries per numeric/categorical-ish column."""

    out: dict[str, list[dict[str, Any]]] = {}
    downgrade = frozenset(cfg.l2_psi_block_downgrade_to_warn_columns)
    lbl_tr = pd.to_numeric(frames["train"][LABEL_COLUMN], errors="coerce")
    lbl_va = pd.to_numeric(frames["val"][LABEL_COLUMN], errors="coerce")
    for col in candidate_columns:
        chk: list[dict[str, Any]] = []
        k_tr = _infer_column_kind(frames["train"][col].rename(col), cfg)
        if k_tr.startswith("mixed") or k_tr == "all_missing_or_single":
            out[col] = chk
            continue
        ks = {_infer_column_kind(frames[s][col].rename(col), cfg) for s in split_names}
        if not ks <= {"numeric", "numeric_coerced", "categorical_named", "categorical", "mixed_or_string_numeric"}:
            chk.append({"code": "l2_skip_incompatible_kind", "severity": "WARN", "detail": str(ks)})
            out[col] = chk
            continue
        xv_tr = pd.to_numeric(frames["train"][col], errors="coerce")
        xv_va = pd.to_numeric(frames["val"][col], errors="coerce")
        xv_te = pd.to_numeric(frames["test"][col], errors="coerce")

        nt = xv_tr.dropna().to_numpy(dtype=np.float64, copy=False)
        frac_non_null = float(xv_tr.notna().mean())
        if nt.size >= 50 and frac_non_null >= 1e-3:
            te_arr = xv_te.dropna().to_numpy(dtype=np.float64, copy=False)
            psi_tv = _psi_numeric(nt, xv_va.dropna().to_numpy(dtype=np.float64), n_bins=min(cfg.psi_bin_count, 10), eps=cfg.psi_eps)
            psi_tt = _psi_numeric(nt, te_arr if te_arr.size else nt[:1], n_bins=min(cfg.psi_bin_count, 10), eps=cfg.psi_eps)
            _l2_append_psi_issue(chk, psi=psi_tv, name_prefix="train_vs_val_psi", col=col, cfg=cfg, downgrade=downgrade)
            _l2_append_psi_issue(chk, psi=psi_tt, name_prefix="train_vs_test_psi", col=col, cfg=cfg, downgrade=downgrade)
        else:
            vc_tr = frames["train"][col]
            if k_tr.startswith("cat") or k_tr.startswith("mixed") or xv_tr.dropna().nunique() <= cfg.low_cardinality_categorical_cutoff:
                pt = vc_tr.astype(str)
                psi_tv_cat = _psi_categorical(pt, frames["val"][col].astype(str), eps=cfg.psi_eps)
                psi_tt_cat = _psi_categorical(pt, frames["test"][col].astype(str), eps=cfg.psi_eps)
                _l2_append_psi_issue(
                    chk,
                    psi=psi_tv_cat,
                    name_prefix="train_vs_val_psi_cat",
                    col=col,
                    cfg=cfg,
                    downgrade=downgrade,
                )
                _l2_append_psi_issue(
                    chk,
                    psi=psi_tt_cat,
                    name_prefix="train_vs_test_psi_cat",
                    col=col,
                    cfg=cfg,
                    downgrade=downgrade,
                )

        bad_m, stab_meta = _month_missing_stability(frames, col, gd_col=gd_col, cfg=cfg)
        if bad_m:
            chk.append({"code": "monthly_missing_volatility_warn", "severity": "WARN", "detail": stab_meta})

        miss_tr = ~xv_tr.notna().to_numpy(dtype=bool)
        miss_va = ~xv_va.notna().to_numpy(dtype=bool)
        ytr = lbl_tr.to_numpy(dtype=np.float64, copy=False)
        yva = lbl_va.to_numpy(dtype=np.float64, copy=False)
        c_tr = _corr_nan_label(miss_tr, ytr)
        c_va = _corr_nan_label(miss_va, yva)
        if np.isfinite(c_tr) and np.isfinite(c_va):
            if abs(c_tr) >= cfg.mnar_corr_abs_floor and abs(c_va) >= cfg.mnar_corr_abs_floor:
                if np.sign(c_tr) != np.sign(c_va):
                    chk.append(
                        {"code": "mnar_corr_sign_flip_warn", "severity": "WARN", "detail": {"train": c_tr, "val": c_va}},
                    )

        flip_r, nm = _flip_rate_monthly_uplift(frames["train"][col], lbl_tr, frames["train"][gd_col], rng=rng)
        if flip_r is not None and flip_r > cfg.uplift_flip_frac_warn:
            chk.append({"code": "uplift_sign_flip_frac_warn", "severity": "WARN", "detail": {"rate": flip_r, "months_ok": nm}})

        out[col] = chk
    return out


def _merge_severity(issues: Sequence[Mapping[str, Any]]) -> str:
    if any(i.get("severity") == "BLOCK" for i in issues):
        return "BLOCK"
    if any(i.get("severity") == "WARN" for i in issues):
        return "WARN"
    return "PASS"


@dataclass(frozen=True)
class FeatureQualityGateResult:
    """Outcome of :func:`run_feature_quality_gate`."""

    fqg_pass: bool
    fqg_status: str
    allowlist: tuple[str, ...]
    report: dict[str, Any]
    blocklist: list[dict[str, Any]]
    warn_pending: tuple[str, ...]


def run_feature_quality_gate(
    *,
    splits_dir: Path,
    candidate_feature_columns: Sequence[str],
    cfg: FeatureQualityGateConfig,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    approved_warn_features: frozenset[str] | None = None,
    gaming_day_column: str = "gaming_day",
) -> FeatureQualityGateResult:
    """Run FQG L1+L2 on split parquet rows; BLOCK implies ``fqg_pass False``.

    Unknown columns in parquet are recorded as BLOCK (column missing). ``duckdb_runtime`` is unused
    (reserved for parity with callers); sampling uses PyArrow.
    """

    _ = duckdb_runtime
    approvals = approved_warn_features or frozenset()
    autoapprove = frozenset(cfg.warn_autoapprove_columns)
    splits = {sn: splits_dir / f"{sn}.parquet" for sn in _SPLIT_NAMES}
    for sid, spa in splits.items():
        if not spa.is_file():
            raise FileNotFoundError(f"FQG expects {spa} ({sid})")

    req_cols_meta = tuple(dict.fromkeys((gaming_day_column, LABEL_COLUMN)))
    parquet_cols_union: set[str] = set()
    for spa in splits.values():
        parquet_cols_union |= set(_parquet_cols(spa))

    if gaming_day_column not in parquet_cols_union:
        raise ValueError(f"FQG requires {gaming_day_column!r} in split parquets.")
    if LABEL_COLUMN not in parquet_cols_union:
        raise ValueError(f"FQG requires {LABEL_COLUMN!r} in split parquets.")

    frames: dict[str, pd.DataFrame] = {}
    seed = cfg.random_seed
    rng_l2 = np.random.default_rng(seed + 13)
    col_report: dict[str, Any] = {}
    warn_pending: list[str] = []

    rng_off = 0
    for idx_sn, sn in enumerate(_SPLIT_NAMES):
        need = list(dict.fromkeys(list(candidate_feature_columns) + list(req_cols_meta)))
        avail = tuple(c for c in need if c in parquet_cols_union)
        rng_off += 7
        frames[sn] = _sample_parquet_to_dataframe(
            splits[sn],
            avail,
            seed=seed + rng_off + idx_sn,
            max_rows=cfg.max_rows_per_split,
        )

    gate_meta = {
        "fqg_version": cfg.fqg_version,
        "splits_dir": str(Path(splits_dir).resolve()),
        "sample_policy": {
            "max_rows_per_split": cfg.max_rows_per_split,
            "random_seed": cfg.random_seed,
        },
        "meta_columns_loaded": req_cols_meta,
    }

    for col in candidate_feature_columns:
        if col not in parquet_cols_union:
            col_report[col] = {
                "status": "BLOCK",
                "issues": [{"severity": "BLOCK", "code": "missing_column_parquet", "detail": ""}],
                "diagnostics": {},
            }
            continue

        fc = {
            split: pd.DataFrame({col: pd.Series(frames[split].loc[:, col].values, dtype=frames[split][col].dtype)}).reset_index(
                drop=True,
            )
            for split in _SPLIT_NAMES
        }
        l1_issues, diag = _collect_l1_issues(col, fc, cfg)

        illegal_mx: dict[str, float] = {}
        top1_mx: dict[str, float] = {}
        miss_mx = {s: float(diag["missing_fracs_all_splits"].get(s, 1.0)) for s in _SPLIT_NAMES}
        for split in _SPLIT_NAMES:
            s = fc[split][col]
            k = diag["per_split"][split]["kind"]
            if k in {"numeric", "numeric_coerced"}:
                illegal_mx[split] = float(_gate0_illegal_frac_numeric(s))
            else:
                illegal_mx[split] = 0.0
            top1_mx[split] = float(_top1_fraction_non_null(s))

        _overlay_gate0(l1_issues, feature=col, illegal_max=illegal_mx, top1_mx=top1_mx, miss_mx=miss_mx, cfg=cfg)

        sev_before_l2 = _merge_severity(l1_issues)

        fc_full_for_l2 = {
            split: pd.DataFrame(
                {
                    gaming_day_column: frames[split][gaming_day_column].values,
                    LABEL_COLUMN: frames[split][LABEL_COLUMN].values,
                    col: frames[split][col].values,
                },
            ).reset_index(drop=True)
            for split in _SPLIT_NAMES
        }

        l2_issues: list[dict[str, Any]] = []
        if sev_before_l2 != "BLOCK":
            subset = collect_l2_checks(
                frames=fc_full_for_l2,
                split_names=("train", "val", "test"),
                candidate_columns=(col,),
                gd_col=gaming_day_column,
                rng=rng_l2,
                cfg=cfg,
            )
            l2_issues.extend(subset.get(col, []))

        all_issues = list(l1_issues) + list(l2_issues)

        severity = _merge_severity(all_issues)
        detail: dict[str, Any] = {
            "l1_issues": l1_issues,
            "l2_issues": l2_issues,
            "diag": diag,
            "illegal_max_frac": illegal_mx,
            "gate0_miss_max": miss_mx,
            "illegal_max_frac_note": "numeric Inf fraction only; NaNs counted under gate0_miss / L1 missing",
        }
        col_report[col] = {"status": severity, "issues": all_issues, "diagnostics": detail}

    blocklist_entries: list[dict[str, Any]] = []
    allow: list[str] = []
    for col, blob in col_report.items():
        st = str(blob["status"])
        if st == "BLOCK":
            rc_code = "blocked"
            rc_detail: Any = None
            for iss in blob["issues"]:
                if iss.get("severity") == "BLOCK":
                    rc_code = str(iss.get("code", rc_code))
                    rc_detail = iss.get("detail")
                    break
            blocklist_entries.append({"feature": col, "reason_code": rc_code, "detail": rc_detail})
            continue

        if st == "WARN":
            if col in autoapprove or col in approvals:
                allow.append(col)
            else:
                warn_pending.append(col)
                continue

        allow.append(col)

    fqg_hard_fail = any(col_report[c]["status"] == "BLOCK" for c in candidate_feature_columns)
    gate_status = "pass" if not fqg_hard_fail else "fail"

    report = {
        "fqg_gate": gate_status,
        **gate_meta,
        "warn_pending_features": tuple(sorted(warn_pending)),
        "approve_auto_baseline_warn": tuple(sorted(autoapprove)),
        "features": col_report,
    }

    warn_pending_sorted = tuple(sorted(warn_pending))
    return FeatureQualityGateResult(
        fqg_pass=not fqg_hard_fail,
        fqg_status="pass" if not fqg_hard_fail else "fail",
        allowlist=tuple(sorted(allow)),
        report=report,
        blocklist=blocklist_entries,
        warn_pending=warn_pending_sorted,
    )


def write_fqg_json_bundle(
    dest_dir: Path,
    *,
    result: FeatureQualityGateResult,
) -> tuple[Path, Path, Path]:
    """Persist the three-contract JSON artefacts under ``dest_dir``."""

    dest_dir.mkdir(parents=True, exist_ok=True)
    pq_path = dest_dir / "feature_quality_report.json"
    al_path = dest_dir / "feature_allowlist.json"
    bl_path = dest_dir / "feature_blocklist.json"

    pq_path.write_text(json.dumps(result.report, indent=2, default=str), encoding="utf-8")
    al_blob = {
        "fqg_version": str(result.report.get("fqg_version", "")),
        "features": list(result.allowlist),
        "blocked_count": len(result.blocklist),
    }
    al_path.write_text(json.dumps(al_blob, indent=2, default=str), encoding="utf-8")
    bl_path.write_text(json.dumps({"blocked": result.blocklist}, indent=2, default=str), encoding="utf-8")
    return pq_path, al_path, bl_path


def parse_warn_approvals_yaml(path: Path) -> frozenset[str]:
    """Load ``approved_warn_features: [...]`` from a YAML mapping."""

    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Warn approvals YAML must be a mapping, got {type(raw)} from {path}")
    aw = raw.get("approved_warn_features", [])
    if not isinstance(aw, list):
        raise ValueError("approved_warn_features must be a list of strings")
    return frozenset(str(x) for x in aw)
