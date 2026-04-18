"""自 trainer chunk Parquet 匯出 baseline 可讀切片（欄位對齊 F2 契約）。"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="從 trainer chunk 讀取列、dropna、寫入 Parquet 供 baseline run 使用。"
    )
    p.add_argument("--chunk", required=True, type=Path, help="trainer chunk 之 .parquet 路徑")
    p.add_argument("--out", required=True, type=Path, help="輸出 .parquet 路徑")
    p.add_argument(
        "--max-rows",
        type=int,
        default=200_000,
        help="輸出列數上限（依 payout_complete_dtm 排序後取前綴；必要欄 dropna 後）",
    )
    p.add_argument(
        "--bet-time-start",
        type=str,
        default=None,
        help="可選：僅保留 payout_complete_dtm >= 此 ISO 時間（與 training_provenance.data_window 對齊）",
    )
    p.add_argument(
        "--bet-time-end",
        type=str,
        default=None,
        help="可選：僅保留 payout_complete_dtm < 此 ISO 時間（半開區間）",
    )
    return p.parse_args()


def _time_mask(series: pd.Series, start: str | None, end: str | None) -> pd.Series:
    """回傳布林遮罩：``[start, end)``（``None`` 表示不限制）。"""
    ts = pd.to_datetime(series, errors="coerce")
    m = pd.Series(True, index=series.index)
    if start is not None and str(start).strip():
        m &= ts >= pd.Timestamp(start)
    if end is not None and str(end).strip():
        m &= ts < pd.Timestamp(end)
    return m


def _build_slice(
    chunk: Path,
    max_rows: int,
    bet_time_start: str | None,
    bet_time_end: str | None,
) -> pd.DataFrame:
    """讀取 chunk、捨棄特徵缺值列、組出 baseline 契約欄名。"""
    cols = [
        "payout_complete_dtm",
        "label",
        "censored",
        "pace_drop_ratio",
        "player_win_sum_30d",
        "wager",
        "theo_win_sum_30d",
        "theo_win_sum_180d",
        "active_days_30d",
        "sessions_30d",
    ]
    required = [
        "pace_drop_ratio",
        "player_win_sum_30d",
        "wager",
        "theo_win_sum_30d",
        "active_days_30d",
        "sessions_30d",
    ]
    pf = pq.ParquetFile(chunk)
    parts: list[pd.DataFrame] = []
    for rg in range(pf.num_row_groups):
        raw = pf.read_row_group(rg, columns=cols).to_pandas()
        clean = raw.dropna(subset=required)
        if not clean.empty:
            tm = _time_mask(clean["payout_complete_dtm"], bet_time_start, bet_time_end)
            clean = clean.loc[tm].copy()
        if clean.empty:
            continue
        parts.append(clean)
        cat = pd.concat(parts, ignore_index=True).sort_values(
            "payout_complete_dtm", kind="mergesort"
        )
        if len(cat) < max_rows:
            continue
        head = cat.iloc[:max_rows].copy()
        n_tr = max(1, int(len(head) * 0.7))
        if head["label"].nunique() >= 2 and head.iloc[:n_tr]["label"].nunique() >= 2:
            df = head
            break
    else:
        if not parts:
            raise ValueError(
                f"無任何列通過 dropna（required={required!r}）: chunk={chunk!r}"
            )
        cat = pd.concat(parts, ignore_index=True).sort_values(
            "payout_complete_dtm", kind="mergesort"
        )
        if len(cat) < max_rows:
            raise ValueError(
                f"乾淨列僅 {len(cat)} < {max_rows}；請換 chunk 或降低 --max-rows"
            )
        df = cat.iloc[:max_rows].copy()
        n_tr = max(1, int(len(df) * 0.7))
        if df["label"].nunique() < 2 or df.iloc[:n_tr]["label"].nunique() < 2:
            raise ValueError(
                "時序前綴內無法取得 train 雙類別（0 與 1）；請換含負例之窗的 chunk "
                "或加大 --max-rows。"
            )
    return pd.DataFrame(
        {
            "bet_time": df["payout_complete_dtm"],
            "label": df["label"].astype("int8"),
            "censored": df["censored"].astype(bool),
            "pace_drop_ratio": df["pace_drop_ratio"].astype("float64"),
            "loss_proxy_net": df["player_win_sum_30d"].astype("float64"),
            "loss_proxy_wager": df["wager"].astype("float64"),
            "theo_win_sum_30d": df["theo_win_sum_30d"].astype("float64"),
            "theo_win_sum_180d": df["theo_win_sum_180d"].astype("float64"),
            "active_days_30d": df["active_days_30d"].astype("float64"),
            "sessions_30d": df["sessions_30d"].astype("float64"),
            "current_session_theo": df["wager"].astype("float64"),
            "smoke_score": df["pace_drop_ratio"].astype("float64"),
        }
    )


def main() -> None:
    """CLI 入口。"""
    ns = _parse_args()
    chunk = ns.chunk.resolve()
    out = ns.out.resolve()
    if not chunk.is_file():
        raise FileNotFoundError(f"chunk 不存在: {chunk!r}")
    frame = _build_slice(
        chunk,
        int(ns.max_rows),
        ns.bet_time_start,
        ns.bet_time_end,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out, index=False)
    print(f"wrote {len(frame)} rows -> {out}")


if __name__ == "__main__":
    main()
