#!/usr/bin/env python3
"""
R1/R6 helper for production investigation.

This script provides two stages:
1) sample: build below-threshold candidate sample from prediction_log
2) evaluate: merge offline labels and compute:
   - precision/recall at current threshold (is_alert)
   - precision@recall=target (default 1%)

Notes:
- Read-only against prediction_log DB.
- Designed for low-memory environments: stream rows for sampling; evaluate on
  labeled subset only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, cast
from zoneinfo import ZoneInfo

import pandas as pd  # type: ignore[import-untyped]

# Ensure repo root is importable when script is executed by file path.
_REPO_ROOT_FOR_IMPORT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_FOR_IMPORT))

from trainer.core import config  # noqa: E402
from trainer.db_conn import get_clickhouse_client  # noqa: E402
from trainer.labels import compute_labels  # noqa: E402


HK_TZ = ZoneInfo("Asia/Hong_Kong")
DEFAULT_TARGET_RECALL = 0.01
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class CandidateRow:
    bet_id: str
    score: float
    scored_at: str
    is_alert: int
    is_rated_obs: int
    bin_id: int


def _log(msg: str) -> None:
    # Keep machine-readable JSON on stdout; progress goes to stderr.
    print(f"[r1_r6] {msg}", file=sys.stderr)


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[3]


def _parse_env_line(line: str) -> Optional[Tuple[str, str]]:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    k, v = s.split("=", 1)
    k = k.strip()
    v = v.strip()
    if not k:
        return None
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1]
    return k, v


def _load_env_file(path: Path) -> Dict[str, str]:
    if not path.exists() or not path.is_file():
        return {}
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        k, v = parsed
        out[k] = v
    return out


def _resolve_pred_db_path(env_file: str, pred_db_path: str) -> Tuple[str, Optional[str]]:
    if pred_db_path.strip():
        return pred_db_path.strip(), None

    repo_root = _repo_root_from_script()
    env_candidates: Sequence[Path]
    if env_file.strip():
        env_candidates = [Path(env_file).resolve()]
    else:
        env_candidates = [
            Path.cwd() / "credential" / ".env",
            Path.cwd() / ".env",
            repo_root / "credential" / ".env",
            repo_root / ".env",
        ]

    env_used = None
    file_env: Dict[str, str] = {}
    for p in env_candidates:
        d = _load_env_file(p)
        if "PREDICTION_LOG_DB_PATH" in d:
            file_env = d
            env_used = str(p)
            break

    # If caller explicitly passes --env-file, prefer that file over process env to
    # avoid stale shell env unexpectedly pointing to another machine's DB.
    if env_file.strip():
        value = file_env.get("PREDICTION_LOG_DB_PATH") or os.getenv("PREDICTION_LOG_DB_PATH") or ""
    else:
        value = os.getenv("PREDICTION_LOG_DB_PATH") or file_env.get("PREDICTION_LOG_DB_PATH") or ""
    return value.strip(), env_used


def _resolve_env_path_for_key(
    key: str,
    env_file: str,
    cli_override: str,
    default_path: Path,
) -> Tuple[str, Optional[str]]:
    """Resolve a path from CLI, env file (with optional env-file precedence), or default."""
    if cli_override.strip():
        return cli_override.strip(), None

    repo_root = _repo_root_from_script()
    if env_file.strip():
        env_candidates: Sequence[Path] = [Path(env_file).resolve()]
    else:
        env_candidates = [
            Path.cwd() / "credential" / ".env",
            Path.cwd() / ".env",
            repo_root / "credential" / ".env",
            repo_root / ".env",
        ]

    env_used = None
    file_env: Dict[str, str] = {}
    for p in env_candidates:
        d = _load_env_file(p)
        if key in d:
            file_env = d
            env_used = str(p)
            break

    if env_file.strip():
        value = file_env.get(key) or os.getenv(key) or ""
    else:
        value = os.getenv(key) or file_env.get(key) or ""
    value = value.strip()
    if value:
        return value, env_used
    return str(default_path), env_used


def _resolve_state_db_path(env_file: str, state_db_path: str) -> Tuple[str, Optional[str]]:
    default = _repo_root_from_script() / "local_state" / "state.db"
    return _resolve_env_path_for_key("STATE_DB_PATH", env_file, state_db_path, default)


def _resolve_model_dir(env_file: str, model_dir: str) -> Tuple[str, Optional[str]]:
    default = Path(str(getattr(config, "DEFAULT_MODEL_DIR", _repo_root_from_script() / "out" / "models")))
    return _resolve_env_path_for_key("MODEL_DIR", env_file, model_dir, default)


def _resolve_window(start_ts: str, end_ts: str) -> Tuple[str, str]:
    s = start_ts.strip()
    e = end_ts.strip()
    if bool(s) ^ bool(e):
        raise ValueError("start-ts and end-ts must be provided together")
    if s and e:
        return s, e
    now_hk = datetime.now(HK_TZ)
    today_start = now_hk.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    return yesterday_start.isoformat(), today_start.isoformat()


def _default_snapshot_paths(start_ts: str, end_ts: str) -> Tuple[Path, Path]:
    """
    Build deterministic per-window artifact paths under investigation snapshots.
    """
    _ = end_ts  # reserved for future naming needs
    start_dt = _parse_iso_ts(start_ts).astimezone(HK_TZ)
    window_tag = start_dt.strftime("%Y%m%d")
    run_tag = f"{datetime.now(HK_TZ).strftime('%H%M%S_%f')}_{time.time_ns()}_{uuid.uuid4().hex[:8]}"
    base = _repo_root_from_script() / "investigations" / "test_vs_production" / "snapshots"
    sample_csv = base / f"latest_r1_r6_below_threshold_sample_{window_tag}_{run_tag}.csv"
    labels_csv = base / f"latest_r1_r6_labeled_{window_tag}_{run_tag}.csv"
    return sample_csv, labels_csv


def _parse_iso_ts(ts: str) -> datetime:
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must include timezone offset: {ts}")
    return dt


def _bin_from_score(score: float, bins: int) -> int:
    if math.isnan(score):
        return -1
    if score <= 0.0:
        return 0
    if score >= 1.0:
        return bins - 1
    idx = int(score * bins)
    if idx >= bins:
        idx = bins - 1
    return idx


def _stable_randint(upper_exclusive: int, seed: int, key: str) -> int:
    if upper_exclusive <= 0:
        return 0
    digest = hashlib.sha256(f"{seed}:{key}".encode("utf-8")).digest()
    v = int.from_bytes(digest[:8], "big", signed=False)
    return v % upper_exclusive


def _reservoir_update(reservoir: List[CandidateRow], item: CandidateRow, seen: int, target_size: int, rng_seed: int) -> None:
    # Deterministic across processes: avoid builtin hash() randomization.
    if len(reservoir) < target_size:
        reservoir.append(item)
        return
    # pseudo-random integer in [0, seen-1]
    j = _stable_randint(seen, rng_seed, f"{item.bet_id}:{seen}:{item.bin_id}")
    if j < target_size:
        reservoir[j] = item


def run_sample_mode(
    db_path: str,
    start_ts: str,
    end_ts: str,
    sample_size: int,
    bins: int,
    seed: int,
    out_csv: Path,
    candidate_filter: str = "below_threshold",
    overwrite: bool = False,
) -> Dict[str, object]:
    _log(
        "sample: starting "
        f"(db={db_path}, window={start_ts} -> {end_ts}, sample_size={sample_size}, bins={bins}, filter={candidate_filter})"
    )
    if sample_size <= 0:
        raise ValueError("sample-size must be > 0")
    if bins <= 0:
        raise ValueError("bins must be > 0")
    if candidate_filter not in {"below_threshold", "alert", "all_rated"}:
        raise ValueError("candidate-filter must be one of: below_threshold, alert, all_rated")

    if out_csv.exists() and not overwrite:
        raise FileExistsError(f"output CSV already exists (set overwrite): {out_csv}")

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='prediction_log'")
        if cur.fetchone() is None:
            raise RuntimeError("prediction_log table not found")

        cur.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN is_rated_obs=1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN is_rated_obs=1 AND is_alert=1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN is_rated_obs=1 AND is_alert=0 THEN 1 ELSE 0 END)
            FROM prediction_log
            WHERE scored_at >= ? AND scored_at < ?
            """,
            (start_ts, end_ts),
        )
        n_total, n_rated, n_alert_rated, n_below_rated = cur.fetchone()

        per_bin_target = max(1, sample_size // bins)
        reservoirs: Dict[int, List[CandidateRow]] = {i: [] for i in range(bins)}
        seen_per_bin: Dict[int, int] = {i: 0 for i in range(bins)}

        if candidate_filter == "below_threshold":
            sample_sql = """
                SELECT bet_id, score, scored_at, is_alert, is_rated_obs
                FROM prediction_log
                WHERE scored_at >= ? AND scored_at < ?
                  AND is_rated_obs = 1
                  AND is_alert = 0
            """
        elif candidate_filter == "alert":
            sample_sql = """
                SELECT bet_id, score, scored_at, is_alert, is_rated_obs
                FROM prediction_log
                WHERE scored_at >= ? AND scored_at < ?
                  AND is_rated_obs = 1
                  AND is_alert = 1
            """
        else:
            sample_sql = """
                SELECT bet_id, score, scored_at, is_alert, is_rated_obs
                FROM prediction_log
                WHERE scored_at >= ? AND scored_at < ?
                  AND is_rated_obs = 1
            """

        cursor = conn.execute(sample_sql, (start_ts, end_ts))
        for bet_id, score, scored_at, is_alert, is_rated_obs in cursor:
            if bet_id is None or score is None:
                continue
            try:
                fscore = float(score)
            except (TypeError, ValueError):
                continue
            b = _bin_from_score(fscore, bins)
            if b < 0:
                continue
            row = CandidateRow(
                bet_id=str(bet_id),
                score=fscore,
                scored_at=str(scored_at),
                is_alert=int(is_alert or 0),
                is_rated_obs=int(is_rated_obs or 0),
                bin_id=b,
            )
            seen_per_bin[b] += 1
            _reservoir_update(reservoirs[b], row, seen_per_bin[b], per_bin_target, seed)

        sampled = [item for b in range(bins) for item in reservoirs[b]]
        sampled = sampled[:sample_size]

        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with out_csv.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["bet_id", "score", "scored_at", "is_alert", "is_rated_obs", "bin_id"])
            for r in sampled:
                w.writerow([r.bet_id, f"{r.score:.10f}", r.scored_at, r.is_alert, r.is_rated_obs, r.bin_id])
        _log(f"sample: done (rows={len(sampled)}, output={out_csv})")

        return {
            "mode": "sample",
            "window": {"start_ts": start_ts, "end_ts": end_ts},
            "db_path": db_path,
            "summary": {
                "n_total": int(n_total or 0),
                "n_rated": int(n_rated or 0),
                "n_alert_rated": int(n_alert_rated or 0),
                "n_below_rated": int(n_below_rated or 0),
                "candidate_filter": candidate_filter,
                "sample_size_requested": sample_size,
                "sample_size_written": len(sampled),
                "bins": bins,
                "per_bin_target": per_bin_target,
            },
            "output_csv": str(out_csv),
            "note": (
                "Run offline labeling on output_csv bet_id, then use evaluate mode with labeled CSV. "
                "Use candidate_filter='alert' to diagnose precision drop; "
                "use candidate_filter='below_threshold' to diagnose missed positives."
            ),
        }
    finally:
        conn.close()


def _sqlite_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _evaluate_join_rows_from_labels(
    db_path: str,
    start_ts: str,
    end_ts: str,
    labels: Dict[str, Tuple[int, int]],
) -> Tuple[List[Tuple[str, float, int, int, str]], int]:
    """
    Join prediction_log with offline labels. Returns rows:
    (bet_id, score, is_alert, label, model_version) and count of censored rows excluded.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    n_censored_excluded = 0
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='prediction_log'")
        if cur.fetchone() is None:
            raise RuntimeError("prediction_log table not found")

        has_model_version = "model_version" in _sqlite_table_columns(conn, "prediction_log")
        mv_sql = "p.model_version" if has_model_version else "NULL AS model_version"

        conn.execute("DROP TABLE IF EXISTS _tmp_labels")
        conn.execute(
            "CREATE TEMP TABLE _tmp_labels (bet_id TEXT PRIMARY KEY, label INTEGER NOT NULL, censored INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO _tmp_labels (bet_id, label, censored) VALUES (?, ?, ?)",
            [(bid, y, c) for bid, (y, c) in labels.items()],
        )

        rows: List[Tuple[str, float, int, int, str]] = []
        sql = f"""
            SELECT p.bet_id, p.score, p.is_alert, l.label, l.censored, {mv_sql}
            FROM prediction_log p
            JOIN _tmp_labels l ON p.bet_id = l.bet_id
            WHERE p.scored_at >= ? AND p.scored_at < ?
              AND p.is_rated_obs = 1
        """
        for bet_id, score, is_alert, label, censored, model_version in conn.execute(sql, (start_ts, end_ts)):
            if int(censored or 0) == 1:
                n_censored_excluded += 1
                continue
            if score is None or bet_id is None:
                continue
            mv = str(model_version).strip() if model_version is not None else "unknown"
            rows.append((str(bet_id), float(score), int(is_alert or 0), int(label), mv))
        return rows, n_censored_excluded
    finally:
        conn.close()


def _load_labels_csv(path: Path) -> Dict[str, Tuple[int, int]]:
    # value: (label, censored) where censored in {0,1}; absent column defaults to 0
    labels: Dict[str, Tuple[int, int]] = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"bet_id", "label"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError("labels CSV must contain columns: bet_id,label")
        has_censored = "censored" in set(reader.fieldnames or [])
        for row in reader:
            bid = str(row["bet_id"]).strip()
            if not bid:
                continue
            try:
                y = int(str(row["label"]).strip())
            except ValueError:
                continue
            if y not in (0, 1):
                continue
            c = 0
            if has_censored:
                try:
                    c = int(str(row.get("censored", "0")).strip())
                except ValueError:
                    c = 0
                c = 1 if c == 1 else 0
            labels[bid] = (y, c)
    return labels


def _load_sample_bet_ids_with_stats(path: Path) -> Tuple[List[str], int, int, int]:
    out: List[str] = []
    seen: set[str] = set()
    n_input_rows = 0
    n_duplicate_bet_id = 0
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if "bet_id" not in set(reader.fieldnames or []):
            raise ValueError("sample CSV must contain column: bet_id")
        for row in reader:
            bid = str(row.get("bet_id", "")).strip()
            n_input_rows += 1
            if not bid or bid in seen:
                if bid in seen:
                    n_duplicate_bet_id += 1
                continue
            seen.add(bid)
            out.append(bid)
    return out, n_input_rows, len(out), n_duplicate_bet_id


def _load_sample_bet_ids(path: Path) -> List[str]:
    out, _n_input, _n_unique, _n_dup = _load_sample_bet_ids_with_stats(path)
    return out


def _chunks(seq: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def run_autolabel_mode(
    db_path: str,
    start_ts: str,
    end_ts: str,
    sample_csv: Path,
    out_labels_csv: Path,
    player_chunk_size: int,
    max_players: int = 20000,
) -> Dict[str, object]:
    _log(
        "autolabel: starting "
        f"(db={db_path}, sample_csv={sample_csv}, window={start_ts} -> {end_ts}, chunk={player_chunk_size}, max_players={max_players})"
    )
    if player_chunk_size <= 0:
        raise ValueError("player-chunk-size must be > 0")

    sample_bids, n_input_rows, n_unique_bet_id, n_duplicate_bet_id = _load_sample_bet_ids_with_stats(sample_csv)
    if not sample_bids:
        raise ValueError("sample CSV contains no bet_id rows")
    sample_bid_set = set(sample_bids)

    # 1) Build bet_id -> (player_id, canonical_id) from prediction_log
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("DROP TABLE IF EXISTS _tmp_sample_bids")
        conn.execute("CREATE TEMP TABLE _tmp_sample_bids (bet_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO _tmp_sample_bids (bet_id) VALUES (?)", [(b,) for b in sample_bids])

        pred_rows = list(
            conn.execute(
                """
                SELECT p.bet_id, p.player_id, p.canonical_id
                FROM prediction_log p
                JOIN _tmp_sample_bids s ON p.bet_id = s.bet_id
                """
            )
        )
    finally:
        conn.close()

    bid_to_player: Dict[str, str] = {}
    player_to_canonical: Dict[str, str] = {}
    ambiguous_players = set()
    for bet_id, player_id, canonical_id in pred_rows:
        if bet_id is None or player_id is None or canonical_id is None:
            continue
        b = str(bet_id)
        p = str(player_id)
        c = str(canonical_id)
        bid_to_player[b] = p
        prev = player_to_canonical.get(p)
        if prev is None:
            player_to_canonical[p] = c
        elif prev != c:
            ambiguous_players.add(p)

    if ambiguous_players:
        raise ValueError(
            f"ambiguous player->canonical mapping detected for {len(ambiguous_players)} player_id(s); "
            "cannot autolabel safely"
        )

    players = sorted(player_to_canonical.keys())
    if not players:
        raise ValueError("No player/canonical mapping found in prediction_log for sample bet_ids")
    if len(players) > max_players:
        raise ValueError(
            f"player set too large ({len(players)} > max_players={max_players}); "
            "narrow window or increase guardrail explicitly"
        )

    # 2) Fetch bet stream from ClickHouse by involved players in chunks
    start_dt = _parse_iso_ts(start_ts).astimezone(HK_TZ).replace(tzinfo=None)
    end_dt = _parse_iso_ts(end_ts).astimezone(HK_TZ).replace(tzinfo=None)
    # Pull a little extra range for label lookahead / terminal determinability.
    pull_start = start_dt - timedelta(minutes=5)
    pull_end = end_dt + timedelta(minutes=config.LABEL_LOOKAHEAD_MIN + config.WALKAWAY_GAP_MIN)

    client = get_clickhouse_client()
    all_bets: List[pd.DataFrame] = []
    if not _IDENT_RE.fullmatch(str(config.SOURCE_DB)) or not _IDENT_RE.fullmatch(str(config.TBET)):
        raise ValueError("invalid SOURCE_DB/TBET identifier")
    tbl = f"{config.SOURCE_DB}.{config.TBET}"
    query = f"""
        SELECT
            bet_id,
            player_id,
            payout_complete_dtm
        FROM {tbl} FINAL
        WHERE payout_complete_dtm >= %(start)s
          AND payout_complete_dtm <= %(end)s
          AND payout_complete_dtm IS NOT NULL
          AND wager > 0
          AND toString(player_id) IN %(player_ids)s
    """
    for chunk in _chunks(players, player_chunk_size):
        _log(f"autolabel: querying ClickHouse chunk (players={len(chunk)})")
        df = client.query_df(
            query,
            parameters={
                "start": pull_start,
                "end": pull_end,
                "player_ids": tuple(chunk),
            },
        )
        if not df.empty:
            all_bets.append(df)

    if not all_bets:
        raise ValueError("No bets fetched from ClickHouse for sampled players")

    bets = pd.concat(all_bets, ignore_index=True)
    bets["player_id"] = bets["player_id"].astype(str)
    bets["canonical_id"] = bets["player_id"].map(player_to_canonical)
    bets = bets.dropna(subset=["canonical_id"]).copy()
    if bets.empty:
        raise ValueError("Fetched bets do not map to canonical_id; cannot compute labels")

    bets["bet_id"] = bets["bet_id"].astype(str)
    _pcd = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce")
    # Keep label computation tz semantics aligned with trainer.labels:
    # normalize to HK local time and drop tz info before comparisons.
    if getattr(_pcd.dt, "tz", None) is None:
        _pcd = _pcd.dt.tz_localize(HK_TZ, ambiguous="NaT", nonexistent="shift_forward")
    else:
        _pcd = _pcd.dt.tz_convert(HK_TZ)
    bets["payout_complete_dtm"] = _pcd.dt.tz_localize(None)
    bets = bets.dropna(subset=["payout_complete_dtm"]).copy()

    # 3) Compute labels with trainer-consistent logic
    labeled = compute_labels(
        bets_df=bets[["canonical_id", "bet_id", "payout_complete_dtm"]],
        window_end=end_dt,
        extended_end=pull_end,
    )
    labeled_sample = labeled[labeled["bet_id"].isin(sample_bid_set)].copy()

    # Keep censored rows visible; downstream evaluate can choose to filter.
    out_labels_csv.parent.mkdir(parents=True, exist_ok=True)
    labeled_sample_out = labeled_sample[["bet_id", "label", "censored"]].copy()
    labeled_sample_out.to_csv(out_labels_csv, index=False)
    _log(f"autolabel: done (labeled_rows={len(labeled_sample_out)}, output={out_labels_csv})")

    n_censored = int(labeled_sample_out["censored"].sum()) if not labeled_sample_out.empty else 0
    n_unmatched = len(sample_bids) - len(labeled_sample_out)

    return {
        "mode": "autolabel",
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "db_path": db_path,
        "sample_csv": str(sample_csv),
        "output_labels_csv": str(out_labels_csv),
        "summary": {
            "n_sample_input": len(sample_bids),
            "n_sample_rows_input": int(n_input_rows),
            "n_unique_bet_id": int(n_unique_bet_id),
            "n_duplicate_bet_id": int(n_duplicate_bet_id),
            "n_players": len(players),
            "n_bets_fetched": int(len(bets)),
            "n_labeled_rows": int(len(labeled_sample_out)),
            "n_censored": n_censored,
            "n_unmatched_sample_bet_id": n_unmatched,
            "player_chunk_size": player_chunk_size,
        },
        "note": (
            "Labels generated via trainer.labels.compute_labels(). "
            "Rows with censored=1 should be excluded from strict evaluation."
        ),
    }


def _precision_recall_at_current_threshold(rows: List[Tuple[float, int, int]]) -> Dict[str, float]:
    # rows: (score, is_alert, label)
    tp = fp = fn = 0
    for _s, pred, y in rows:
        if pred == 1 and y == 1:
            tp += 1
        elif pred == 1 and y == 0:
            fp += 1
        elif pred == 0 and y == 1:
            fn += 1
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return {
        "tp": float(tp),
        "fp": float(fp),
        "fn": float(fn),
        "precision": precision,
        "recall": recall,
    }


def _precision_at_recall_target(rows: List[Tuple[float, int]], target_recall: float) -> Dict[str, float]:
    # rows: (score, label)
    if not rows:
        return {"precision_at_target_recall": 0.0, "threshold_at_target": 0.0, "achieved_recall": 0.0}
    sorted_rows = sorted(rows, key=lambda x: x[0], reverse=True)
    total_pos = sum(y for _, y in sorted_rows)
    if total_pos <= 0:
        return {"precision_at_target_recall": 0.0, "threshold_at_target": sorted_rows[0][0], "achieved_recall": 0.0}

    tp = fp = 0
    best_precision = -1.0
    best_threshold = sorted_rows[-1][0]
    best_recall = 0.0
    for score, label in sorted_rows:
        if label == 1:
            tp += 1
        else:
            fp += 1
        recall = tp / total_pos
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        if recall >= target_recall and precision > best_precision:
            best_precision = precision
            best_threshold = score
            best_recall = recall

    if best_precision < 0.0:
        return {
            "precision_at_target_recall": 0.0,
            "threshold_at_target": sorted_rows[-1][0],
            "achieved_recall": tp / total_pos if total_pos else 0.0,
        }
    return {
        "precision_at_target_recall": best_precision,
        "threshold_at_target": best_threshold,
        "achieved_recall": best_recall,
    }


def _safe_div(numer: float, denom: float) -> float:
    return numer / denom if denom > 0 else 0.0


def _as_float(v: object) -> float:
    return float(cast(float, v))


def _cross_check_alerts_vs_prediction_log(
    pred_db_path: str,
    state_db_path: str,
    start_ts: str,
    end_ts: str,
) -> Dict[str, object]:
    """R2: compare prediction_log is_alert volume vs alerts table (duplicate suppression)."""
    sp = Path(state_db_path)
    if not sp.exists():
        return {"status": "skipped", "reason": f"state DB not found: {state_db_path}"}

    pred_conn = sqlite3.connect(pred_db_path, timeout=10)
    try:
        n_pl = int(
            pred_conn.execute(
                """
                SELECT COUNT(*) FROM prediction_log
                WHERE scored_at >= ? AND scored_at < ?
                  AND is_rated_obs = 1 AND is_alert = 1
                """,
                (start_ts, end_ts),
            ).fetchone()[0]
        )
    finally:
        pred_conn.close()

    st_conn = sqlite3.connect(str(sp), timeout=10)
    try:
        cur = st_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'",
        )
        if cur.fetchone() is None:
            return {"status": "skipped", "reason": "alerts table missing in state DB"}
        n_al = int(
            st_conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE ts >= ? AND ts < ?",
                (start_ts, end_ts),
            ).fetchone()[0]
        )
    finally:
        st_conn.close()

    diff = n_pl - n_al
    return {
        "status": "ok",
        "state_db_path": str(sp),
        "n_prediction_log_is_alert_rows": n_pl,
        "n_alerts_table_rows_ts_window": n_al,
        "difference_pl_minus_alerts": diff,
        "alerts_to_prediction_log_ratio": _safe_div(float(n_al), float(n_pl)) if n_pl else 0.0,
        "note": (
            "Compares counts in the same [start_ts, end_ts) string window on scored_at vs alerts.ts. "
            "Mismatch may reflect duplicate suppression (R2) or timestamp semantics differences."
        ),
    }


def _load_training_metrics_baseline(model_dir: Path) -> Dict[str, object]:
    """R8 / R1 baseline: read trainer-written training_metrics.json when present."""
    path = model_dir / "training_metrics.json"
    if not path.is_file():
        return {
            "status": "skipped",
            "reason": f"training_metrics.json not found under {model_dir}",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "error", "reason": str(exc)}

    baseline: Dict[str, object] = {
        "status": "ok",
        "path": str(path.resolve()),
        "model_version": data.get("model_version"),
        "test_precision_at_recall_0.01": data.get("test_precision_at_recall_0.01"),
        "threshold_at_recall_0.01": data.get("threshold_at_recall_0.01"),
        "test_threshold_uncalibrated": data.get("test_threshold_uncalibrated"),
        "uncalibrated_threshold": data.get("uncalibrated_threshold"),
    }
    rated = data.get("rated")
    if isinstance(rated, dict):
        baseline["rated_threshold"] = rated.get("threshold")
    return baseline


def _build_unified_sample_evaluation(
    pred_db_path: str,
    start_ts: str,
    end_ts: str,
    labels_below: Dict[str, Tuple[int, int]],
    labels_alert: Dict[str, Tuple[int, int]],
    target_recall: float,
) -> Dict[str, object]:
    """
    Merge labeled rows from both stratified branches for a single (score, pred, label) cohort.
    Not an unbiased draw from production — see description field.
    """
    rows_b, c_b = _evaluate_join_rows_from_labels(pred_db_path, start_ts, end_ts, labels_below)
    rows_a, c_a = _evaluate_join_rows_from_labels(pred_db_path, start_ts, end_ts, labels_alert)

    merged: Dict[str, Tuple[str, float, int, int, str]] = {}
    for r in rows_b:
        merged[r[0]] = r
    dup = 0
    for r in rows_a:
        if r[0] in merged:
            dup += 1
        merged[r[0]] = r
    unified = list(merged.values())
    triples: List[Tuple[float, int, int]] = [(s, p, y) for _bid, s, p, y, _mv in unified]

    current = _precision_recall_at_current_threshold(triples)
    pat = _precision_at_recall_target([(s, y) for s, _p, y in triples], target_recall)

    by_mv: Dict[str, List[Tuple[float, int, int]]] = defaultdict(list)
    for _bid, s, p, y, mv in unified:
        by_mv[mv].append((s, p, y))

    per_version: Dict[str, object] = {}
    for mv, trip in sorted(by_mv.items(), key=lambda x: -len(x[1])):
        per_version[mv] = {
            "n_rows": len(trip),
            "current_threshold_metrics": _precision_recall_at_current_threshold(trip),
            "precision_at_recall_target": {
                "target_recall": target_recall,
                **_precision_at_recall_target([(s, y) for s, _p, y in trip], target_recall),
            },
        }

    return {
        "description": (
            "Merged union of stratified below-threshold + stratified alert labeled samples. "
            "NOT an i.i.d. or cohort-unbiased estimate of full production PR — use for unified "
            "score–label diagnostics and model_version splits only."
        ),
        "n_rows_below_branch": len(rows_b),
        "n_rows_alert_branch": len(rows_a),
        "n_duplicate_bet_id_overlap": dup,
        "n_rows_merged_unique": len(unified),
        "n_censored_excluded_below": c_b,
        "n_censored_excluded_alert": c_a,
        "current_threshold_metrics": current,
        "precision_at_recall_target": {"target_recall": target_recall, **pat},
        "by_model_version": per_version,
    }


def run_evaluate_mode(
    db_path: str,
    start_ts: str,
    end_ts: str,
    labels_csv: Path,
    target_recall: float,
) -> Dict[str, object]:
    _log(f"evaluate: starting (db={db_path}, labels_csv={labels_csv}, window={start_ts} -> {end_ts})")
    if target_recall <= 0 or target_recall > 1:
        raise ValueError("target-recall must be in (0, 1]")

    labels = _load_labels_csv(labels_csv)
    if not labels:
        raise ValueError("labels CSV contains no valid (bet_id,label) rows")

    detail_rows, n_censored_excluded = _evaluate_join_rows_from_labels(
        db_path, start_ts, end_ts, labels
    )
    rows: List[Tuple[float, int, int]] = [(s, p, y) for _bid, s, p, y, _mv in detail_rows]

    if not rows:
        raise ValueError("No labeled rows matched prediction_log in the selected window")

    current = _precision_recall_at_current_threshold(rows)
    pat = _precision_at_recall_target([(s, y) for s, _pred, y in rows], target_recall)

    out = {
        "mode": "evaluate",
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "db_path": db_path,
        "labels_csv": str(labels_csv),
        "n_labeled_input": len(labels),
        "n_labeled_matched": len(rows),
        "n_censored_excluded": n_censored_excluded,
        "current_threshold_metrics": current,
        "precision_at_recall_target": {
            "target_recall": target_recall,
            **pat,
        },
        "note": "Compare current_threshold_metrics and precision_at_recall_target against offline test metrics with same definition.",
    }
    _log("evaluate: done")
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run R1/R6 investigation helper.")
    p.add_argument("--mode", choices=["sample", "autolabel", "evaluate", "all"], default="all")
    p.add_argument("--start-ts", default="", help="Window start timestamp (inclusive). Default: dynamic yesterday start in HKT.")
    p.add_argument("--end-ts", default="", help="Window end timestamp (exclusive). Default: dynamic today start in HKT.")
    p.add_argument("--env-file", default="", help="Optional .env file path.")
    p.add_argument("--pred-db-path", default="", help="Override prediction log DB path.")
    p.add_argument("--state-db-path", default="", help="Override STATE_DB_PATH for alerts cross-check (R2).")
    p.add_argument("--model-dir", default="", help="Override MODEL_DIR for training_metrics.json baseline (R1/R8).")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    p.add_argument("--overwrite", action="store_true", help="Allow overwriting existing output CSV files.")

    # sample mode args
    p.add_argument("--sample-size", type=int, default=4000, help="Target below-threshold sample size.")
    p.add_argument(
        "--candidate-filter",
        choices=["below_threshold", "alert", "all_rated"],
        default="below_threshold",
        help="Sample candidate source: below-threshold (`is_alert=0`), alert (`is_alert=1`), or all rated.",
    )
    p.add_argument("--bins", type=int, default=10, help="Number of score bins for stratified sampling.")
    p.add_argument("--seed", type=int, default=42, help="Deterministic seed for reservoir replacement.")
    p.add_argument(
        "--out-csv",
        default="",
        help="Output CSV for sample mode. Default: auto path under snapshots by window date.",
    )

    # evaluate mode args
    p.add_argument("--labels-csv", default="", help="CSV with columns bet_id,label. Default: auto labels path by window date.")
    p.add_argument("--target-recall", type=float, default=DEFAULT_TARGET_RECALL, help="Recall target for precision@recall.")
    p.add_argument(
        "--sample-csv",
        default="",
        help="Input sample CSV (for autolabel mode). Default: auto sample path by window date.",
    )
    p.add_argument(
        "--out-labels-csv",
        default="",
        help="Output labeled CSV path (for autolabel mode). Default: auto labels path by window date.",
    )
    p.add_argument(
        "--player-chunk-size",
        type=int,
        default=200,
        help="ClickHouse player_id IN chunk size for autolabel mode.",
    )
    p.add_argument(
        "--max-players",
        type=int,
        default=5000,
        help="Guardrail: maximum distinct players allowed in autolabel mode.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    _log(f"main: start (mode={args.mode})")
    candidate_filter = getattr(args, "candidate_filter", "below_threshold")
    try:
        start_ts, end_ts = _resolve_window(args.start_ts, args.end_ts)
        start_dt = _parse_iso_ts(start_ts)
        end_dt = _parse_iso_ts(end_ts)
        if start_dt >= end_dt:
            raise ValueError("start-ts must be earlier than end-ts")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    pred_db_path, env_file_used = _resolve_pred_db_path(args.env_file, args.pred_db_path)
    if not pred_db_path:
        print("PREDICTION_LOG_DB_PATH is empty. Provide --pred-db-path or set env/.env.", file=sys.stderr)
        return 2
    if not Path(pred_db_path).exists():
        print(f"prediction log DB not found: {pred_db_path}", file=sys.stderr)
        return 2
    _log(f"main: using prediction_log DB ({pred_db_path})")

    state_db_path, state_env_file_used = _resolve_state_db_path(args.env_file, getattr(args, "state_db_path", ""))
    model_dir_str, model_dir_env_file_used = _resolve_model_dir(args.env_file, getattr(args, "model_dir", ""))
    _log(f"main: resolved state DB ({state_db_path}), model_dir ({model_dir_str})")

    default_sample_csv, default_labels_csv = _default_snapshot_paths(start_ts, end_ts)
    effective_out_csv = Path(args.out_csv) if args.out_csv.strip() else default_sample_csv
    effective_sample_csv = Path(args.sample_csv) if args.sample_csv.strip() else effective_out_csv
    effective_out_labels_csv = Path(args.out_labels_csv) if args.out_labels_csv.strip() else default_labels_csv
    effective_labels_csv = Path(args.labels_csv) if args.labels_csv.strip() else effective_out_labels_csv

    try:
        if args.mode == "sample":
            _log("main: executing step sample")
            payload = run_sample_mode(
                db_path=pred_db_path,
                start_ts=start_ts,
                end_ts=end_ts,
                sample_size=args.sample_size,
                candidate_filter=candidate_filter,
                bins=args.bins,
                seed=args.seed,
                out_csv=effective_out_csv,
                overwrite=args.overwrite,
            )
        elif args.mode == "autolabel":
            _log("main: executing step autolabel")
            payload = run_autolabel_mode(
                db_path=pred_db_path,
                start_ts=start_ts,
                end_ts=end_ts,
                sample_csv=effective_sample_csv,
                out_labels_csv=effective_out_labels_csv,
                player_chunk_size=args.player_chunk_size,
                max_players=args.max_players,
            )
        elif args.mode == "evaluate":
            _log("main: executing step evaluate")
            payload = run_evaluate_mode(
                db_path=pred_db_path,
                start_ts=start_ts,
                end_ts=end_ts,
                labels_csv=effective_labels_csv,
                target_recall=args.target_recall,
            )
        else:
            # all: run two diagnosis branches automatically:
            # - below_threshold (missed positives / FN concentration)
            # - alert (current precision / FP concentration)
            # This keeps one-line UX and avoids requiring extra params.
            branches: Dict[str, Dict[str, object]] = {}
            for branch_name, branch_filter in (
                ("below_threshold", "below_threshold"),
                ("alert", "alert"),
            ):
                _log(f"main: executing all-mode branch {branch_name}")
                try:
                    sample_payload = run_sample_mode(
                        db_path=pred_db_path,
                        start_ts=start_ts,
                        end_ts=end_ts,
                        sample_size=args.sample_size,
                        candidate_filter=branch_filter,
                        bins=args.bins,
                        seed=args.seed,
                        out_csv=effective_out_csv.with_name(
                            f"{effective_out_csv.stem}_{branch_name}{effective_out_csv.suffix}"
                        ),
                        overwrite=getattr(args, "overwrite", False),
                    )
                except Exception as exc:
                    raise RuntimeError(f"all-mode branch '{branch_name}' step 'sample' failed: {exc}") from exc
                try:
                    autolabel_payload = run_autolabel_mode(
                        db_path=pred_db_path,
                        start_ts=start_ts,
                        end_ts=end_ts,
                        sample_csv=Path(str(sample_payload["output_csv"])),
                        out_labels_csv=effective_out_labels_csv.with_name(
                            f"{effective_out_labels_csv.stem}_{branch_name}{effective_out_labels_csv.suffix}"
                        ),
                        player_chunk_size=args.player_chunk_size,
                        max_players=args.max_players,
                    )
                except Exception as exc:
                    raise RuntimeError(f"all-mode branch '{branch_name}' step 'autolabel' failed: {exc}") from exc
                try:
                    evaluate_payload = run_evaluate_mode(
                        db_path=pred_db_path,
                        start_ts=start_ts,
                        end_ts=end_ts,
                        labels_csv=Path(str(autolabel_payload["output_labels_csv"])),
                        target_recall=args.target_recall,
                    )
                except Exception as exc:
                    raise RuntimeError(f"all-mode branch '{branch_name}' step 'evaluate' failed: {exc}") from exc
                branches[branch_name] = {
                    "sample": sample_payload,
                    "autolabel": autolabel_payload,
                    "evaluate": evaluate_payload,
                }

            below_eval = cast(Dict[str, object], branches["below_threshold"]["evaluate"])
            alert_eval = cast(Dict[str, object], branches["alert"]["evaluate"])
            below_current = cast(Dict[str, float], below_eval["current_threshold_metrics"])
            alert_current = cast(Dict[str, float], alert_eval["current_threshold_metrics"])
            below_autolabel = cast(Dict[str, object], branches["below_threshold"]["autolabel"])
            alert_autolabel = cast(Dict[str, object], branches["alert"]["autolabel"])
            below_summary = cast(Dict[str, object], below_autolabel["summary"])
            alert_summary = cast(Dict[str, object], alert_autolabel["summary"])

            below_fn_rate = _safe_div(_as_float(below_current["fn"]), _as_float(below_eval["n_labeled_matched"]))
            alert_fp_rate = _safe_div(
                _as_float(alert_current["fp"]),
                _as_float(alert_current["tp"]) + _as_float(alert_current["fp"]),
            )
            diagnostics = {
                "one_line_intent": "R1/R6 comprehensive run completed with both below-threshold and alert-side analyses.",
                "below_threshold": {
                    "n_labeled_matched": below_eval["n_labeled_matched"],
                    "fn": below_current["fn"],
                    "fn_rate_within_below_threshold_sample": below_fn_rate,
                    "n_unmatched_sample_bet_id": below_summary["n_unmatched_sample_bet_id"],
                },
                "alert": {
                    "n_labeled_matched": alert_eval["n_labeled_matched"],
                    "tp": alert_current["tp"],
                    "fp": alert_current["fp"],
                    "current_threshold_precision": alert_current["precision"],
                    "fp_rate_within_alert_sample": alert_fp_rate,
                    "n_unmatched_sample_bet_id": alert_summary["n_unmatched_sample_bet_id"],
                },
                "interpretation_hints": [
                    "Use alert.current_threshold_precision to diagnose precision drop.",
                    "Use below_threshold.fn_rate_within_below_threshold_sample to quantify missed-positive concentration.",
                    "See unified_sample_evaluation for merged (below+alert) score–label metrics; not an unbiased prod cohort.",
                    "See r2_prediction_log_vs_alerts for duplicate-suppression / count parity signals.",
                    "See training_artifact_baseline for offline test_precision_at_recall_0.01 vs current merged sample.",
                ],
            }

            _log("main: unified merge + R2 cross-check + training artifact baseline")
            labels_below_d = _load_labels_csv(Path(str(below_autolabel["output_labels_csv"])))
            labels_alert_d = _load_labels_csv(Path(str(alert_autolabel["output_labels_csv"])))
            unified_sample_evaluation = _build_unified_sample_evaluation(
                pred_db_path,
                start_ts,
                end_ts,
                labels_below_d,
                labels_alert_d,
                args.target_recall,
            )
            r2_prediction_log_vs_alerts = _cross_check_alerts_vs_prediction_log(
                pred_db_path,
                state_db_path,
                start_ts,
                end_ts,
            )
            training_artifact_baseline = _load_training_metrics_baseline(Path(model_dir_str))

            try:
                # Backward-compat fields point to below-threshold branch.
                payload = {
                    "mode": "all",
                    "sample": branches["below_threshold"]["sample"],
                    "autolabel": branches["below_threshold"]["autolabel"],
                    "evaluate": branches["below_threshold"]["evaluate"],
                    "branches": branches,
                    "diagnostics": diagnostics,
                    "unified_sample_evaluation": unified_sample_evaluation,
                    "r2_prediction_log_vs_alerts": r2_prediction_log_vs_alerts,
                    "training_artifact_baseline": training_artifact_baseline,
                }
                _log("main: all-mode completed")
            except Exception as exc:
                raise RuntimeError(f"all-mode payload assembly failed: {exc}") from exc
    except Exception as exc:
        print(f"R1/R6 script failed: {exc}", file=sys.stderr)
        return 2

    payload["resolution"] = {
        "pred_db_path": pred_db_path,
        "env_file_used": env_file_used,
        "state_db_path": state_db_path,
        "state_env_file_used": state_env_file_used,
        "model_dir": model_dir_str,
        "model_dir_env_file_used": model_dir_env_file_used,
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "mode": args.mode,
        "effective_paths": {
            "out_csv": str(effective_out_csv),
            "sample_csv": str(effective_sample_csv),
            "out_labels_csv": str(effective_out_labels_csv),
            "labels_csv": str(effective_labels_csv),
        },
    }

    if args.pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False))
    _log("main: finished successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

