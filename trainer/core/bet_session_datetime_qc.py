"""Sanity checks: ``t_bet.payout_complete_dtm`` vs ``t_session`` window.

Used to guard against mixed timezones / inconsistent raw timestamps across
tables. Normalisation mirrors ``trainer.training.feature_pipeline.apply_dq``
(R23: naive → HK localize, aware → HK convert, then tz-naive ``datetime64[ns]``).

**Resource note:** This joins bets to sessions in memory. For production-scale
raw tables, pass a time-bounded slice or stratified sample (for example a few
``gaming_day`` partitions) to avoid OOM on a laptop.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from trainer.core import _config_training_domain as _tdom
from trainer.identity import _fnd01_dedup_pandas


def _hk_zone(hk_zone: Optional[ZoneInfo]) -> ZoneInfo:
    """Resolve HK ``ZoneInfo``, defaulting from ``_config_training_domain``."""
    if hk_zone is not None:
        return hk_zone
    return ZoneInfo(getattr(_tdom, "HK_TZ", "Asia/Hong_Kong"))


def _r23_hk_naive_ns(series: pd.Series, hk: ZoneInfo) -> pd.Series:
    """Match ``apply_dq`` payout/session coercion: HK-local wall clock, ns naive."""
    ts = pd.to_datetime(series, utc=False, errors="coerce")
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(hk, nonexistent="shift_forward", ambiguous="NaT")
    else:
        ts = ts.dt.tz_convert(hk)
    return ts.dt.tz_localize(None).astype("datetime64[ns]")


def _require_columns(df: pd.DataFrame, cols: tuple[str, ...], label: str) -> None:
    """Raise if ``df`` is missing any of ``cols``."""
    miss = [c for c in cols if c not in df.columns]
    if miss:
        raise ValueError(f"{label} missing required columns: {miss}")


def summarize_bet_payout_vs_session_window(
    bets: pd.DataFrame,
    sessions: pd.DataFrame,
    *,
    hk_zone: Optional[ZoneInfo] = None,
    dedupe_sessions: bool = True,
) -> Dict[str, Any]:
    """Count bets whose payout time is outside the parent session window.

    A bet is **in window** when ``session_start_dtm <= payout_complete_dtm <= session_end_dtm``
    (inclusive), after R23 normalisation on both sides. Rows with null payout,
    null session bounds, inverted ``start > end``, or unknown ``session_id`` are
    reported separately and are **not** counted as in-window violations.

    Parameters
    ----------
    bets:
        Raw or normalised bets; must include ``session_id``, ``payout_complete_dtm``.
    sessions:
        Raw ``t_session`` rows; must include ``session_id``, ``session_start_dtm``,
        ``session_end_dtm``. When ``dedupe_sessions`` is True, ``lud_dtm`` is
        required for FND-01 dedup (same as pipeline).
    hk_zone:
        Optional ``ZoneInfo``; defaults to ``trainer.core._config_training_domain.HK_TZ``.
    dedupe_sessions:
        If True, keep latest row per ``session_id`` via ``_fnd01_dedup_pandas``.

    Returns
    -------
    dict
        Counts including ``n_violations`` (strictly outside the closed interval).
    """
    _require_columns(bets, ("session_id", "payout_complete_dtm"), "bets")
    _require_columns(
        sessions,
        ("session_id", "session_start_dtm", "session_end_dtm"),
        "sessions",
    )
    hk = _hk_zone(hk_zone)

    if dedupe_sessions and "lud_dtm" not in sessions.columns:
        raise ValueError("sessions missing lud_dtm (required when dedupe_sessions=True)")

    sess = _fnd01_dedup_pandas(sessions) if dedupe_sessions else sessions.drop_duplicates(
        subset=["session_id"], keep="last"
    )
    sess = sess[
        ["session_id", "session_start_dtm", "session_end_dtm"]
    ].copy()

    b = bets[["session_id", "payout_complete_dtm"]].copy()
    b["session_id"] = pd.to_numeric(b["session_id"], errors="coerce")
    sess["session_id"] = pd.to_numeric(sess["session_id"], errors="coerce")

    s_start = _r23_hk_naive_ns(sess["session_start_dtm"], hk)
    s_end = _r23_hk_naive_ns(sess["session_end_dtm"], hk)
    sess["_s_start"] = s_start.to_numpy()
    sess["_s_end"] = s_end.to_numpy()

    m = b.merge(sess, on="session_id", how="left", indicator=True)

    n_bets = int(len(m))
    sid = pd.to_numeric(m["session_id"], errors="coerce")
    missing_session = (m["_merge"] == "left_only") & sid.notna()

    payout = _r23_hk_naive_ns(m["payout_complete_dtm"], hk)
    null_payout = payout.isna()
    null_start = m["_s_start"].isna()
    null_end = m["_s_end"].isna()
    inverted = m["_s_start"].notna() & m["_s_end"].notna() & (m["_s_start"] > m["_s_end"])

    evaluable = (
        ~missing_session
        & ~null_payout
        & ~null_start
        & ~null_end
        & ~inverted
    )
    in_low = payout >= m["_s_start"]
    in_high = payout <= m["_s_end"]
    in_window = evaluable & in_low & in_high
    n_violations = int((evaluable & ~in_window).sum())

    return {
        "n_bets": n_bets,
        "n_missing_session": int(missing_session.sum()),
        "n_skipped_null_payout": int(null_payout.sum()),
        "n_skipped_null_session_start": int((~missing_session & null_start).sum()),
        "n_skipped_null_session_end": int((~missing_session & null_end).sum()),
        "n_skipped_inverted_session_window": int(inverted.sum()),
        "n_evaluable": int(evaluable.sum()),
        "n_in_window": int(in_window.sum()),
        "n_violations": n_violations,
        "violation_rate": (n_violations / int(evaluable.sum())) if evaluable.any() else 0.0,
    }


def assert_bet_payout_within_session_or_raise(
    bets: pd.DataFrame,
    sessions: pd.DataFrame,
    *,
    hk_zone: Optional[ZoneInfo] = None,
    dedupe_sessions: bool = True,
    max_violations_to_print: int = 5,
) -> None:
    """Raise ``AssertionError`` if any evaluable bet is outside its session window."""
    summary = summarize_bet_payout_vs_session_window(
        bets,
        sessions,
        hk_zone=hk_zone,
        dedupe_sessions=dedupe_sessions,
    )
    if summary["n_violations"] == 0:
        return
    extra = ""
    if max_violations_to_print > 0:
        extra = _sample_violation_lines(
            bets,
            sessions,
            hk=_hk_zone(hk_zone),
            dedupe_sessions=dedupe_sessions,
            limit=max_violations_to_print,
        )
    raise AssertionError(
        "bet payout vs session window: "
        f"n_violations={summary['n_violations']}, summary={summary}.{extra}"
    )


def _sample_violation_lines(
    bets: pd.DataFrame,
    sessions: pd.DataFrame,
    *,
    hk: ZoneInfo,
    dedupe_sessions: bool,
    limit: int,
) -> str:
    """Return a short string of example violating rows for error messages."""
    sess = _fnd01_dedup_pandas(sessions) if dedupe_sessions else sessions.drop_duplicates(
        subset=["session_id"], keep="last"
    )
    cols = ["session_id", "payout_complete_dtm"]
    if "bet_id" in bets.columns:
        cols = ["bet_id"] + cols
    b = bets[cols].copy()
    b["session_id"] = pd.to_numeric(b["session_id"], errors="coerce")
    sess = sess[["session_id", "session_start_dtm", "session_end_dtm"]].copy()
    sess["session_id"] = pd.to_numeric(sess["session_id"], errors="coerce")

    payout = _r23_hk_naive_ns(b["payout_complete_dtm"], hk)
    m = b.assign(_payout=payout).merge(sess, on="session_id", how="left")
    m["_s_start"] = _r23_hk_naive_ns(m["session_start_dtm"], hk)
    m["_s_end"] = _r23_hk_naive_ns(m["session_end_dtm"], hk)

    evaluable = (
        m["_s_start"].notna()
        & m["_s_end"].notna()
        & m["_payout"].notna()
        & (m["_s_start"] <= m["_s_end"])
    )
    bad = evaluable & (
        (m["_payout"] < m["_s_start"]) | (m["_payout"] > m["_s_end"])
    )
    if not bad.any():
        return ""
    samp = m.loc[bad].head(limit)
    lines = [f" sample_row={row.to_dict()}" for _, row in samp.iterrows()]
    return "".join(lines)
