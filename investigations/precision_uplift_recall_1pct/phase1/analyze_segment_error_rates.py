"""Ad-hoc Phase 1 segment error analysis (W1-B2 quick path).

Purpose:
- Given ``prediction_log`` + ``state_db``, compute by-segment error rate and sample size.
- Use Parquet / ClickHouse only when profile segment fields are unavailable in state DB.

Output:
- JSON file with per-segment metrics and metadata.
- Console summary (top segments by error rate, filtered by min sample size).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PROFILE_FIELDS: tuple[str, ...] = (
    "theo_win_sum_30d",
    "active_days_30d",
    "turnover_sum_30d",
    "days_since_first_session",
)


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
            sql = (
                f"SELECT {sel} FROM prediction_log WHERE scored_at >= ? AND scored_at < ?"
                f"{rated_filter} ORDER BY scored_at ASC LIMIT ?"
            )
            rows = conn.execute(sql, (start_ts, end_ts, max_rows + 1)).fetchall()
    except sqlite3.Error:
        return [], ["prediction_log_db_unavailable"]

    if len(rows) > max_rows:
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
        sql = (
            f"SELECT {select_cols} FROM read_parquet(?) "
            f"WHERE {ts_col} >= ? AND {ts_col} < ? "
            f"ORDER BY {ts_col} ASC LIMIT ?"
        )
        rows = con.execute(
            sql,
            [str(parquet_path), start_ts, end_ts, max_rows + 1],
        ).fetchall()
        con.close()
    except Exception:
        return out, ["backtest_predictions_query_failed"], profile_rows

    if len(rows) > max_rows:
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
    # keep only complete profile rows
    ready: dict[str, dict[str, Any]] = {}
    for cid, pr in out.items():
        if all(k in pr for k in PROFILE_FIELDS):
            ready[cid] = pr
    return ready


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


def _load_profiles_from_parquet(parquet_path: Path, cids: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    notes: list[str] = []
    out: dict[str, dict[str, Any]] = {}
    if not parquet_path.is_file():
        return out, ["parquet_not_found_for_profiles"]
    try:
        import duckdb  # type: ignore
    except Exception:
        return out, ["duckdb_unavailable_for_parquet_profiles"]
    uniq = sorted({c for c in cids if c})
    if not uniq:
        return out, ["no_canonical_ids"]
    try:
        con = duckdb.connect(":memory:")
        cols = {str(r[0]) for r in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(parquet_path)]).fetchall()}
        req = {"canonical_id", *PROFILE_FIELDS}
        if not req.issubset(cols):
            con.close()
            return out, [f"parquet_profile_missing_cols:{','.join(sorted(req - cols))}"]
        rows = con.execute(
            "SELECT canonical_id, theo_win_sum_30d, active_days_30d, turnover_sum_30d, days_since_first_session "
            "FROM read_parquet(?) WHERE canonical_id IN ?",
            [str(parquet_path), tuple(uniq)],
        ).fetchall()
        con.close()
    except Exception:
        return out, ["parquet_query_failed_for_profiles"]
    for r in rows:
        cid = str(r[0] or "").strip()
        if not cid or cid in out:
            continue
        out[cid] = {
            "theo_win_sum_30d": r[1],
            "active_days_30d": r[2],
            "turnover_sum_30d": r[3],
            "days_since_first_session": r[4],
        }
    return out, notes


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
    p.add_argument("--max-rows", type=int, default=200000)
    p.add_argument("--score-threshold", type=float, default=0.5)
    p.add_argument("--profile-table", default="player_profile")
    p.add_argument("--profile-parquet-path", default="")
    p.add_argument("--use-clickhouse-fallback", action="store_true")
    p.add_argument("--clickhouse-source-db", default="")
    p.add_argument("--clickhouse-profile-table", default="")
    p.add_argument("--min-segment-n", type=int, default=30)
    args = p.parse_args()

    pred_db = Path(str(args.prediction_log_db).strip()) if str(args.prediction_log_db).strip() else Path()
    state_db = Path(str(args.state_db).strip()) if str(args.state_db).strip() else Path()
    bt_pred_path = Path(str(args.backtest_predictions_parquet).strip()) if str(args.backtest_predictions_parquet).strip() else None
    embedded_profile_rows: list[dict[str, Any]] = []
    if bt_pred_path is not None:
        eval_rows, eval_notes, embedded_profile_rows = _load_eval_rows_from_backtest_parquet(
            bt_pred_path,
            start_ts=str(args.start_ts),
            end_ts=str(args.end_ts),
            max_rows=max(1, int(args.max_rows)),
            score_threshold=float(args.score_threshold),
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
            max_rows=max(1, int(args.max_rows)),
            score_threshold=float(args.score_threshold),
        )
        eval_notes.append("eval_source:prediction_log_state_db")
    if not eval_rows:
        out = {"notes": eval_notes + ["no_eval_rows"], "segments": {}, "summary": {}}
        if args.output_json:
            Path(args.output_json).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 2

    cids = [r.canonical_id for r in eval_rows]
    profiles = _profiles_from_backtest_rows(embedded_profile_rows)
    profile_notes: list[str] = []
    source = "backtest_predictions_parquet_embedded" if profiles else "state_db"
    if not profiles:
        profiles, profile_notes = _load_profiles_from_state_db(
            state_db, cids, str(args.profile_table)
        )

    # Fallback trigger: no profiles OR state_db profile schema is insufficient.
    need_profile_fallback = (not profiles) or _has_note_prefix(
        profile_notes, "state_db_profile_missing_cols:"
    )
    parquet_path_raw = str(args.profile_parquet_path or "").strip()
    parquet_path = Path(parquet_path_raw) if parquet_path_raw else (Path.cwd() / "data" / "player_profile.parquet")
    tried_default_parquet = not bool(parquet_path_raw)

    if need_profile_fallback:
        parq_profiles, pn = _load_profiles_from_parquet(parquet_path, cids)
        profile_notes.extend(pn)
        if parq_profiles:
            profiles = parq_profiles
            source = "parquet"
            if _has_note_prefix(profile_notes, "state_db_profile_missing_cols:"):
                profile_notes.append("state_db_profile_missing_cols_resolved_by_parquet")
        elif tried_default_parquet:
            profile_notes.append(
                f"parquet_default_not_usable:{parquet_path.as_posix()}"
            )

    if (not profiles) and args.use_clickhouse_fallback:
        profiles, cn = _load_profiles_from_clickhouse(
            cids,
            source_db=str(args.clickhouse_source_db),
            profile_table=str(args.clickhouse_profile_table),
        )
        profile_notes.extend(cn)
        if profiles:
            source = "clickhouse"

    enriched: list[dict[str, Any]] = []
    adt_vals: list[float] = []
    act_vals: list[float] = []
    to_vals: list[float] = []
    drop_counts: dict[str, int] = {
        "missing_profile_for_canonical_id": 0,
        "profile_missing_required_field": 0,
        "profile_non_numeric_field": 0,
        "profile_active_days_non_positive": 0,
    }
    for r in eval_rows:
        pr = profiles.get(r.canonical_id)
        if not isinstance(pr, dict):
            drop_counts["missing_profile_for_canonical_id"] += 1
            continue
        try:
            ad = float(pr["active_days_30d"])
            theo = float(pr["theo_win_sum_30d"])
            to_v = float(pr["turnover_sum_30d"])
        except KeyError:
            drop_counts["profile_missing_required_field"] += 1
            continue
        except (TypeError, ValueError):
            drop_counts["profile_non_numeric_field"] += 1
            continue
        if ad <= 0:
            drop_counts["profile_active_days_non_positive"] += 1
            continue
        adt = theo / ad
        adt_vals.append(adt)
        act_vals.append(ad)
        to_vals.append(to_v)
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
            }
        )

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
            },
        }
        if args.output_json:
            Path(args.output_json).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 3

    adt_lbl = _empirical_decile_labels(adt_vals, "adt")
    act_lbl = _empirical_decile_labels(act_vals, "activity")
    to_lbl = _empirical_decile_labels(to_vals, "to")
    for i, row in enumerate(enriched):
        row["adt_percentile_bucket"] = adt_lbl[i]
        row["activity_percentile_bucket"] = act_lbl[i]
        row["turnover_30d_percentile_bucket"] = to_lbl[i]

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
    dropped_total = sum(drop_counts.values())
    notes = list(eval_notes) + list(profile_notes)
    notes.append(f"profile_join_dropped_total:{dropped_total}")
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

