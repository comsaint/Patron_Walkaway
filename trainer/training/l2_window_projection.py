"""Subset-window projection for L2 auto-bundle reuse (source-invariant hit + view miss).

Uses DuckDB filtered COPY (not full pandas loads) to narrow monolithic split Parquets,
then rebuilds ``split_day_manifest`` day shards. Fail-closed when semantics cannot be
preserved (embedded label assets, schema mismatch, empty splits).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import duckdb
import pandas as pd

from trainer.training.l2_bundle_materialize import (
    L2_BUNDLE_CACHE_KEY_FILE,
    TEST_SPLIT_NAME,
    TRAIN_SPLIT_NAME,
    VALID_SPLIT_NAME,
    stable_cache_key_fingerprint,
)
from trainer.training.l2_day_shard import min_max_day_from_manifest_rows, shard_split_parquet_by_day
from trainer.training.l2_reuse_keys import (
    normalize_auto_l2_cache_key,
    source_invariant_match,
    validate_expected_l2_cache_key,
    window_view_match,
)

logger = logging.getLogger(__name__)


def _day_filter_sql(parquet_path: Path) -> str:
    from trainer.training.l2_day_shard import _duckdb_escape_path, _day_column_sql, _normalized_column_map

    sp = _duckdb_escape_path(Path(parquet_path).resolve())
    con = duckdb.connect(":memory:")
    try:
        cols = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{sp}')").fetchall()
        source_columns = [str(row[0]) for row in cols]
        colmap = _normalized_column_map(source_columns)
        return _day_column_sql(colmap, "src")
    finally:
        con.close()


def _filter_split_file(
    *,
    src_parquet: Path,
    dest_parquet: Path,
    start_day: str,
    end_day: str,
) -> int:
    """Write rows whose calendar day is in ``[start_day, end_day)``; return row count."""
    sp = str(src_parquet.resolve()).replace("'", "''")
    dp = str(dest_parquet.resolve()).replace("'", "''")
    day_expr = _day_filter_sql(src_parquet)
    con = duckdb.connect(":memory:")
    try:
        qcount = (
            f"SELECT count(*) FROM read_parquet('{sp}') AS src WHERE {day_expr} IS NOT NULL "
            f"AND CAST({day_expr} AS DATE) >= DATE '{start_day}' "
            f"AND CAST({day_expr} AS DATE) < DATE '{end_day}'"
        )
        cnt = int(con.execute(qcount).fetchone()[0] or 0)
        con.execute(
            f"COPY ( SELECT * FROM read_parquet('{sp}') AS src WHERE {day_expr} IS NOT NULL "
            f"AND CAST({day_expr} AS DATE) >= DATE '{start_day}' "
            f"AND CAST({day_expr} AS DATE) < DATE '{end_day}' ) TO '{dp}' (FORMAT PARQUET)"
        )
        return cnt
    finally:
        con.close()


def _train_end_iso_from_parquet(train_parquet: Path) -> str:
    sp = str(train_parquet.resolve()).replace("'", "''")
    con = duckdb.connect(":memory:")
    try:
        row = con.execute(
            f"SELECT max(payout_complete_dtm) AS m FROM read_parquet('{sp}')"
        ).fetchone()
        if row is None or row[0] is None:
            return ""
        return pd.Timestamp(row[0]).isoformat()
    finally:
        con.close()


def _non_date_view_fields_match(cached_view: Mapping[str, Any], expected_view: Mapping[str, Any]) -> bool:
    skip = {"window_start_iso", "window_end_iso"}
    ck = {k: v for k, v in dict(cached_view).items() if k not in skip}
    ek = {k: v for k, v in dict(expected_view).items() if k not in skip}
    return ck == ek


def try_project_l2_bundle_window(bundle_dir: Path, expected_key: Mapping[str, Any]) -> Dict[str, Any]:
    """Project an existing schema-v2 bundle to *expected_key* window when safe.

    Returns:
        Dict with at least ``ok: bool``, ``miss_reason`` (optional), and diagnostics keys.
    """
    from trainer.core import _config_training_domain as _tdom

    diagnostics: Dict[str, Any] = {
        "l2_window_projection_attempted": True,
        "l2_window_projection_ok": False,
    }
    if not bool(getattr(_tdom, "L2_WINDOW_PROJECTION_ENABLED", True)):
        diagnostics["l2_window_projection_miss_reason"] = "projection_disabled"
        return {"ok": False, "miss_reason": "projection_disabled", "diagnostics": diagnostics}

    bdir = Path(bundle_dir)
    mf_path = bdir / "l2_training_bundle.json"
    if not mf_path.is_file():
        diagnostics["l2_window_projection_miss_reason"] = "missing_manifest"
        return {"ok": False, "miss_reason": "missing_manifest", "diagnostics": diagnostics}

    try:
        raw_mf = json.loads(mf_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        diagnostics["l2_window_projection_miss_reason"] = "manifest_unreadable"
        return {"ok": False, "miss_reason": "manifest_unreadable", "diagnostics": diagnostics}
    if not isinstance(raw_mf, dict):
        diagnostics["l2_window_projection_miss_reason"] = "manifest_invalid"
        return {"ok": False, "miss_reason": "manifest_invalid", "diagnostics": diagnostics}
    if str(raw_mf.get("schema_version") or "") != "2":
        diagnostics["l2_window_projection_miss_reason"] = "schema_not_v2"
        return {"ok": False, "miss_reason": "schema_not_v2", "diagnostics": diagnostics}
    if raw_mf.get("label_asset"):
        diagnostics["l2_window_projection_miss_reason"] = "bundle_has_embedded_label_asset"
        return {"ok": False, "miss_reason": "bundle_has_embedded_label_asset", "diagnostics": diagnostics}

    sidecar_path = bdir / L2_BUNDLE_CACHE_KEY_FILE
    if not sidecar_path.is_file():
        diagnostics["l2_window_projection_miss_reason"] = "no_cache_sidecar"
        return {"ok": False, "miss_reason": "no_cache_sidecar", "diagnostics": diagnostics}
    try:
        cached_key_raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        diagnostics["l2_window_projection_miss_reason"] = "cache_sidecar_unreadable"
        return {"ok": False, "miss_reason": "cache_sidecar_unreadable", "diagnostics": diagnostics}
    if not isinstance(cached_key_raw, dict):
        diagnostics["l2_window_projection_miss_reason"] = "cache_sidecar_invalid"
        return {"ok": False, "miss_reason": "cache_sidecar_invalid", "diagnostics": diagnostics}

    exp_n = normalize_auto_l2_cache_key(expected_key)
    cache_n = normalize_auto_l2_cache_key(cached_key_raw)
    try:
        _strict = bool(getattr(_tdom, "L2_REUSE_STRICT_KEY_SCHEMA", True))
    except Exception:
        _strict = True
    _ve = validate_expected_l2_cache_key(exp_n, strict=_strict)
    if _ve is not None:
        diagnostics["l2_window_projection_miss_reason"] = "invalid_expected_key"
        diagnostics["l2_window_projection_invalid_key_detail"] = _ve
        return {"ok": False, "miss_reason": "invalid_expected_key", "diagnostics": diagnostics}

    if not source_invariant_match(cache_n, exp_n):
        diagnostics["l2_window_projection_miss_reason"] = "source_invariant_mismatch"
        return {"ok": False, "miss_reason": "source_invariant_mismatch", "diagnostics": diagnostics}
    if window_view_match(cache_n, exp_n):
        diagnostics["l2_window_projection_miss_reason"] = "window_already_matches"
        return {"ok": False, "miss_reason": "window_already_matches", "diagnostics": diagnostics}

    cview = cache_n.get("window_view")
    eview = exp_n.get("window_view")
    if not isinstance(cview, dict) or not isinstance(eview, dict):
        diagnostics["l2_window_projection_miss_reason"] = "window_view_invalid"
        return {"ok": False, "miss_reason": "window_view_invalid", "diagnostics": diagnostics}
    if not _non_date_view_fields_match(cview, eview):
        diagnostics["l2_window_projection_miss_reason"] = "window_view_nondate_mismatch"
        return {"ok": False, "miss_reason": "window_view_nondate_mismatch", "diagnostics": diagnostics}

    old_ws = pd.Timestamp(str(raw_mf.get("window_start") or ""))
    old_we = pd.Timestamp(str(raw_mf.get("window_end") or ""))
    new_ws = pd.Timestamp(str(eview["window_start_iso"]))
    new_we = pd.Timestamp(str(eview["window_end_iso"]))
    old_ws = old_ws.tz_convert("Asia/Hong_Kong").replace(tzinfo=None) if old_ws.tzinfo else old_ws
    old_we = old_we.tz_convert("Asia/Hong_Kong").replace(tzinfo=None) if old_we.tzinfo else old_we
    new_ws = new_ws.tz_convert("Asia/Hong_Kong").replace(tzinfo=None) if new_ws.tzinfo else new_ws
    new_we = new_we.tz_convert("Asia/Hong_Kong").replace(tzinfo=None) if new_we.tzinfo else new_we

    if new_ws < old_ws or new_we > old_we:
        diagnostics["l2_window_projection_miss_reason"] = "new_window_not_subset_of_cached_manifest"
        return {"ok": False, "miss_reason": "new_window_not_subset_of_cached_manifest", "diagnostics": diagnostics}

    start_day = new_ws.strftime("%Y-%m-%d")
    end_day = new_we.strftime("%Y-%m-%d")

    train_p = bdir / TRAIN_SPLIT_NAME
    valid_p = bdir / VALID_SPLIT_NAME
    test_p = bdir / TEST_SPLIT_NAME
    for p in (train_p, valid_p, test_p):
        if not p.is_file():
            diagnostics["l2_window_projection_miss_reason"] = "split_parquet_missing"
            return {"ok": False, "miss_reason": "split_parquet_missing", "diagnostics": diagnostics}

    tmp_train = bdir / ".l2_projection_tmp_train.parquet"
    tmp_valid = bdir / ".l2_projection_tmp_valid.parquet"
    tmp_test = bdir / ".l2_projection_tmp_test.parquet"
    n_tr = n_va = n_te = 0
    try:
        n_tr = _filter_split_file(src_parquet=train_p, dest_parquet=tmp_train, start_day=start_day, end_day=end_day)
        n_va = _filter_split_file(src_parquet=valid_p, dest_parquet=tmp_valid, start_day=start_day, end_day=end_day)
        n_te = _filter_split_file(src_parquet=test_p, dest_parquet=tmp_test, start_day=start_day, end_day=end_day)
        if n_tr < 1 or n_va < 1 or n_te < 1:
            diagnostics["l2_window_projection_miss_reason"] = "empty_split_after_filter"
            diagnostics["l2_window_projection_split_rows"] = {"train": n_tr, "valid": n_va, "test": n_te}
            return {"ok": False, "miss_reason": "empty_split_after_filter", "diagnostics": diagnostics}

        os.replace(tmp_train, train_p)
        os.replace(tmp_valid, valid_p)
        os.replace(tmp_test, test_p)

        dm_train = shard_split_parquet_by_day(bdir, "train", train_p)
        dm_valid = shard_split_parquet_by_day(bdir, "valid", valid_p)
        dm_test = shard_split_parquet_by_day(bdir, "test", test_p)
        tr_lo, tr_hi = min_max_day_from_manifest_rows(dm_train)
        va_lo, va_hi = min_max_day_from_manifest_rows(dm_valid)
        te_lo, te_hi = min_max_day_from_manifest_rows(dm_test)
        split_calendar = {
            "train": {
                "gaming_day_min": tr_lo,
                "gaming_day_max": tr_hi,
                "policy": "row_fraction_step7_then_day_shard",
            },
            "valid": {"gaming_day_min": va_lo, "gaming_day_max": va_hi, "policy": "row_fraction_step7_then_day_shard"},
            "test": {"gaming_day_min": te_lo, "gaming_day_max": te_hi, "policy": "row_fraction_step7_then_day_shard"},
        }
        train_end_iso = _train_end_iso_from_parquet(train_p)
        if not train_end_iso:
            diagnostics["l2_window_projection_miss_reason"] = "train_end_unresolved"
            return {"ok": False, "miss_reason": "train_end_unresolved", "diagnostics": diagnostics}

        sid = str(raw_mf.get("source_snapshot_id") or "unknown")
        fp12 = stable_cache_key_fingerprint(exp_n)
        raw_mf["l2_snapshot_id"] = f"l2auto_{sid}_{fp12}"
        raw_mf["train_end"] = train_end_iso
        raw_mf["window_start"] = str(eview["window_start_iso"])
        raw_mf["window_end"] = str(eview["window_end_iso"])
        raw_mf["split_day_manifest"] = {"train": dm_train, "valid": dm_valid, "test": dm_test}
        raw_mf["split_calendar"] = split_calendar
        mf_path.write_text(json.dumps(raw_mf, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        sidecar_path.write_text(json.dumps(dict(exp_n), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("L2 window projection failed: %s", exc)
        diagnostics["l2_window_projection_miss_reason"] = "projection_exception"
        diagnostics["l2_window_projection_error"] = str(exc)[:500]
        return {"ok": False, "miss_reason": "projection_exception", "diagnostics": diagnostics}
    finally:
        for p in (tmp_train, tmp_valid, tmp_test):
            p.unlink(missing_ok=True)

    diagnostics["l2_window_projection_ok"] = True
    diagnostics["l2_window_projection_miss_reason"] = None
    logger.info(
        "L2 window projection ok bundle=%s window=%s -> %s (train_rows=%d valid=%d test=%d)",
        bdir,
        start_day,
        end_day,
        n_tr,
        n_va,
        n_te,
    )
    return {"ok": True, "miss_reason": None, "diagnostics": diagnostics}
