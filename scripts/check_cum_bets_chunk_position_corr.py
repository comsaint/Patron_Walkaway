#!/usr/bin/env python3
"""Quick check: corr(cum_bets_proxy, minutes_since_chunk_start) on a bet Parquet slice.

Mimics trainer semantics *approximately*:
  - partition: ``canonical_id`` if present else ``player_id``
  - sort: ``payout_complete_dtm``, ``bet_id`` (string)
  - load lower bound: ``chunk_window_start - buffer_days`` (``HISTORY_BUFFER_DAYS`` analogue)
  - upper bound: ``extended_end`` (default ``window_end + max(1d, label_slop)``)

This is a **sanity probe**, not a full trainer parity repro (no DQ / rated prune / PIT).

Usage (repo root)::

    python scripts/check_cum_bets_chunk_position_corr.py \\
      --bet-parquet data/gmwds_t_bet.parquet \\
      --chunk-start 2026-03-01 \\
      --buffer-days 2

Optional: write the high-risk feature markdown only::

    python scripts/check_cum_bets_chunk_position_corr.py --doc-only --emit-risk-doc
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _next_month_start(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _parse_date(s: str) -> date:
    s = str(s).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        y, m, dd = map(int, s.split("-"))
        return date(y, m, dd)
    raise ValueError(f"expected YYYY-MM-DD, got {s!r}")


def _pearson(x: list[float], y: list[float]) -> tuple[float, float]:
    """Return (r, n). Empty / degenerate -> (nan, n)."""
    n = len(x)
    if n < 3:
        return float("nan"), n
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    dx = math.sqrt(sum((xi - mx) ** 2 for xi in x))
    dy = math.sqrt(sum((yi - my) ** 2 for yi in y))
    if dx == 0.0 or dy == 0.0:
        return float("nan"), n
    return num / (dx * dy), n


def _spearman_rank(values: list[float]) -> list[float]:
    """Average ranks for ties; 1-based ranks."""
    n = len(values)
    indexed = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def _spearman(x: list[float], y: list[float]) -> tuple[float, float]:
    n = len(x)
    if n < 3:
        return float("nan"), n
    rx = _spearman_rank(x)
    ry = _spearman_rank(y)
    return _pearson(rx, ry)[0], n


def _emit_chunk_dependent_doc(path: Path) -> None:
    """Parse ``feature_spec.yaml`` and write a static risk note + auto table."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as e:
        raise SystemExit(f"PyYAML required for --emit-risk-doc: {e}") from e

    spec_path = _repo_root() / "trainer" / "feature_spec" / "feature_spec.yaml"
    spec: dict[str, Any] = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    llm = (spec.get("track_llm") or {}).get("candidates") or []

    rows_max: list[tuple[str, str, str]] = []
    rows_row: list[tuple[str, str, str, str]] = []
    rows_range: list[tuple[str, str, str]] = []
    derived_touch: list[tuple[str, str, str]] = []

    seed_ids = {"cum_bets", "cum_wager", "avg_wager_sofar"}

    for c in llm:
        fid = str(c.get("feature_id") or "")
        ftype = str(c.get("type") or "")
        wf = str(c.get("window_frame") or "")
        deps = c.get("depends_on") or []
        dep_s = ",".join(str(d) for d in deps) if deps else ""

        if ftype == "window" and "UNBOUNDED PRECEDING" in wf.upper():
            rows_max.append((fid, wf, str(c.get("description") or "")[:80]))
            continue
        m = re.search(
            r"ROWS\s+BETWEEN\s+(\d+)\s+PRECEDING\s+AND\s+CURRENT\s+ROW",
            wf,
            flags=re.IGNORECASE,
        )
        if ftype == "window" and m:
            rows_row.append((fid, m.group(1), wf, str(c.get("description") or "")[:80]))
            continue
        if ftype == "window" and "RANGE BETWEEN" in wf.upper() and "INTERVAL" in wf.upper():
            rows_range.append((fid, wf, str(c.get("description") or "")[:80]))
            continue
        if ftype == "derived" and deps:
            hits = [d for d in deps if str(d) in seed_ids or str(d).startswith("cum_")]
            if hits:
                derived_touch.append((fid, dep_s, str(c.get("description") or "")[:80]))

    lines: list[str] = []
    lines.append("# Track LLM：chunk / slice 敏感特徵（高風險清單）\n")
    lines.append(
        "本文件由 `scripts/check_cum_bets_chunk_position_corr.py --emit-risk-doc` 產生；"
            "依 `trainer/feature_spec/feature_spec.yaml` 靜態掃描。\n"
    )
    lines.append("## 分級說明\n")
    lines.append(
        "- **A（嚴重）**：`ROWS … UNBOUNDED PRECEDING` — 值域強烈依賴「本 chunk 載入表內從第一筆算起」，"
        "易與「距離 chunk 起點多久」共線。\n"
    )
    lines.append(
        "- **B（中高）**：`ROWS BETWEEN k PRECEDING` — 只看本表內最近 k **列**；"
        "在 chunk 開頭可用歷史不足，語意隨切片變形。\n"
    )
    lines.append(
        "- **C（輕度）**：`RANGE BETWEEN INTERVAL …` — 仍以事件時間定窗，"
        "但在 chunk 最前段「實際可回溯的時間長度」較短，邊界有弱敏感。\n"
    )
    lines.append(
        "- **D（衍生連鎖）**：`depends_on` 直接依 `cum_bets` / `cum_wager` / `avg_wager_sofar` 者，"
        "繼承 A 類風險。\n"
    )
    lines.append("\n## A — UNBOUNDED PRECEDING（window）\n")
    lines.append("| feature_id | window_frame | description (trunc) |\n")
    lines.append("|------------|--------------|------------------------|\n")
    for fid, wf, desc in sorted(rows_max, key=lambda t: t[0]):
        lines.append(f"| `{fid}` | `{wf}` | {desc} |\n")
    lines.append("\n## B — ROWS k PRECEDING（window）\n")
    lines.append("| feature_id | k | window_frame | description (trunc) |\n")
    lines.append("|------------|---|----------------|------------------------|\n")
    for fid, k, wf, desc in sorted(rows_row, key=lambda t: t[0]):
        lines.append(f"| `{fid}` | {k} | `{wf}` | {desc} |\n")
    lines.append("\n## C — RANGE INTERVAL（window）\n")
    lines.append("| feature_id | window_frame | description (trunc) |\n")
    lines.append("|------------|--------------|------------------------|\n")
    for fid, wf, desc in sorted(rows_range, key=lambda t: t[0]):
        lines.append(f"| `{fid}` | `{wf}` | {desc} |\n")
    lines.append("\n## D — derived 依賴 cum_* / avg_wager_sofar\n")
    lines.append("| feature_id | depends_on | description (trunc) |\n")
    lines.append("|------------|------------|------------------------|\n")
    for fid, dep_s, desc in sorted(derived_touch, key=lambda t: t[0]):
        lines.append(f"| `{fid}` | `{dep_s}` | {desc} |\n")

    lines.append("\n## 備註\n")
    lines.append(
        "- `track_human`（例如 run boundary）也可能有 lookback/chunk 語意；本清單**僅掃 track_llm**。\n"
    )
    lines.append(
        "- 實際訓練仍以 `trainer.training.process_chunk` 載入邊界 + DQ + identity 為準；"
        "本檔用於設計/物化前的風險盤點。\n"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    print(f"[emit-risk-doc] wrote {path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bet-parquet",
        type=Path,
        default=_repo_root() / "data" / "gmwds_t_bet.parquet",
        help="Path to gmwds_t_bet.parquet",
    )
    parser.add_argument(
        "--chunk-start",
        type=str,
        default="",
        help="Core chunk window_start (YYYY-MM-DD). Required unless --doc-only",
    )
    parser.add_argument(
        "--doc-only",
        action="store_true",
        help="Only write --emit-risk-doc markdown and exit",
    )
    parser.add_argument(
        "--window-end",
        type=str,
        default="",
        help="Exclusive core window end (YYYY-MM-DD). Default: first day of next month after chunk-start",
    )
    parser.add_argument("--buffer-days", type=int, default=2, help="Analogue of HISTORY_BUFFER_DAYS (default 2)")
    parser.add_argument(
        "--extended-slop-days",
        type=int,
        default=2,
        help="extended_end = window_end + this many days (trainer uses max(LABEL_LOOKAHEAD,1d); default 2)",
    )
    parser.add_argument("--max-rows", type=int, default=400_000, help="Max rows after filter (safety cap)")
    parser.add_argument(
        "--partition",
        choices=("auto", "canonical_id", "player_id"),
        default="auto",
        help="Partition key for cum proxy",
    )
    parser.add_argument(
        "--core-only",
        action="store_true",
        help="Restrict correlation rows to payout_complete_dtm in [window_start, window_end)",
    )
    parser.add_argument(
        "--emit-risk-doc",
        type=Path,
        nargs="?",
        const=_repo_root() / "trainer" / "feature_spec" / "chunk_dependent_high_risk_features.md",
        help="Write markdown risk list; default path under trainer/feature_spec/",
    )
    args = parser.parse_args()

    if args.doc_only:
        if args.emit_risk_doc is None:
            print("--doc-only requires --emit-risk-doc", file=sys.stderr)
            return 2
        _emit_chunk_dependent_doc(args.emit_risk_doc)
        return 0

    if not str(args.chunk_start).strip():
        parser.error("--chunk-start is required unless --doc-only")

    bet_path: Path = args.bet_parquet.expanduser().resolve()
    if not bet_path.is_file():
        print(f"Missing bet parquet: {bet_path}", file=sys.stderr)
        return 2

    ws_d = _parse_date(args.chunk_start)
    if str(args.window_end).strip():
        we_d = _parse_date(str(args.window_end).strip())
    else:
        we_d = _next_month_start(ws_d)
    if we_d <= ws_d:
        print("window_end must be after chunk-start", file=sys.stderr)
        return 2

    window_start = datetime(ws_d.year, ws_d.month, ws_d.day)
    window_end = datetime(we_d.year, we_d.month, we_d.day)
    extended_end = window_end + timedelta(days=int(args.extended_slop_days))
    bets_lo = window_start - timedelta(days=int(args.buffer_days))

    import numpy as np
    import pandas as pd
    import pyarrow.parquet as pq

    schema_names = set(pq.read_schema(str(bet_path)).names)
    need = ["bet_id", "payout_complete_dtm"]
    for c in need:
        if c not in schema_names:
            print(f"Parquet missing column {c!r}; have={sorted(schema_names)[:40]}", file=sys.stderr)
            return 2

    part = args.partition
    if part == "auto":
        part = "canonical_id" if "canonical_id" in schema_names else "player_id"
    if part not in schema_names:
        print(f"Partition column {part!r} not in parquet schema", file=sys.stderr)
        return 2

    cols = [part, "bet_id", "payout_complete_dtm"]
    if "gaming_day" in schema_names:
        cols.append("gaming_day")

    # Stream batches until a row cap (approx) to avoid loading entire huge Parquet.
    import pyarrow as pa
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(str(bet_path))
    batch_list: list[Any] = []
    total = 0
    batch_size = 65_536
    read_cap = min(int(args.max_rows) * 4, pf.metadata.num_rows)
    for batch in pf.iter_batches(batch_size=batch_size, columns=cols):
        batch_list.append(batch)
        total += batch.num_rows
        if total >= read_cap:
            break
    df = pa.Table.from_batches(batch_list).to_pandas()
    if "gaming_day" in df.columns:
        try:
            gdc = pd.to_datetime(df["gaming_day"], errors="coerce").dt.date
            mask_gd = (gdc >= bets_lo.date()) & (gdc <= extended_end.date())
            df = df.loc[mask_gd.fillna(False)].copy()
        except Exception:
            pass
    if df.empty:
        print("No rows after gaming_day filter; widen dates or check file", file=sys.stderr)
        return 2

    ts = pd.to_datetime(df["payout_complete_dtm"], errors="coerce")
    df = df.assign(_ts=ts).dropna(subset=["_ts"]).copy()
    df["_ts"] = df["_ts"].dt.tz_localize(None) if df["_ts"].dt.tz is not None else df["_ts"]

    ws = window_start
    we = window_end
    ee = extended_end
    mask = (df["_ts"] >= pd.Timestamp(bets_lo)) & (df["_ts"] < pd.Timestamp(ee))
    df = df.loc[mask].copy()
    if len(df) > int(args.max_rows):
        df = df.sample(n=int(args.max_rows), random_state=0).sort_values([part, "_ts", "bet_id"])

    df["bet_id_str"] = df["bet_id"].astype(str)
    df = df.sort_values([part, "_ts", "bet_id_str"], kind="mergesort")
    # Trainer uses COUNT(*) -> 1-based running count within partition
    df["cum_bets_proxy"] = df.groupby(part, sort=False).cumcount() + 1

    df["minutes_since_chunk_start"] = (df["_ts"] - pd.Timestamp(ws)).dt.total_seconds() / 60.0

    if args.core_only:
        core = (df["_ts"] >= pd.Timestamp(ws)) & (df["_ts"] < pd.Timestamp(we))
        dfc = df.loc[core]
    else:
        dfc = df

    x = dfc["cum_bets_proxy"].astype(float).tolist()
    y = dfc["minutes_since_chunk_start"].astype(float).tolist()
    r_p, n = _pearson(x, y)
    r_s, _ = _spearman(x, y)

    print(
        f"[check] bet_parquet={bet_path.name} partition={part} rows={len(dfc)} "
        f"load=[{bets_lo.isoformat()}, {ee.isoformat()}) core=[{ws.isoformat()}, {we.isoformat()}) "
        f"core_only={bool(args.core_only)}",
        flush=True,
    )
    print(f"[check] pearson(cum_bets_proxy, minutes_since_chunk_start) = {r_p:.4f} (n={n})", flush=True)
    print(f"[check] spearman(cum_bets_proxy, minutes_since_chunk_start) = {r_s:.4f} (n={n})", flush=True)

    # Decile diagnostic: mean cum within each decile of minutes
    if n >= 30:
        qs = np.linspace(0, 1, 11)
        edges = dfc["minutes_since_chunk_start"].quantile(qs).values
        print("[check] mean cum_bets_proxy by minutes decile (bucket by quantile):", flush=True)
        for i in range(10):
            lo, hi = edges[i], edges[i + 1]
            sl = dfc[(dfc["minutes_since_chunk_start"] >= lo) & (dfc["minutes_since_chunk_start"] <= hi)]
            if len(sl) == 0:
                continue
            print(
                f"  D{i+1}: minutes[{lo:.1f},{hi:.1f}] n={len(sl)} mean_cum={sl['cum_bets_proxy'].mean():.2f}",
                flush=True,
            )

    if args.emit_risk_doc is not None:
        _emit_chunk_dependent_doc(args.emit_risk_doc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
