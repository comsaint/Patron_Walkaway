"""Ad-hoc Phase 1 segment error analysis (W1-B2 quick path).

Purpose:
- Compute by-segment error metrics from either:
  1) ``--backtest-predictions-parquet``; or
  2) ``--prediction-log-db`` + ``--state-db``.
- Enrich eval rows with profile fields required for segmentation:
  ``theo_win_sum_30d``, ``active_days_30d``, ``turnover_sum_30d``,
  ``days_since_first_session``.
- Merge profile sources incrementally (embedded backtest fields -> state DB ->
  ClickHouse fallback -> ``player_profile.parquet`` row-level as-of join on
  ``snapshot_dtm <= scored_at``) to fill missing profile fields.

Row-retention policy:
- Missing canonical profile (no profile row for canonical_id) is dropped.
- Rows with incomplete/non-numeric profile fields or ``active_days_30d <= 0``
  are kept and assigned ``*_unknown`` percentile buckets, instead of being dropped.

Runtime behavior:
- ``--max-rows <= 0`` means uncapped/full-window evaluation.
- Default ``--score-threshold`` is read from ``<--model-bundle-dir>/training_metrics.json``
  as ``rated.threshold_at_recall_0.01`` (apple-to-apple with training); pass
  ``--score-threshold`` only to override.
- ClickHouse profile fallback is on by default; pass ``--no-use-clickhouse-fallback`` to disable.
- For backtest parquet timestamps that are tz-naive, values are normalized as UTC
  before HKT date bucketing to avoid off-by-one-day segment labels.

Output:
- JSON file with summary, notes, and per-segment metrics.
- Console summary (top segments by precision-at-alert/error-rate ordering).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROFILE_FIELDS: tuple[str, ...] = (
    "theo_win_sum_30d",
    "active_days_30d",
    "turnover_sum_30d",
    "days_since_first_session",
)

# Row-level as-of join to player_profile.parquet (avoid OOM on laptop).
PROFILE_PARQUET_ASOF_CHUNK: int = 250_000

_SENTINEL_SCORE_THRESHOLD = object()


def _load_rated_threshold_at_recall_0_01(model_bundle_dir: Path) -> float:
    """Load ``rated.threshold_at_recall_0.01`` from ``training_metrics.json`` under the model bundle."""
    metrics_path = (model_bundle_dir / "training_metrics.json").resolve()
    if not metrics_path.is_file():
        raise SystemExit(
            f"training_metrics.json not found under --model-bundle-dir: {metrics_path}"
        )
    try:
        data = json.loads(metrics_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {metrics_path}: {exc}") from exc
    rated = data.get("rated")
    if not isinstance(rated, dict):
        raise SystemExit(f"training_metrics.json missing dict 'rated': {metrics_path}")
    key = "threshold_at_recall_0.01"
    if key not in rated:
        raise SystemExit(
            f"rated.{key} missing in {metrics_path}; cannot align alert threshold with training."
        )
    try:
        return float(rated[key])
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"rated.{key} is not numeric in {metrics_path}") from exc


@dataclass(frozen=True)
class EvalRow:
    bet_id: str
    canonical_id: str
    scored_at: str
    table_id: str
    is_alert: int
    label: int


def _parse_iso(raw: str) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _eval_date_hkt(raw: str) -> str:
    dt = _parse_iso(raw)
    if dt is None:
        return "UNKNOWN_DATE"
    if dt.tzinfo is None:
        return dt.date().isoformat()
    try:
        from zoneinfo import ZoneInfo

        return dt.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
    except Exception:
        return dt.date().isoformat()


def _normalize_backtest_ts(raw: str) -> str:
    """Normalize backtest timestamp for stable HKT date bucketing.

    Backtest parquet often stores tz-naive timestamps that are effectively UTC.
    To avoid off-by-one-day segment labels, treat naive values as UTC.
    """
    dt = _parse_iso(raw)
    if dt is None:
        return str(raw or "").strip()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _tenure_bucket(v: Any) -> str:
    try:
        d = float(v)
    except (TypeError, ValueError):
        return "UNKNOWN_TENURE"
    if d <= 7:
        return "T0_seg"
    if d <= 30:
        return "T1"
    if d <= 90:
        return "T2"
    return "T3"


def _empirical_decile_labels(vals: list[float], prefix: str) -> list[str]:
    n = len(vals)
    if n == 0:
        return []
    if n == 1:
        return [f"{prefix}_d1"]
    ord_idx = sorted(range(n), key=lambda i: vals[i])
    labels = [""] * n
    for rank, idx in enumerate(ord_idx):
        bucket = int(rank * 10 / n) + 1
        if bucket > 10:
            bucket = 10
        labels[idx] = f"{prefix}_d{bucket}"
    return labels


def _load_eval_rows(
    pred_db: Path,
    state_db: Path,
    *,
    start_ts: str,
    end_ts: str,
    max_rows: int,
    score_threshold: float,
) -> tuple[list[EvalRow], list[str]]:
    notes: list[str] = []
    if not pred_db.is_file():
        return [], ["prediction_log_db_not_found"]
    try:
        with sqlite3.connect(f"file:{pred_db}?mode=ro", uri=True) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(prediction_log)").fetchall()}
            needed = {"bet_id", "canonical_id", "scored_at"}
            if not needed.issubset(cols):
                return [], [f"prediction_log_missing_cols:{','.join(sorted(needed - cols))}"]
            has_table_id = "table_id" in cols
            has_alert = "is_alert" in cols
            has_score = "score" in cols
            rated_filter = " AND is_rated_obs = 1" if "is_rated_obs" in cols else ""
            if not rated_filter:
                notes.append("prediction_log_missing_is_rated_obs")
            sel = "bet_id, canonical_id, scored_at"
            if has_table_id:
                sel += ", table_id"
            if has_alert:
                sel += ", is_alert"
            if has_score:
                sel += ", score"
            if max_rows > 0:
                sql = (
                    f"SELECT {sel} FROM prediction_log WHERE scored_at >= ? AND scored_at < ?"
                    f"{rated_filter} ORDER BY scored_at ASC LIMIT ?"
                )
                rows = conn.execute(sql, (start_ts, end_ts, max_rows + 1)).fetchall()
            else:
                sql = (
                    f"SELECT {sel} FROM prediction_log WHERE scored_at >= ? AND scored_at < ?"
                    f"{rated_filter} ORDER BY scored_at ASC"
                )
                rows = conn.execute(sql, (start_ts, end_ts)).fetchall()
    except sqlite3.Error:
        return [], ["prediction_log_db_unavailable"]

    if max_rows > 0 and len(rows) > max_rows:
        rows = rows[:max_rows]
        notes.append(f"eval_rows_truncated_at:{max_rows}")

    raw_rows: list[dict[str, Any]] = []
    for r in rows:
        i = 0
        bet_id = str(r[i] or "").strip()
        i += 1
        cid = str(r[i] or "").strip()
        i += 1
        scored_at = str(r[i] or "").strip()
        i += 1
        if not bet_id or not cid or not scored_at:
            continue
        table_id = str(r[i] or "").strip() if has_table_id else ""
        i += 1 if has_table_id else 0
        raw_alert = r[i] if has_alert else None
        i += 1 if has_alert else 0
        raw_score = r[i] if has_score else None
        if raw_alert is None:
            try:
                alert = 1 if float(raw_score) >= score_threshold else 0
            except (TypeError, ValueError):
                alert = 0
        else:
            alert = 1 if int(raw_alert) == 1 else 0
        raw_rows.append(
            {
                "bet_id": bet_id,
                "canonical_id": cid,
                "scored_at": scored_at,
                "table_id": table_id or "UNKNOWN_TABLE",
                "is_alert": alert,
            }
        )
    if not raw_rows:
        return [], notes + ["prediction_log_rows_empty_after_validation"]

    labels: dict[str, int] = {}
    if state_db.is_file():
        try:
            with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True) as conn:
                cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(validation_results)").fetchall()
                }
                if {"bet_id", "result"}.issubset(cols):
                    fin = ""
                    if "validated_at" in cols:
                        fin = " AND TRIM(COALESCE(validated_at, '')) != ''"
                    chunk = 800
                    for j in range(0, len(raw_rows), chunk):
                        bids = [x["bet_id"] for x in raw_rows[j : j + chunk]]
                        q = ",".join(["?"] * len(bids))
                        sql = f"SELECT bet_id, result FROM validation_results WHERE bet_id IN ({q}){fin}"
                        for b, y in conn.execute(sql, tuple(bids)).fetchall():
                            try:
                                labels[str(b)] = int(y)
                            except (TypeError, ValueError):
                                continue
                else:
                    notes.append("validation_results_missing_bet_id_or_result")
        except sqlite3.Error:
            notes.append("state_db_unavailable_for_labels")
    else:
        notes.append("state_db_not_found_for_labels")

    out: list[EvalRow] = []
    dropped = 0
    for r in raw_rows:
        y = labels.get(r["bet_id"])
        if y is None:
            dropped += 1
            continue
        out.append(
            EvalRow(
                bet_id=r["bet_id"],
                canonical_id=r["canonical_id"],
                scored_at=r["scored_at"],
                table_id=r["table_id"],
                is_alert=r["is_alert"],
                label=int(y),
            )
        )
    if dropped:
        notes.append(f"unlabeled_rows_dropped:{dropped}")
    return out, notes


def _load_eval_rows_from_backtest_parquet(
    parquet_path: Path,
    *,
    start_ts: str,
    end_ts: str,
    max_rows: int,
    score_threshold: float,
) -> tuple[list[EvalRow], list[str], list[dict[str, Any]]]:
    """Load eval rows directly from backtest predictions parquet.

    Returns:
      (eval_rows, notes, profile_rows)
    """
    notes: list[str] = []
    out: list[EvalRow] = []
    profile_rows: list[dict[str, Any]] = []
    if not parquet_path.is_file():
        return out, ["backtest_predictions_parquet_not_found"], profile_rows
    try:
        import duckdb  # type: ignore
    except Exception:
        return out, ["duckdb_unavailable_for_backtest_predictions"], profile_rows

    start_dt = _parse_iso(start_ts)
    end_dt = _parse_iso(end_ts)
    if start_dt is None or end_dt is None:
        return out, ["backtest_window_ts_unparseable"], profile_rows
    try:
        con = duckdb.connect(":memory:")
        cols = {
            str(r[0])
            for r in con.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)",
                [str(parquet_path)],
            ).fetchall()
        }
        if "canonical_id" not in cols or "label" not in cols:
            con.close()
            return out, ["backtest_predictions_missing_canonical_or_label"], profile_rows
        ts_col = "scored_at" if "scored_at" in cols else ("payout_complete_dtm" if "payout_complete_dtm" in cols else "")
        if not ts_col:
            con.close()
            return out, ["backtest_predictions_missing_time_col"], profile_rows
        has_table_id = "table_id" in cols
        has_alert = "is_alert" in cols
        has_score = "score" in cols
        has_bet_id = "bet_id" in cols
        select_cols = "canonical_id, label, " + ts_col
        if has_table_id:
            select_cols += ", table_id"
        if has_alert:
            select_cols += ", is_alert"
        if has_score:
            select_cols += ", score"
        if has_bet_id:
            select_cols += ", bet_id"
        # pull profile fields if present in backtest output
        pf_present = [c for c in PROFILE_FIELDS if c in cols]
        if pf_present:
            select_cols += ", " + ", ".join(pf_present)
        if max_rows > 0:
            sql = (
                f"SELECT {select_cols} FROM read_parquet(?) "
                f"WHERE {ts_col} >= ? AND {ts_col} < ? "
                f"ORDER BY {ts_col} ASC LIMIT ?"
            )
            rows = con.execute(
                sql,
                [str(parquet_path), start_ts, end_ts, max_rows + 1],
            ).fetchall()
        else:
            sql = (
                f"SELECT {select_cols} FROM read_parquet(?) "
                f"WHERE {ts_col} >= ? AND {ts_col} < ? "
                f"ORDER BY {ts_col} ASC"
            )
            rows = con.execute(
                sql,
                [str(parquet_path), start_ts, end_ts],
            ).fetchall()
        con.close()
    except Exception:
        return out, ["backtest_predictions_query_failed"], profile_rows

    if max_rows > 0 and len(rows) > max_rows:
        rows = rows[:max_rows]
        notes.append(f"eval_rows_truncated_at:{max_rows}")

    for r in rows:
        i = 0
        cid = str(r[i] or "").strip()
        i += 1
        try:
            label = int(r[i])
        except (TypeError, ValueError):
            continue
        i += 1
        ts_raw = str(r[i] or "").strip()
        i += 1
        table_id = str(r[i] or "").strip() if has_table_id else ""
        i += 1 if has_table_id else 0
        raw_alert = r[i] if has_alert else None
        i += 1 if has_alert else 0
        raw_score = r[i] if has_score else None
        i += 1 if has_score else 0
        bet_id = str(r[i] or "").strip() if has_bet_id else ""
        i += 1 if has_bet_id else 0
        if not cid or not ts_raw:
            continue
        ts_raw = _normalize_backtest_ts(ts_raw)
        if raw_alert is None:
            try:
                is_alert = 1 if float(raw_score) >= score_threshold else 0
            except (TypeError, ValueError):
                is_alert = 0
        else:
            try:
                is_alert = 1 if int(raw_alert) == 1 else 0
            except (TypeError, ValueError):
                is_alert = 0
        out.append(
            EvalRow(
                bet_id=bet_id or f"{cid}:{ts_raw}",
                canonical_id=cid,
                scored_at=ts_raw,
                table_id=table_id or "UNKNOWN_TABLE",
                is_alert=is_alert,
                label=label,
            )
        )
        if pf_present:
            prow: dict[str, Any] = {"canonical_id": cid}
            for j, c in enumerate(pf_present):
                prow[c] = r[i + j]
            profile_rows.append(prow)
    return out, notes, profile_rows


def _profiles_from_backtest_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        cid = str(r.get("canonical_id") or "").strip()
        if not cid:
            continue
        cur = out.get(cid)
        if cur is None:
            cur = {}
            out[cid] = cur
        for f in PROFILE_FIELDS:
            v = r.get(f)
            if v is None:
                continue
            cur[f] = v
    return out


def _merge_profiles(base: dict[str, dict[str, Any]], incoming: dict[str, dict[str, Any]]) -> int:
    """Merge profile maps, filling only missing required fields.

    Returns:
      Number of field values newly filled in ``base``.
    """
    filled = 0
    for cid, src in incoming.items():
        if not cid:
            continue
        dst = base.get(cid)
        if dst is None:
            dst = {}
            base[cid] = dst
        for f in PROFILE_FIELDS:
            v = src.get(f)
            if v is None:
                continue
            if dst.get(f) is None:
                dst[f] = v
                filled += 1
    return filled


def _missing_profile_cids(
    profiles: dict[str, dict[str, Any]],
    cids: list[str],
) -> list[str]:
    uniq = sorted({str(c).strip() for c in cids if str(c).strip()})
    out: list[str] = []
    for cid in uniq:
        pr = profiles.get(cid)
        if not isinstance(pr, dict):
            out.append(cid)
            continue
        if any(pr.get(f) is None for f in PROFILE_FIELDS):
            out.append(cid)
    return out


def _as_of_naive_utc_for_profile_join(scored_at: str) -> datetime | None:
    """Interpret ``scored_at`` as an instant and return naive UTC for join to ``snapshot_dtm``."""
    dt = _parse_iso(str(scored_at or "").strip())
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(tzinfo=None)


def _merge_parquet_row_into_cid_profile(
    row_parq: dict[str, Any] | None,
    profiles_by_cid: dict[str, dict[str, Any]],
    cid: str,
) -> dict[str, Any] | None:
    """Prefer non-null PIT parquet fields over the CID-level profile map."""
    out: dict[str, Any] = {}
    base = profiles_by_cid.get(cid)
    if isinstance(base, dict):
        out.update(base)
    if row_parq:
        for f in PROFILE_FIELDS:
            v = row_parq.get(f)
            if v is not None:
                out[f] = v
    return out if out else None


def _load_profiles_parquet_asof_per_row(
    parquet_path: Path,
    eval_rows: list[EvalRow],
    *,
    chunk_size: int = PROFILE_PARQUET_ASOF_CHUNK,
) -> tuple[list[dict[str, Any] | None] | None, list[str]]:
    """Latest ``player_profile`` row per eval row with ``snapshot_dtm <= as_of`` (PIT).

    Returns:
      List aligned with ``eval_rows`` (entries may be ``None``), or ``None`` if join cannot run.
    """
    notes: list[str] = []
    if not eval_rows:
        return [], notes
    if not parquet_path.is_file():
        return None, ["parquet_not_found_for_profiles"]
    try:
        import duckdb  # type: ignore
        import pandas as pd  # type: ignore
    except Exception:
        return None, ["duckdb_or_pandas_unavailable_for_parquet_asof"]
    try:
        probe = duckdb.connect(":memory:")
        cols = {
            str(r[0])
            for r in probe.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(parquet_path)]).fetchall()
        }
        probe.close()
    except Exception:
        return None, ["parquet_profile_describe_failed"]

    req = {"canonical_id", "snapshot_dtm", *PROFILE_FIELDS}
    if not req.issubset(cols):
        return None, [f"parquet_profile_missing_cols_for_asof:{','.join(sorted(req - cols))}"]

    pp = str(parquet_path.resolve())
    sel = ", ".join(f"prof.{c}" for c in PROFILE_FIELDS)
    sql = f"""
    SELECT ev.rid, {sel}
    FROM ev_chunk ev
    LEFT JOIN read_parquet(?) prof
      ON ev.canonical_id = prof.canonical_id
     AND prof.snapshot_dtm <= ev.as_of
    QUALIFY ROW_NUMBER() OVER (PARTITION BY ev.rid ORDER BY prof.snapshot_dtm DESC NULLS LAST) = 1
    """

    out: list[dict[str, Any] | None] = [None] * len(eval_rows)
    rows_with_any_field = 0
    con = duckdb.connect(":memory:")
    try:
        for start in range(0, len(eval_rows), chunk_size):
            end = min(start + chunk_size, len(eval_rows))
            rids: list[int] = []
            cids_chunk: list[str] = []
            as_ofs: list[Any] = []
            for i in range(start, end):
                r = eval_rows[i]
                ao = _as_of_naive_utc_for_profile_join(r.scored_at)
                if ao is None:
                    continue
                rids.append(i)
                cids_chunk.append(r.canonical_id)
                as_ofs.append(ao)
            if not rids:
                continue
            df = pd.DataFrame({"rid": rids, "canonical_id": cids_chunk, "as_of": as_ofs})
            con.register("ev_chunk", df)
            try:
                res = con.execute(sql, [pp]).fetchall()
            finally:
                try:
                    con.unregister("ev_chunk")
                except Exception:
                    pass
            for row in res:
                rid = int(row[0])
                vals = list(row[1 : 1 + len(PROFILE_FIELDS)])
                if all(v is None for v in vals):
                    out[rid] = None
                else:
                    out[rid] = {PROFILE_FIELDS[j]: vals[j] for j in range(len(PROFILE_FIELDS))}
                    rows_with_any_field += 1
    except Exception:
        try:
            con.close()
        except Exception:
            pass
        return None, ["parquet_asof_query_failed"]
    con.close()

    notes.append(f"profile_parquet_asof_rows_with_any_field:{rows_with_any_field}")
    notes.append(f"profile_parquet_asof_chunk_size:{chunk_size}")
    return out, notes


def _load_profiles_from_state_db(state_db: Path, cids: list[str], table: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    notes: list[str] = []
    out: dict[str, dict[str, Any]] = {}
    if not cids:
        return out, ["no_canonical_ids"]
    if not state_db.is_file():
        return out, ["state_db_not_found_for_profiles"]
    try:
        with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True) as conn:
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            req = {"canonical_id", *PROFILE_FIELDS}
            if not req.issubset(cols):
                return out, [f"state_db_profile_missing_cols:{','.join(sorted(req - cols))}"]
            uniq = sorted({c for c in cids if c})
            chunk = 800
            for i in range(0, len(uniq), chunk):
                part = uniq[i : i + chunk]
                q = ",".join(["?"] * len(part))
                sql = (
                    "SELECT canonical_id, theo_win_sum_30d, active_days_30d, "
                    "turnover_sum_30d, days_since_first_session "
                    f"FROM {table} WHERE canonical_id IN ({q})"
                )
                for r in conn.execute(sql, tuple(part)).fetchall():
                    cid = str(r[0] or "").strip()
                    if not cid or cid in out:
                        continue
                    out[cid] = {
                        "theo_win_sum_30d": r[1],
                        "active_days_30d": r[2],
                        "turnover_sum_30d": r[3],
                        "days_since_first_session": r[4],
                    }
    except sqlite3.Error:
        return out, ["state_db_unavailable_for_profiles"]
    return out, notes


def _has_note_prefix(notes: list[str], prefix: str) -> bool:
    for n in notes:
        if str(n).startswith(prefix):
            return True
    return False


def _load_profiles_from_clickhouse(
    cids: list[str],
    *,
    source_db: str = "",
    profile_table: str = "",
    max_ids: int = 5000,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    notes: list[str] = []
    out: dict[str, dict[str, Any]] = {}
    uniq = sorted({c for c in cids if c})
    if len(uniq) > max_ids:
        return out, [f"clickhouse_id_limit_exceeded:{len(uniq)}>{max_ids}"]
    try:
        from trainer import db_conn as _db_conn
        from trainer.core import config as _cfg
    except Exception:
        return out, ["clickhouse_import_failed"]
    db = source_db.strip() or str(_cfg.SOURCE_DB).strip()
    tbl = profile_table.strip() or str(_cfg.TPROFILE).strip()
    if not db or not tbl:
        return out, ["clickhouse_missing_source_table"]
    full = f"{db}.{tbl}"
    try:
        cli = _db_conn.get_clickhouse_client()
        ddf = cli.query_df(f"DESCRIBE TABLE {full}")
    except Exception:
        return out, ["clickhouse_describe_failed"]
    if "name" not in getattr(ddf, "columns", []):
        return out, ["clickhouse_describe_missing_name_col"]
    cols = {str(x) for x in ddf["name"].tolist()}
    req = {"canonical_id", *PROFILE_FIELDS}
    if not req.issubset(cols):
        return out, [f"clickhouse_profile_missing_cols:{','.join(sorted(req - cols))}"]
    chunk = 800
    for i in range(0, len(uniq), chunk):
        part = uniq[i : i + chunk]
        sql = (
            f"SELECT canonical_id, theo_win_sum_30d, active_days_30d, turnover_sum_30d, days_since_first_session "
            f"FROM {full} WHERE canonical_id IN %(cids)s"
        )
        try:
            pdf = cli.query_df(sql, parameters={"cids": tuple(part)})
        except Exception:
            notes.append("clickhouse_query_failed")
            continue
        if pdf is None or len(pdf) == 0:
            continue
        for row in pdf.itertuples(index=False):
            cid = str(getattr(row, "canonical_id", "") or "").strip()
            if not cid or cid in out:
                continue
            out[cid] = {
                "theo_win_sum_30d": getattr(row, "theo_win_sum_30d", None),
                "active_days_30d": getattr(row, "active_days_30d", None),
                "turnover_sum_30d": getattr(row, "turnover_sum_30d", None),
                "days_since_first_session": getattr(row, "days_since_first_session", None),
            }
    return out, notes


def _aggregate_segment_error(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = str(r.get(key) or f"UNKNOWN_{key.upper()}")
        item = agg.setdefault(
            k,
            {
                "segment_key": k,
                "n": 0,
                "error_count": 0,
                "tp": 0,
                "fp": 0,
                "fn": 0,
                "alerts": 0,
            },
        )
        item["n"] += 1
        item["error_count"] += int(r.get("error", 0))
        is_alert = int(r.get("is_alert", 0))
        label = int(r.get("label", 0))
        if is_alert == 1:
            item["alerts"] += 1
            if label == 1:
                item["tp"] += 1
            else:
                item["fp"] += 1
        elif label == 1:
            item["fn"] += 1
    out: list[dict[str, Any]] = []
    for x in agg.values():
        n = int(x["n"])
        e = int(x["error_count"])
        tp = int(x["tp"])
        fp = int(x["fp"])
        fn = int(x["fn"])
        alerts = int(x["alerts"])
        out.append(
            {
                "segment_key": x["segment_key"],
                "n": n,
                "error_count": e,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "alerts": alerts,
                "precision_at_alert": (float(tp) / float(tp + fp)) if (tp + fp) > 0 else None,
                "error_rate": (float(e) / float(n)) if n > 0 else None,
                "alert_rate": (float(alerts) / float(n)) if n > 0 else None,
            }
        )
    out.sort(
        key=lambda z: (
            (z["precision_at_alert"] is None),  # prefer segments with defined precision
            (z["precision_at_alert"] if z["precision_at_alert"] is not None else 1.0),  # lowest precision first
            -(z["error_rate"] or 0.0),  # then higher error rate
            -z["n"],
        )
    )
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Ad-hoc by-segment error-rate analysis from prediction_log/state_db.")
    p.add_argument("--prediction-log-db", default="")
    p.add_argument("--state-db", default="")
    p.add_argument("--backtest-predictions-parquet", default="")
    p.add_argument("--start-ts", required=True)
    p.add_argument("--end-ts", required=True)
    p.add_argument("--output-json", default="")
    p.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Max eval rows to load. <=0 means no cap (use all available rows).",
    )
    p.add_argument(
        "--model-bundle-dir",
        type=Path,
        default=Path("out/models/20260419-040815-6ec219f"),
        help=(
            "Directory with training_metrics.json. Default score threshold is "
            "rated.threshold_at_recall_0.01 from that file (unless --score-threshold is set)."
        ),
    )
    p.add_argument(
        "--score-threshold",
        type=float,
        default=_SENTINEL_SCORE_THRESHOLD,
        help=(
            "Alert if score >= this when ``is_alert`` is absent. "
            "Default: read rated.threshold_at_recall_0.01 from --model-bundle-dir/training_metrics.json."
        ),
    )
    p.add_argument("--profile-table", default="player_profile")
    p.add_argument("--profile-parquet-path", default="")
    p.add_argument(
        "--use-clickhouse-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "After embedded/state/parquet profile merge, query ClickHouse for remaining gaps. "
            "Default: on. Use --no-use-clickhouse-fallback to skip."
        ),
    )
    p.add_argument("--clickhouse-source-db", default="")
    p.add_argument("--clickhouse-profile-table", default="")
    p.add_argument("--min-segment-n", type=int, default=30)
    args = p.parse_args()

    model_bundle_dir = Path(args.model_bundle_dir).expanduser().resolve()
    if args.score_threshold is not _SENTINEL_SCORE_THRESHOLD:
        score_threshold = float(args.score_threshold)
        score_threshold_source = "cli_override"
    else:
        score_threshold = _load_rated_threshold_at_recall_0_01(model_bundle_dir)
        score_threshold_source = "training_metrics.rated.threshold_at_recall_0.01"

    pred_db = Path(str(args.prediction_log_db).strip()) if str(args.prediction_log_db).strip() else Path()
    state_db = Path(str(args.state_db).strip()) if str(args.state_db).strip() else Path()
    bt_pred_path = Path(str(args.backtest_predictions_parquet).strip()) if str(args.backtest_predictions_parquet).strip() else None
    embedded_profile_rows: list[dict[str, Any]] = []
    row_cap = int(args.max_rows)
    if row_cap <= 0:
        row_cap = 0
        eval_cap = 0
    else:
        eval_cap = row_cap

    if bt_pred_path is not None:
        eval_rows, eval_notes, embedded_profile_rows = _load_eval_rows_from_backtest_parquet(
            bt_pred_path,
            start_ts=str(args.start_ts),
            end_ts=str(args.end_ts),
            max_rows=eval_cap,
            score_threshold=score_threshold,
        )
        eval_notes.append("eval_source:backtest_predictions_parquet")
    else:
        if not str(args.prediction_log_db).strip() or not str(args.state_db).strip():
            out = {
                "notes": [
                    "missing_required_inputs",
                    "require --backtest-predictions-parquet OR both --prediction-log-db and --state-db",
                ],
                "segments": {},
                "summary": {},
            }
            if args.output_json:
                Path(args.output_json).write_text(json.dumps(out, indent=2), encoding="utf-8")
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 2
        eval_rows, eval_notes = _load_eval_rows(
            pred_db,
            state_db,
            start_ts=str(args.start_ts),
            end_ts=str(args.end_ts),
            max_rows=eval_cap,
            score_threshold=score_threshold,
        )
        eval_notes.append("eval_source:prediction_log_state_db")
    if eval_cap == 0:
        eval_notes.append("eval_rows_uncapped")
    eval_notes.append(
        f"score_threshold_effective:{score_threshold}|source:{score_threshold_source}|"
        f"model_bundle_dir:{model_bundle_dir.as_posix()}"
    )
    if not eval_rows:
        out = {"notes": eval_notes + ["no_eval_rows"], "segments": {}, "summary": {}}
        if args.output_json:
            Path(args.output_json).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 2

    cids = [r.canonical_id for r in eval_rows]
    profiles = _profiles_from_backtest_rows(embedded_profile_rows)
    profile_notes: list[str] = []
    source_parts: list[str] = []
    if profiles:
        source_parts.append("backtest_predictions_parquet_embedded")

    need_cids = _missing_profile_cids(profiles, cids)
    # Load from state_db as primary only when no embedded profiles exist.
    # When embedded profiles exist, use state_db as supplementary source if provided.
    has_state_db_arg = bool(str(args.state_db).strip())
    if (not profiles) or (has_state_db_arg and need_cids):
        st_profiles, st_notes = _load_profiles_from_state_db(
            state_db, need_cids if need_cids else cids, str(args.profile_table)
        )
        profile_notes.extend(st_notes)
        st_filled = _merge_profiles(profiles, st_profiles)
        if st_filled > 0:
            source_parts.append("state_db")
            profile_notes.append(f"profile_fields_filled_from_state_db:{st_filled}")

    parquet_path_raw = str(args.profile_parquet_path or "").strip()
    parquet_path = Path(parquet_path_raw) if parquet_path_raw else (Path.cwd() / "data" / "player_profile.parquet")
    tried_default_parquet = not bool(parquet_path_raw)

    need_cids = _missing_profile_cids(profiles, cids)
    if need_cids and args.use_clickhouse_fallback:
        ch_profiles, cn = _load_profiles_from_clickhouse(
            need_cids,
            source_db=str(args.clickhouse_source_db),
            profile_table=str(args.clickhouse_profile_table),
        )
        profile_notes.extend(cn)
        ch_filled = _merge_profiles(profiles, ch_profiles)
        if ch_filled > 0:
            source_parts.append("clickhouse")
            profile_notes.append(f"profile_fields_filled_from_clickhouse:{ch_filled}")

    parquet_row_profiles: list[dict[str, Any] | None] | None = None
    if parquet_path.is_file():
        parquet_row_profiles, pn = _load_profiles_parquet_asof_per_row(parquet_path, eval_rows)
        profile_notes.extend(pn)
        if parquet_row_profiles is not None:
            source_parts.append("parquet_asof")
            if _has_note_prefix(profile_notes, "state_db_profile_missing_cols:"):
                profile_notes.append("state_db_profile_missing_cols_resolved_by_parquet")
    elif tried_default_parquet:
        profile_notes.append(
            f"parquet_default_not_usable:{parquet_path.as_posix()}"
        )

    source = "+".join(source_parts) if source_parts else "none"

    enriched: list[dict[str, Any]] = []
    adt_vals: list[float] = []
    act_vals: list[float] = []
    to_vals: list[float] = []
    metric_ready_idx: list[int] = []
    drop_counts: dict[str, int] = {
        "missing_profile_for_canonical_id": 0,
        "profile_missing_required_field": 0,
        "profile_non_numeric_field": 0,
        "profile_active_days_non_positive": 0,
    }
    for i, r in enumerate(eval_rows):
        row_p = parquet_row_profiles[i] if parquet_row_profiles is not None else None
        pr = _merge_parquet_row_into_cid_profile(row_p, profiles, r.canonical_id)
        if not pr:
            drop_counts["missing_profile_for_canonical_id"] += 1
            continue
        metric_ready = False
        try:
            ad = float(pr["active_days_30d"])
            theo = float(pr["theo_win_sum_30d"])
            to_v = float(pr["turnover_sum_30d"])
            if ad <= 0:
                drop_counts["profile_active_days_non_positive"] += 1
            else:
                adt = theo / ad
                adt_vals.append(adt)
                act_vals.append(ad)
                to_vals.append(to_v)
                metric_ready = True
        except KeyError:
            drop_counts["profile_missing_required_field"] += 1
        except (TypeError, ValueError):
            drop_counts["profile_non_numeric_field"] += 1
        enriched.append(
            {
                "bet_id": r.bet_id,
                "canonical_id": r.canonical_id,
                "eval_date": _eval_date_hkt(r.scored_at),
                "table_id": r.table_id or "UNKNOWN_TABLE",
                "tenure_bucket": _tenure_bucket(pr.get("days_since_first_session")),
                "is_alert": int(r.is_alert),
                "label": int(r.label),
                "error": int(int(r.is_alert) != int(r.label)),
                "_metric_ready": metric_ready,
            }
        )
        if metric_ready:
            metric_ready_idx.append(len(enriched) - 1)

    if not enriched:
        dropped_total = sum(drop_counts.values())
        out = {
            "notes": eval_notes + profile_notes + [f"profile_join_dropped_total:{dropped_total}", "no_rows_after_profile_join"],
            "segments": {},
            "summary": {
                "source": source,
                "eval_rows_total": len(eval_rows),
                "rows_after_profile_join": 0,
                "profile_join_drop_counts": drop_counts,
                "score_threshold_effective": score_threshold,
                "score_threshold_source": score_threshold_source,
                "model_bundle_dir": model_bundle_dir.as_posix(),
            },
        }
        if args.output_json:
            Path(args.output_json).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 3

    adt_lbl = _empirical_decile_labels(adt_vals, "adt")
    act_lbl = _empirical_decile_labels(act_vals, "activity")
    to_lbl = _empirical_decile_labels(to_vals, "to")
    for row in enriched:
        row["adt_percentile_bucket"] = "adt_unknown"
        row["activity_percentile_bucket"] = "activity_unknown"
        row["turnover_30d_percentile_bucket"] = "to_unknown"
    for j, idx in enumerate(metric_ready_idx):
        row = enriched[idx]
        row["adt_percentile_bucket"] = adt_lbl[j]
        row["activity_percentile_bucket"] = act_lbl[j]
        row["turnover_30d_percentile_bucket"] = to_lbl[j]
        row.pop("_metric_ready", None)
    for row in enriched:
        row.pop("_metric_ready", None)

    seg_dims = [
        "eval_date",
        "table_id",
        "adt_percentile_bucket",
        "tenure_bucket",
        "activity_percentile_bucket",
        "turnover_30d_percentile_bucket",
    ]
    seg_out: dict[str, list[dict[str, Any]]] = {}
    for d in seg_dims:
        rows = _aggregate_segment_error(enriched, d)
        min_n = max(1, int(args.min_segment_n))
        seg_out[d] = [x for x in rows if int(x["n"]) >= min_n]

    total_n = len(enriched)
    total_err = sum(int(x["error"]) for x in enriched)
    dropped_total = int(drop_counts["missing_profile_for_canonical_id"])
    notes = list(eval_notes) + list(profile_notes)
    notes.append(f"profile_join_dropped_total:{dropped_total}")
    notes.append(
        "profile_rows_kept_with_unknown_buckets_when_profile_missing_or_active_days_non_positive"
    )
    for k, v in drop_counts.items():
        notes.append(f"profile_join_drop_{k}:{v}")
    result = {
        "summary": {
            "eval_rows_total": len(eval_rows),
            "rows_after_profile_join": total_n,
            "rows_dropped_after_profile_join": dropped_total,
            "profile_join_drop_counts": drop_counts,
            "global_error_rate": (float(total_err) / float(total_n)) if total_n > 0 else None,
            "profile_source_used": source,
            "backtest_predictions_parquet_used": str(bt_pred_path) if bt_pred_path is not None else "",
            "start_ts": str(args.start_ts),
            "end_ts": str(args.end_ts),
            "score_threshold_effective": score_threshold,
            "score_threshold_source": score_threshold_source,
            "model_bundle_dir": model_bundle_dir.as_posix(),
        },
        "notes": notes,
        "segments": seg_out,
    }

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"[wrote] {out_path}")

    print("Top segment drags (by precision_at_alert, then error_rate):")
    for dim in seg_dims:
        rows = seg_out.get(dim) or []
        if not rows:
            print(f"- {dim}: no rows (after min_segment_n filter)")
            continue
        top = rows[:5]
        line = ", ".join(
            (
                f"{r['segment_key']} "
                f"prec={r['precision_at_alert']:.3f}" if r["precision_at_alert"] is not None else f"{r['segment_key']} prec=NA"
            )
            + f" err={r['error_rate']:.3f} alert_rate={r['alert_rate']:.3f} n={r['n']}"
            for r in top
            if r["error_rate"] is not None and r["alert_rate"] is not None
        )
        print(f"- {dim}: {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

