"""MVP orchestrator: preprocess (rated-only) -> run_fact -> trip_fact under ``gaming_ym`` layout.

Optional CLI flags are documented below and in ``parallel_lda_mvp/README.md``.

Preprocess 仍透過既有 CLI；``run_fact`` / ``trip_fact`` 於行程內呼叫
``pipelines.layered_data_assets``（單月 staging 一次、span 一次 trip 框架）。

Run from repo root:

    python -m parallel_lda_mvp.run_mvp

Optional flags:

- ``--emit-trainer-local-parquet`` — after MVP, write ``data/mvp_trainer_bridge/`` Parquet
  (``trainer_bridge_{bet,session}.parquet``; Phase C L1 join when snapshot has ``run_fact`` parts)
  plus ``trainer_local_parquet_bridge.manifest.json`` (does not overwrite L0 ``data/gmwds_t_*.parquet``).
- ``--trainer-bridge-emit-only`` — only run the bridge (no preprocess/run/trip). Requires
  ``--snapshot-id <snap>`` **or** ``PARALLEL_LDA_MVP_SNAPSHOT_ID`` pointing at an existing
  ``data/parallel_lda_mvp/<snap>/`` tree with ``mvp_summary.json``.

Idempotency / RAM (bridge): skip unchanged rebuilds when
``trainer.config.TRAINER_BRIDGE_SKIP_IF_UNCHANGED`` is True (default); DuckDB cap via
``PARALLEL_LDA_BRIDGE_DUCKDB_MEMORY_LIMIT`` (e.g. ``4GB``).

Defaults (first match wins for paths):

- ``t_bet``: ``PARALLEL_LDA_MVP_T_BET`` if set, else ``data/gmwds_t_bet.parquet``, else all
  ``data/l0_layered/*/t_bet/**/*.parquet`` (sorted).
- ``t_session``: ``PARALLEL_LDA_MVP_T_SESSION`` if set, else ``data/gmwds_t_session.parquet``.
- ``t_session`` mapping ingest cap: ``PARALLEL_LDA_MVP_SESSION_INGEST_DISABLE`` (passthrough raw),
  ``PARALLEL_LDA_MVP_SESSION_INGEST_REGISTRY`` (override YAML path); see ``session_for_mapping``.
- ``gaming_ym``: ``PARALLEL_LDA_MVP_GAMING_YM`` if set (single month only), else **every
  distinct calendar month** present in ``gaming_day`` across resolved ``t_bet`` (sorted).
- ``source_snapshot_id``: ``PARALLEL_LDA_MVP_SNAPSHOT_ID`` if set, else parent folder after
  ``l0_layered/`` when path matches, else ``snap_mvp_<sha16>`` from bet path fingerprints.
- ``cutoff_dtm``: ``PARALLEL_LDA_MVP_CUTOFF_DTM`` if set (ISO), else last microsecond of the
  **last** ``gaming_ym`` in the resolved span in ``Asia/Hong_Kong`` (one cutoff for eligible).
- ``PARALLEL_LDA_MVP_FORCE_RECOMPUTE``: if ``1`` / ``true``, bypass **month-level** skip (re-run preprocess,
  split, run_fact, trip for each ``gaming_ym``). Also forces **full-span** month-bet fingerprint
  recompute (not only tail months). Ad-hoc or scheduled full rerun when upstream or cache policy
  changed; see ``force_recompute_used`` and ``month_bet_sha_fingerprint_algo`` / ``month_bet_sha_strategy``
  in ``mvp_summary`` (``rolling_keys_plus_core_v1`` vs ``legacy_extract_sha_v1``).

Per-day ``run_fact`` / ``trip`` concurrency is set in code: ``DAY_MATERIALIZE_MAX_WORKERS`` in this module.

Trip materialization runs **after** all months have ``run_fact`` outputs, using **full-span**
``run_fact`` inputs plus ``--coverage-end`` on the last span calendar day (see README).

``python -m parallel_lda_mvp.run_mvp -h`` / ``--help`` prints this text.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

_ENV_T_BET = "PARALLEL_LDA_MVP_T_BET"
_ENV_T_SESSION = "PARALLEL_LDA_MVP_T_SESSION"
_ENV_GAMING_YM = "PARALLEL_LDA_MVP_GAMING_YM"
_ENV_SNAPSHOT_ID = "PARALLEL_LDA_MVP_SNAPSHOT_ID"
_ENV_CUTOFF = "PARALLEL_LDA_MVP_CUTOFF_DTM"
_ENV_FORCE_RECOMPUTE = "PARALLEL_LDA_MVP_FORCE_RECOMPUTE"

# Max concurrent subprocesses for each per-day phase (run_fact, then trip) within one gaming_ym.
# Effective workers = min(days in month, this cap, cpu_count). Tune here (not via env).
DAY_MATERIALIZE_MAX_WORKERS = 1


def repo_root() -> Path:
    """Return repository root (parent of ``parallel_lda_mvp`` package)."""
    return Path(__file__).resolve().parent.parent


def _parse_cutoff_dtm(raw: str) -> datetime:
    """Parse ISO datetime (accept trailing ``Z``)."""
    text = str(raw).strip()
    if not text:
        raise ValueError("cutoff_dtm must be non-empty")
    norm = text[:-1] + "+00:00" if text.endswith("Z") else text
    return datetime.fromisoformat(norm)


def gaming_days_in_month(gaming_ym: str) -> list[str]:
    """Return ``YYYY-MM-DD`` for each calendar day in ``gaming_ym`` (``YYYY-MM``)."""
    s = gaming_ym.strip()
    if not re.fullmatch(r"\d{4}-\d{2}", s):
        raise ValueError(f"gaming_ym must be YYYY-MM, got {gaming_ym!r}")
    y_str, m_str = s.split("-")
    y, m = int(y_str), int(m_str)
    last = calendar.monthrange(y, m)[1]
    return [f"{y:04d}-{m:02d}-{d:02d}" for d in range(1, last + 1)]


def default_cutoff_for_gaming_ym(gaming_ym: str) -> datetime:
    """Return last representable instant of ``gaming_ym`` in Asia/Hong_Kong."""
    y, mo = map(int, gaming_ym.split("-"))
    last = calendar.monthrange(y, mo)[1]
    tz = ZoneInfo("Asia/Hong_Kong")
    return datetime(y, mo, last, 23, 59, 59, 999999, tzinfo=tz)


def resolve_t_bet_paths(data_root: Path) -> list[Path]:
    """Resolve ``t_bet`` Parquet list from env or conventional locations."""
    raw = os.environ.get(_ENV_T_BET, "").strip()
    if raw:
        p = Path(raw).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"{_ENV_T_BET}={raw!r} is not a file")
        return [p]
    single = (data_root / "gmwds_t_bet.parquet").resolve()
    if single.is_file():
        return [single]
    parts = sorted(data_root.glob("l0_layered/*/t_bet/**/*.parquet"))
    if parts:
        return [x.resolve() for x in parts]
    raise FileNotFoundError(
        f"No t_bet inputs: set {_ENV_T_BET}, or place {single.name} under {data_root}, "
        f"or add files under l0_layered/*/t_bet/**/*.parquet"
    )


def resolve_t_session(data_root: Path) -> Path:
    """Resolve ``t_session`` Parquet path."""
    raw = os.environ.get(_ENV_T_SESSION, "").strip()
    if raw:
        p = Path(raw).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"{_ENV_T_SESSION}={raw!r} is not a file")
        return p
    p = (data_root / "gmwds_t_session.parquet").resolve()
    if not p.is_file():
        raise FileNotFoundError(
            f"No t_session: set {_ENV_T_SESSION}, or place gmwds_t_session.parquet under {data_root}"
        )
    return p


def infer_gaming_ym_span_from_t_bet(paths: list[Path]) -> list[str]:
    """Return sorted distinct ``YYYY-MM`` from ``gaming_day`` across given Parquet files."""
    import duckdb

    if not paths:
        raise ValueError("infer_gaming_ym_span_from_t_bet requires non-empty paths")
    rp_list = ", ".join(f"'{p.resolve().as_posix().replace(chr(39), chr(39) + chr(39))}'" for p in paths)
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            SELECT DISTINCT strftime(date_trunc('month', d), '%Y-%m') AS ym
            FROM (SELECT TRY_CAST(gaming_day AS DATE) AS d FROM read_parquet([{rp_list}])) s
            WHERE d IS NOT NULL
            ORDER BY ym
            """
        ).fetchall()
    finally:
        con.close()
    if not rows:
        raise RuntimeError("Could not infer gaming_ym span: gaming_day missing or null in t_bet inputs")
    out: list[str] = []
    for (cell,) in rows:
        ym = str(cell).strip()
        if not re.fullmatch(r"\d{4}-\d{2}", ym):
            raise RuntimeError(f"Inferred gaming_ym invalid in span: {ym!r}")
        out.append(ym)
    return out


def infer_snapshot_id(bet_paths: list[Path]) -> str:
    """Use ``l0_layered/<snap>/...`` when present; else content-addressed id."""
    raw = os.environ.get(_ENV_SNAPSHOT_ID, "").strip()
    if raw:
        return raw
    for p in bet_paths:
        parts = p.resolve().parts
        for i, part in enumerate(parts):
            if part == "l0_layered" and i + 1 < len(parts):
                nxt = parts[i + 1]
                if nxt.startswith("snap_"):
                    return nxt
    h = hashlib.sha256()
    for p in sorted(bet_paths, key=lambda x: str(x.resolve())):
        rp = p.resolve()
        h.update(str(rp).encode("utf-8"))
        try:
            h.update(str(int(rp.stat().st_size)).encode("ascii"))
        except OSError:
            h.update(b"0")
    return f"snap_mvp_{h.hexdigest()[:16]}"


def resolve_gaming_ym_list(bet_paths: list[Path]) -> list[str]:
    """Return months to process: env forces one month; else all distinct months in ``t_bet``."""
    raw = os.environ.get(_ENV_GAMING_YM, "").strip()
    if raw:
        if not re.fullmatch(r"\d{4}-\d{2}", raw):
            raise ValueError(f"{_ENV_GAMING_YM} must be YYYY-MM, got {raw!r}")
        return [raw]
    return infer_gaming_ym_span_from_t_bet(bet_paths)


def resolve_cutoff(gaming_ym: str) -> datetime:
    """Return cutoff from env or end-of-month HK default for ``gaming_ym``."""
    raw = os.environ.get(_ENV_CUTOFF, "").strip()
    if raw:
        return _parse_cutoff_dtm(raw)
    return default_cutoff_for_gaming_ym(gaming_ym)


def _force_recompute_months() -> bool:
    """True when env requests ignoring per-month ``inputs_fingerprint`` skip."""
    v = os.environ.get(_ENV_FORCE_RECOMPUTE, "").strip().lower()
    return v in ("1", "true", "yes")


def _ingest_yaml_content_sha256(ingest_yaml: Path | None) -> str:
    """Return SHA-256 hex of registry bytes, or fixed digest when absent (always 64 hex)."""
    from parallel_lda_mvp.eligible_builder import streaming_sha256_hex_file

    if ingest_yaml is None or not ingest_yaml.is_file():
        return hashlib.sha256(b"ingest_yaml_absent").hexdigest()
    return streaming_sha256_hex_file(ingest_yaml.resolve())


_MONTH_BET_SHA_CACHE_SCHEMA_VERSION = 2
_T_BET_MONTH_SHA_ALGO_VERSION = "t_bet_month_fp_v2"
_MONTH_BET_SHA_CACHE_FILENAME = "month_bet_sha_cache.v1.json"
_MONTH_BET_SHA_TAIL_MONTHS_DEFAULT = 2
_MONTH_BET_SHA_POLICY_VERSION = "tail2_late_runtime_v1"


def _mvp_late_arrival_window_days() -> int:
    """Late-arrival window in whole days (from ``canonical_mapping_runtime_config``)."""
    from parallel_lda_mvp.canonical_mapping_runtime_config import late_arrival_window_days

    return int(late_arrival_window_days())
_FINGERPRINT_ALGO_ROLLING = "rolling_keys_plus_core_v1"
_FINGERPRINT_ALGO_LEGACY = "legacy_extract_sha_v1"
# Bump when canonical row-hash inputs change (see ``schema/GDP_GMWDS_Raw_Schema_Dictionary.md`` §4 t_bet).
_ROLLING_FP_ROW_VERSION = "rolling_row_v2"

# Keys + money slots for rolling row hash. GMWDS raw ``t_bet`` uses ``wager`` / ``casino_win`` (no
# ``turnover`` / ``valid_stake`` / ``net_win`` on that export); layered/cleaned Parquet may use the
# latter three names—see ``_rolling_fp_hash_concat_sql``.
_ROLLING_FP_KEY_COLS: frozenset[str] = frozenset(
    {"bet_id", "gaming_day", "player_id", "payout_complete_dtm"}
)


def _t_bet_paths_input_fingerprint(t_bet_paths: list[Path]) -> str:
    """Return SHA-256 hex over stable ``path\\tsize\\tmtime_ns`` lines for all ``t_bet`` inputs."""
    lines: list[str] = []
    for p in sorted((x.resolve() for x in t_bet_paths), key=str):
        if not p.is_file():
            raise FileNotFoundError(f"t_bet input not found: {p}")
        st = p.stat()
        lines.append(f"{p.as_posix()}\t{st.st_size}\t{int(st.st_mtime_ns)}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _t_bet_read_parquet_rp_list(t_bet_paths: list[Path]) -> str:
    """SQL-safe ``read_parquet([...])`` path list fragment."""
    return ", ".join(f"'{p.resolve().as_posix().replace(chr(39), chr(39) + chr(39))}'" for p in t_bet_paths)


def _parquet_union_column_names(con: object, *, rp_list: str) -> set[str]:
    """Return column names from ``read_parquet([...]) LIMIT 0`` (union schema)."""
    desc = con.execute(f"SELECT * FROM read_parquet([{rp_list}]) LIMIT 0").description
    if not desc:
        return set()
    return {str(d[0]) for d in desc}


def _rolling_fp_hash_concat_sql(cols: set[str]) -> str | None:
    """Build ``concat_ws(...)`` body for row hash, or ``None`` if schema cannot be fingerprinted."""
    if not _ROLLING_FP_KEY_COLS <= cols:
        return None
    if "turnover" in cols:
        t_expr = "turnover"
    elif "wager" in cols:
        t_expr = "wager"
    else:
        return None
    if "valid_stake" in cols:
        v_expr = "valid_stake"
    elif "wager" in cols:
        v_expr = "wager"
    elif "turnover" in cols:
        v_expr = "turnover"
    else:
        return None
    if "net_win" in cols:
        n_expr = "net_win"
    elif "casino_win" in cols:
        n_expr = "(-1 * casino_win)"
    else:
        return None
    return (
        "concat_ws(\n"
        "                chr(1),\n"
        "                coalesce(strftime('%Y-%m-%d', TRY_CAST(gaming_day AS DATE)), ''),\n"
        "                coalesce(CAST(bet_id AS VARCHAR), ''),\n"
        "                coalesce(CAST(player_id AS VARCHAR), ''),\n"
        "                coalesce(strftime('%Y-%m-%d %H:%M:%S', "
        "TRY_CAST(payout_complete_dtm AS TIMESTAMP)), ''),\n"
        f"                coalesce(CAST({t_expr} AS VARCHAR), ''),\n"
        f"                coalesce(CAST({v_expr} AS VARCHAR), ''),\n"
        f"                coalesce(CAST({n_expr} AS VARCHAR), '')\n"
        "              )"
    )


def _rolling_parquet_columns_ok(con: object, *, rp_list: str) -> bool:
    """True when Parquet exposes enough columns to build the rolling row-hash (keys + money slots)."""
    cols = _parquet_union_column_names(con, rp_list=rp_list)
    return _rolling_fp_hash_concat_sql(cols) is not None


def _target_month_bet_fingerprint_algo(t_bet_paths: list[Path]) -> str:
    """Prefer rolling row-hash aggregate when schema allows; else legacy extract path."""
    import duckdb

    try:
        con = duckdb.connect()
        try:
            rp_list = _t_bet_read_parquet_rp_list(t_bet_paths)
            if _rolling_parquet_columns_ok(con, rp_list=rp_list):
                return _FINGERPRINT_ALGO_ROLLING
        finally:
            con.close()
    except Exception:
        pass
    return _FINGERPRINT_ALGO_LEGACY


def _rolling_month_digest_from_components(
    *,
    ym: str,
    row_count: int,
    xor_u64: int,
    sum_u64: int,
) -> str:
    """SHA-256 hex over canonical monthly aggregate components (downstream expects 64 hex)."""
    s = (
        f"{_ROLLING_FP_ROW_VERSION}|{ym}|{int(row_count)}|"
        f"{int(xor_u64) % (2**64):016x}|{int(sum_u64) % (2**64):016x}"
    )
    return hashlib.sha256(s.encode("ascii")).hexdigest()


def _rolling_month_fingerprints_for_yms(
    con: object,
    *,
    rp_list: str,
    yms: Sequence[str],
) -> dict[str, str]:
    """One DuckDB scan: per-``ym`` rolling fingerprint (keys + core) for ``yms`` only."""
    for ym in yms:
        if not re.fullmatch(r"\d{4}-\d{2}", str(ym).strip()):
            raise ValueError(f"ym must be YYYY-MM, got {ym!r}")
    if not yms:
        return {}
    cols = _parquet_union_column_names(con, rp_list=rp_list)
    hash_inner = _rolling_fp_hash_concat_sql(cols)
    if hash_inner is None:
        raise RuntimeError("rolling fingerprint: unsupported t_bet schema (missing keys/core slots)")
    vals_sql = ", ".join(f"('{str(ym).strip()}')" for ym in yms)
    rows = con.execute(
        f"""
        WITH wanted(ym) AS (VALUES {vals_sql}),
        base AS (
          SELECT
            strftime('%Y-%m', TRY_CAST(gaming_day AS DATE)) AS ym,
            hash(
              {hash_inner}
            )::UBIGINT AS rh
          FROM read_parquet([{rp_list}])
          WHERE strftime('%Y-%m', TRY_CAST(gaming_day AS DATE)) IN (SELECT ym FROM wanted)
        ),
        agg AS (
          SELECT
            ym,
            COUNT(*)::BIGINT AS n,
            bit_xor(rh)::UBIGINT AS xorh,
            sum(CAST(rh AS HUGEINT)) AS sumh
          FROM base
          GROUP BY ym
        )
        SELECT
          w.ym,
          COALESCE(a.n, 0::BIGINT),
          COALESCE(a.xorh, 0::UBIGINT),
          COALESCE(a.sumh, 0::HUGEINT)
        FROM wanted w
        LEFT JOIN agg a USING (ym)
        ORDER BY w.ym
        """
    ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        ym_cell, n_cell, xor_cell, sum_cell = row
        ym_s = str(ym_cell).strip()
        n_i = int(n_cell)
        xor_u = int(xor_cell) % (2**64) if xor_cell is not None else 0
        sum_raw = 0 if sum_cell is None else int(sum_cell)
        sum_u = sum_raw % (2**64)
        out[ym_s] = _rolling_month_digest_from_components(
            ym=ym_s, row_count=n_i, xor_u64=xor_u, sum_u64=sum_u
        )
    if set(out.keys()) != {str(x).strip() for x in yms}:
        raise RuntimeError(f"rolling fingerprint ym keys mismatch: want={yms!r} got={sorted(out)!r}")
    return out


def _month_bet_sha_policy_payload(*, fingerprint_algo: str) -> dict[str, int | str]:
    """Embedded policy for month SHA cache (must match reader validation)."""
    return {
        "fingerprint_algo": fingerprint_algo,
        "late_arrival_window_days": int(_mvp_late_arrival_window_days()),
        "policy_version": _MONTH_BET_SHA_POLICY_VERSION,
        "tail_months": int(_MONTH_BET_SHA_TAIL_MONTHS_DEFAULT),
    }


def _read_month_bet_sha_cache(
    scratch_dir: Path,
) -> tuple[str, dict[str, str], str] | None:
    """Load validated month SHA cache; return ``(inputs_fp, by_ym, fingerprint_algo)`` or ``None``."""
    path = scratch_dir / _MONTH_BET_SHA_CACHE_FILENAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("cache_schema_version") != _MONTH_BET_SHA_CACHE_SCHEMA_VERSION:
        return None
    if raw.get("algo_version") != _T_BET_MONTH_SHA_ALGO_VERSION:
        return None
    pol = raw.get("policy")
    if not isinstance(pol, dict):
        return None
    if pol.get("policy_version") != _MONTH_BET_SHA_POLICY_VERSION:
        return None
    if int(pol.get("tail_months", -1)) != _MONTH_BET_SHA_TAIL_MONTHS_DEFAULT:
        return None
    if int(pol.get("late_arrival_window_days", -1)) != _mvp_late_arrival_window_days():
        return None
    fa = pol.get("fingerprint_algo")
    if fa not in (_FINGERPRINT_ALGO_ROLLING, _FINGERPRINT_ALGO_LEGACY):
        return None
    fp = raw.get("t_bet_inputs_fingerprint")
    if not isinstance(fp, str) or len(fp) != 64:
        return None
    by = raw.get("by_ym")
    if not isinstance(by, dict):
        return None
    out: dict[str, str] = {}
    for k, v in by.items():
        if isinstance(k, str) and re.fullmatch(r"\d{4}-\d{2}", k) and isinstance(v, str) and len(v) == 64:
            out[k] = v
    return fp, out, str(fa)


def _write_month_bet_sha_cache(
    scratch_dir: Path,
    *,
    t_bet_inputs_fingerprint: str,
    span_by_ym: dict[str, str],
    fingerprint_algo: str,
) -> None:
    """Merge ``span_by_ym`` into on-disk cache (same fingerprint rows preserved)."""
    scratch_dir.mkdir(parents=True, exist_ok=True)
    path = scratch_dir / _MONTH_BET_SHA_CACHE_FILENAME
    prev_by: dict[str, str] = {}
    prev = _read_month_bet_sha_cache(scratch_dir)
    if prev is not None and prev[0] == t_bet_inputs_fingerprint and prev[2] == fingerprint_algo:
        prev_by = dict(prev[1])
    merged = {**prev_by, **span_by_ym}
    payload = {
        "algo_version": _T_BET_MONTH_SHA_ALGO_VERSION,
        "by_ym": merged,
        "cache_schema_version": _MONTH_BET_SHA_CACHE_SCHEMA_VERSION,
        "policy": _month_bet_sha_policy_payload(fingerprint_algo=fingerprint_algo),
        "t_bet_inputs_fingerprint": t_bet_inputs_fingerprint,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _t_bet_month_content_sha256_with_con(
    ym: str,
    t_bet_paths: list[Path],
    scratch_dir: Path,
    con: object,
) -> str:
    """Hash month slice using existing DuckDB connection (same semantics as legacy path)."""
    from parallel_lda_mvp.eligible_builder import streaming_sha256_hex_file

    if not re.fullmatch(r"\d{4}-\d{2}", ym.strip()):
        raise ValueError(f"ym must be YYYY-MM, got {ym!r}")
    rp_list = ", ".join(f"'{p.resolve().as_posix().replace(chr(39), chr(39) + chr(39))}'" for p in t_bet_paths)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    tmp = scratch_dir / f"bet_month_{ym.replace('-', '_')}_extract.parquet.tmp"
    esc_path = str(tmp.resolve().as_posix()).replace("'", "''")
    con.execute(
        f"""
        COPY (
          SELECT * FROM read_parquet([{rp_list}])
          WHERE strftime('%Y-%m', TRY_CAST(gaming_day AS DATE)) = '{ym.strip()}'
          ORDER BY
            TRY_CAST(gaming_day AS DATE) NULLS LAST,
            coalesce(cast(bet_id AS VARCHAR), ''),
            coalesce(cast(player_id AS VARCHAR), '')
        ) TO '{esc_path}' (FORMAT PARQUET)
        """
    )
    if not tmp.is_file():
        raise RuntimeError(f"DuckDB did not write month extract: {tmp}")
    try:
        return streaming_sha256_hex_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def _t_bet_month_content_sha256(ym: str, t_bet_paths: list[Path], scratch_dir: Path) -> str:
    """Hash Parquet bytes of all ``t_bet`` rows whose ``gaming_day`` falls in ``ym``."""
    import duckdb

    con = duckdb.connect()
    try:
        return _t_bet_month_content_sha256_with_con(ym, t_bet_paths, scratch_dir, con)
    finally:
        con.close()


def _recompute_month_bet_shas_one_connection(
    months: Sequence[str],
    t_bet_paths: list[Path],
    scratch_dir: Path,
) -> dict[str, str]:
    """Recompute month SHAs using one DuckDB connection (still one scan per month; saves connect overhead)."""
    import duckdb

    out: dict[str, str] = {}
    con = duckdb.connect()
    n = len(months)
    try:
        for i, ym in enumerate(months, start=1):
            print(
                f"[parallel_lda_mvp] month_bet_sha DuckDB ym={ym} ({i}/{n}) …",
                flush=True,
            )
            t0 = time.perf_counter()
            out[ym] = _t_bet_month_content_sha256_with_con(ym, t_bet_paths, scratch_dir, con)
            print(
                f"[parallel_lda_mvp] month_bet_sha DuckDB ym={ym} ({i}/{n}) ok "
                f"{time.perf_counter() - t0:.1f}s",
                flush=True,
            )
    finally:
        con.close()
    return out


def _tail_and_frozen_month_lists(
    gaming_yms: Sequence[str],
    tail_months: int,
) -> tuple[list[str], list[str]]:
    """Return ``(tail_yms, frozen_yms)`` for tail recompute policy (sorted span preserved)."""
    g = list(gaming_yms)
    n = int(tail_months)
    if n < 1:
        raise ValueError(f"tail_months must be >= 1, got {tail_months!r}")
    if len(g) <= n:
        return g, []
    return g[-n:], g[:-n]


def _compute_month_bet_shas_for_span(
    gaming_yms: Sequence[str],
    t_bet_paths: list[Path],
    scratch_dir: Path,
    *,
    force: bool,
    coverage_end_gaming_day: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Resolve ``month_bet_sha`` with tail-month recompute + disk cache for frozen months.

    Returns ``(by_ym, stats)``. ``stats`` includes recompute/cache counters, policy metadata,
    and ``escalated_full_span`` (1 when full span DuckDB was required).
    """
    tail_n = _MONTH_BET_SHA_TAIL_MONTHS_DEFAULT
    stats: dict[str, Any] = {
        "cache_hits": 0,
        "coverage_end_gaming_day": coverage_end_gaming_day.strip(),
        "escalated_full_span": 0,
        "fallback_separate_connects": 0,
        "force_recompute_used": bool(force),
        "frozen_months_this_run": [],
        "late_arrival_window_days": _mvp_late_arrival_window_days(),
        "mode": "unset",
        "month_bet_sha_fingerprint_algo": "unset",
        "month_bet_sha_policy_version": _MONTH_BET_SHA_POLICY_VERSION,
        "month_bet_sha_strategy": "unset",
        "recomputed": 0,
        "tail_months_default": tail_n,
        "tail_months_this_run": [],
    }
    fp = _t_bet_paths_input_fingerprint(t_bet_paths)
    target_fp_algo = _target_month_bet_fingerprint_algo(t_bet_paths)
    cached = _read_month_bet_sha_cache(scratch_dir)
    cache_ok = cached is not None and cached[0] == fp
    disk_algo = str(cached[2]) if cache_ok else None
    by_disk: dict[str, str] = dict(cached[1]) if cache_ok else {}
    cache_effective = bool(cache_ok and disk_algo == target_fp_algo)
    if cache_ok and not cache_effective:
        print(
            "[parallel_lda_mvp] month_bet_sha cache ignored: fingerprint_algo "
            f"disk={disk_algo!r} target={target_fp_algo!r} (rebuild span months)",
            flush=True,
        )
        by_disk = {}

    result: dict[str, str] = {}
    to_compute: list[str] = []
    gys = list(gaming_yms)

    if force:
        to_compute = list(gys)
        stats["escalated_full_span"] = 1
        stats["mode"] = "force_full"
    elif len(gys) <= tail_n:
        if cache_effective and all(ym in by_disk for ym in gys):
            for ym in gys:
                result[ym] = by_disk[ym]
                stats["cache_hits"] += 1
            stats["mode"] = "small_span_cache_all"
        else:
            to_compute = list(gys)
            stats["mode"] = "small_span_cold_or_incomplete"
            if (not cache_effective) or (not all(ym in by_disk for ym in gys)):
                stats["escalated_full_span"] = 1
    else:
        tail, frozen = _tail_and_frozen_month_lists(gys, tail_n)
        stats["tail_months_this_run"] = tail
        stats["frozen_months_this_run"] = frozen
        if not cache_effective:
            to_compute = list(gys)
            stats["mode"] = "cold_no_cache_or_fp_mismatch"
            stats["escalated_full_span"] = 1
        else:
            missing_frozen = [ym for ym in frozen if ym not in by_disk]
            if missing_frozen:
                to_compute = list(gys)
                stats["mode"] = "escalate_missing_frozen_in_cache"
                stats["escalated_full_span"] = 1
            else:
                for ym in frozen:
                    result[ym] = by_disk[ym]
                    stats["cache_hits"] += 1
                to_compute = list(tail)
                stats["mode"] = "tail_only_recompute"

    if not to_compute:
        stats["month_bet_sha_fingerprint_algo"] = disk_algo if cache_effective else target_fp_algo
        stats["month_bet_sha_strategy"] = stats["month_bet_sha_fingerprint_algo"]
    else:
        stats["month_bet_sha_fingerprint_algo"] = target_fp_algo
        stats["month_bet_sha_strategy"] = target_fp_algo

    print(
        f"[parallel_lda_mvp] month_bet_sha policy={stats['month_bet_sha_policy_version']} "
        f"mode={stats['mode']} inputs_fp={fp[:12]}… "
        f"from_cache={stats['cache_hits']} recompute_queue={len(to_compute)} "
        f"late_arrival_days={stats['late_arrival_window_days']} "
        f"force_recompute={stats['force_recompute_used']} "
        f"target_fp_algo={target_fp_algo} fp_algo={stats['month_bet_sha_fingerprint_algo']}",
        flush=True,
    )

    if to_compute:
        stats["recomputed"] = len(to_compute)
        recompute_algo = target_fp_algo
        batch: dict[str, str] = {}
        if recompute_algo == _FINGERPRINT_ALGO_ROLLING:
            try:
                import duckdb

                con_roll = duckdb.connect()
                try:
                    rp_list = _t_bet_read_parquet_rp_list(t_bet_paths)
                    if not _rolling_parquet_columns_ok(con_roll, rp_list=rp_list):
                        raise RuntimeError("rolling fingerprint: required columns missing on t_bet")
                    batch = _rolling_month_fingerprints_for_yms(
                        con_roll, rp_list=rp_list, yms=to_compute
                    )
                finally:
                    con_roll.close()
            except Exception as exc:
                print(
                    f"[parallel_lda_mvp] month_bet_sha rolling fingerprint failed ({exc!r}); "
                    "fallback legacy extract+hash",
                    flush=True,
                )
                recompute_algo = _FINGERPRINT_ALGO_LEGACY
                batch = {}

        if recompute_algo == _FINGERPRINT_ALGO_LEGACY:
            try:
                batch = _recompute_month_bet_shas_one_connection(to_compute, t_bet_paths, scratch_dir)
            except Exception as exc:
                print(
                    f"[parallel_lda_mvp] month_bet_sha batch DuckDB failed ({exc!r}); "
                    f"fallback per-month connect",
                    flush=True,
                )
                stats["fallback_separate_connects"] = 1
                nf = len(to_compute)
                for j, ym in enumerate(to_compute, start=1):
                    print(
                        f"[parallel_lda_mvp] month_bet_sha fallback ym={ym} ({j}/{nf}) …",
                        flush=True,
                    )
                    t_fb = time.perf_counter()
                    try:
                        batch[ym] = _t_bet_month_content_sha256(ym, t_bet_paths, scratch_dir)
                        print(
                            f"[parallel_lda_mvp] month_bet_sha fallback ym={ym} ({j}/{nf}) ok "
                            f"{time.perf_counter() - t_fb:.1f}s",
                            flush=True,
                        )
                    except Exception as inner_exc:
                        done_here = sorted(k for k in to_compute if k in batch)
                        pending_here = sorted(k for k in to_compute if k not in batch)
                        raise RuntimeError(
                            "[parallel_lda_mvp] month_bet_sha per-month fallback failed "
                            f"at ym={ym!r} ({inner_exc!r}); "
                            f"months completed in this fallback batch: {done_here}; "
                            f"not yet computed in this batch: {pending_here}"
                        ) from inner_exc

        result.update(batch)
        stats["month_bet_sha_fingerprint_algo"] = recompute_algo
        stats["month_bet_sha_strategy"] = recompute_algo
        print(
            f"[parallel_lda_mvp] month_bet_sha recompute finished algo={recompute_algo} "
            f"n={len(to_compute)}",
            flush=True,
        )

    missing = [ym for ym in gaming_yms if ym not in result]
    if missing:
        raise RuntimeError(
            "[parallel_lda_mvp] month_bet_sha span incomplete; "
            f"missing keys: {missing}"
        )
    subset = {ym: result[ym] for ym in gaming_yms}
    cache_write_algo = str(stats.get("month_bet_sha_fingerprint_algo") or target_fp_algo)
    if stats["recomputed"] > 0 or force:
        try:
            _write_month_bet_sha_cache(
                scratch_dir,
                t_bet_inputs_fingerprint=fp,
                span_by_ym=subset,
                fingerprint_algo=cache_write_algo,
            )
        except OSError as exc:
            print(f"[parallel_lda_mvp] month_bet_sha cache write skipped: {exc!r}", flush=True)
    return subset, stats


def _month_skip_keys_cached(out_root: Path) -> tuple[str, str, str, str] | None:
    """Return cached skip tuple from ``mvp_summary.json`` if all keys valid (else None)."""
    path = out_root / "mvp_summary.json"
    if not path.is_file():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    mb = meta.get("t_bet_month_content_sha256")
    mp = meta.get("mapping_cache_fingerprint")
    ing = meta.get("ingest_yaml_content_sha256")
    pst = meta.get("preprocess_month_batch_stamp")
    if not (
        isinstance(mb, str)
        and len(mb) == 64
        and isinstance(mp, str)
        and len(mp) == 64
        and isinstance(ing, str)
        and len(ing) == 64
        and isinstance(pst, str)
        and pst.strip()
    ):
        return None
    return mb, mp, ing, pst.strip()


def _expected_span_run_fact_paths(*, snap_root: Path, gaming_yms: Sequence[str]) -> list[Path]:
    """Ordered list of every ``run_fact__*.parquet`` path for the span (for trip inputs)."""
    out: list[Path] = []
    for ym in gaming_yms:
        out_root = snap_root / f"gaming_ym={ym}"
        for gd in gaming_days_in_month(ym):
            out.append(_run_fact_parquet_flat(out_root, gd))
    return out


def _span_run_fact_input_fingerprint(paths: Sequence[Path]) -> str:
    """SHA-256 over stable path/size/mtime lines (invalidates when any span run_fact changes)."""
    lines: list[str] = []
    for p in sorted((x.resolve() for x in paths), key=lambda x: str(x)):
        if not p.is_file():
            raise FileNotFoundError(f"span run_fact missing for fingerprint: {p}")
        st = p.stat()
        lines.append(f"{p.as_posix()}\t{st.st_size}\t{int(st.st_mtime_ns)}")
    blob = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _month_trip_phase_skip_ok(
    out_root: Path,
    *,
    span_run_fact_input_fingerprint: str,
    month_bet_sha: str,
    map_fp: str,
    ingest_sha: str,
    preprocess_stamp: str,
    trip_fact_paths: Sequence[Path],
    trip_map_paths: Sequence[Path],
    prior_summary: dict[str, Any] | None = None,
) -> bool:
    """True if cached summary + outputs allow skipping the trip subprocess phase for this month."""
    meta: dict[str, Any] | None = prior_summary
    if meta is None:
        path = out_root / "mvp_summary.json"
        if not path.is_file():
            return False
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
    if meta is None:
        return False
    sp = meta.get("span_run_fact_input_fingerprint")
    if not (isinstance(sp, str) and len(sp) == 64):
        return False
    if sp != span_run_fact_input_fingerprint:
        return False
    if meta.get("t_bet_month_content_sha256") != month_bet_sha:
        return False
    if meta.get("mapping_cache_fingerprint") != map_fp:
        return False
    if meta.get("ingest_yaml_content_sha256") != ingest_sha:
        return False
    if meta.get("preprocess_month_batch_stamp") != preprocess_stamp:
        return False
    return _all_outputs_ready(trip_fact_paths) and _all_outputs_ready(trip_map_paths)


def _flat_month_paths(out_root: Path, ym: str) -> tuple[Path, Path, Path, Path]:
    """Return ``(t_bet_dir, run_fact_dir, trip_fact_dir, trip_run_map_dir)`` for ``gaming_ym``."""
    _ = ym  # reserved for future multi-layout
    return (
        out_root / "t_bet",
        out_root / "run_fact",
        out_root / "trip_fact",
        out_root / "trip_run_map",
    )


def _cleaned_parquet_flat(out_root: Path, gd: str) -> Path:
    """L1 cleaned bet path under ``gaming_ym`` (flat ``t_bet/`` layout)."""
    return out_root / "t_bet" / f"cleaned__{gd}.parquet"


def _cleaned_month_parquet_path(out_root: Path, ym: str) -> Path:
    """Single full-month preprocess output (dedup / patches over entire ``ym``)."""
    return out_root / "t_bet" / f"cleaned_month__{ym}.parquet"


def _split_month_cleaned_to_days(
    *,
    month_cleaned_parquet: Path,
    ym: str,
    out_root: Path,
) -> list[Path]:
    """Materialize per-``gaming_day`` cleaned Parquets from one month-wide preprocess output."""
    import duckdb

    if not month_cleaned_parquet.is_file():
        raise FileNotFoundError(f"month cleaned parquet missing: {month_cleaned_parquet}")
    src = str(month_cleaned_parquet.resolve().as_posix()).replace("'", "''")
    days = gaming_days_in_month(ym)
    out_paths: list[Path] = []
    con = duckdb.connect()
    try:
        for gd in days:
            out_pq = _cleaned_parquet_flat(out_root, gd)
            out_pq.parent.mkdir(parents=True, exist_ok=True)
            esc = str(out_pq.resolve().as_posix()).replace("'", "''")
            con.execute(
                f"""
                COPY (
                  SELECT * FROM read_parquet('{src}')
                  WHERE TRY_CAST(gaming_day AS DATE) = ?::DATE
                  ORDER BY TRY_CAST(gaming_day AS DATE) ASC NULLS LAST, bet_id ASC
                ) TO '{esc}' (FORMAT PARQUET)
                """,
                [gd],
            )
            out_paths.append(out_pq)
        return out_paths
    finally:
        con.close()


def _materialize_cleaned_bets_with_canonical_id(
    *,
    cleaned_paths: list[Path],
    mapping_parquet: Path,
) -> list[Path]:
    """INNER JOIN rated canonical mapping onto each per-day cleaned bet Parquet (drops unmapped rows)."""
    import duckdb

    mp = mapping_parquet.resolve()
    if not mp.is_file():
        raise FileNotFoundError(f"canonical mapping parquet missing: {mp}")
    mp_sql = str(mp.as_posix()).replace("'", "''")
    out_paths: list[Path] = []
    con = duckdb.connect()
    try:
        for src in cleaned_paths:
            if not src.is_file():
                raise FileNotFoundError(f"cleaned bet parquet missing: {src}")
            dst = src.with_name(src.stem + "__canonical.parquet")
            sp = str(src.resolve().as_posix()).replace("'", "''")
            dp = str(dst.resolve().as_posix()).replace("'", "''")
            con.execute(
                f"""
                COPY (
                  SELECT b.*, CAST(m.canonical_id AS VARCHAR) AS canonical_id
                  FROM read_parquet('{sp}') b
                  INNER JOIN (
                    SELECT
                      CAST(player_id AS BIGINT) AS player_id,
                      CAST(canonical_id AS VARCHAR) AS canonical_id
                    FROM read_parquet('{mp_sql}')
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY player_id) = 1
                  ) m USING (player_id)
                ) TO '{dp}' (FORMAT PARQUET)
                """
            )
            out_paths.append(dst)
        return out_paths
    finally:
        con.close()


def _run_fact_parquet_flat(out_root: Path, gd: str) -> Path:
    """L1 ``run_fact`` path (flat ``run_fact/`` layout)."""
    return out_root / "run_fact" / f"run_fact__{gd}.parquet"


def _trip_fact_parquet_flat(out_root: Path, gd: str) -> Path:
    """L1 ``trip_fact`` path (flat ``trip_fact/`` layout)."""
    return out_root / "trip_fact" / f"trip_fact__{gd}.parquet"


def _trip_run_map_parquet_flat(out_root: Path, gd: str) -> Path:
    """L1 ``trip_run_map`` path (flat ``trip_run_map/`` layout)."""
    return out_root / "trip_run_map" / f"trip_run_map__{gd}.parquet"


def _preprocess_cleaned_paths_for_month(
    *,
    ym: str,
    out_root: Path,
    snap: str,
    data_root: Path,
    root: Path,
    t_bet_paths: list[Path],
    eligible_path: Path,
    ingest_yaml: Path | None,
    py: str,
) -> tuple[list[Path], bool]:
    """One full-month preprocess (dedup/patches), then split to per-day Parquets for ``run_fact``."""
    t_bet_dir, _, _, _ = _flat_month_paths(out_root, ym)
    t_bet_dir.mkdir(parents=True, exist_ok=True)
    legacy_day_state = t_bet_dir / "day_inputs_sha256.json"
    legacy_day_state.unlink(missing_ok=True)

    month_pq = _cleaned_month_parquet_path(out_root, ym)
    cmd = _preprocess_argv(
        py=py,
        gaming_day=None,
        gaming_ym=ym,
        output_parquet=month_pq,
        t_bet_paths=t_bet_paths,
        snap=snap,
        data_root=data_root,
        eligible=eligible_path,
        ingest_yaml=ingest_yaml,
    )
    _run_subprocess(cmd, cwd=root)
    cleaned_paths = _split_month_cleaned_to_days(month_cleaned_parquet=month_pq, ym=ym, out_root=out_root)
    return cleaned_paths, True


def _all_outputs_ready(parquet_paths: Sequence[Path]) -> bool:
    """True if each Parquet exists (non-empty) and sibling ``*.manifest.json`` exists."""
    for pq in parquet_paths:
        if not pq.is_file() or pq.stat().st_size == 0:
            return False
        mf = pq.with_name(pq.stem + ".manifest.json")
        if not mf.is_file():
            return False
    return True


def _run_runfact_for_month(
    *,
    ym: str,
    out_root: Path,
    snap: str,
    data_root: Path,
    root: Path,
    cleaned_paths: list[Path],
    mapping_parquet: Path,
    py: str,
    dirty_preprocess: bool,
) -> None:
    """Materialize all ``run_fact`` day partitions for ``ym`` in one DuckDB session (staging once)."""
    _ = (data_root, root, py)  # reserved for subprocess fallback / CLI symmetry
    days = gaming_days_in_month(ym)
    run_fact_paths = [_run_fact_parquet_flat(out_root, gd) for gd in days]
    _, run_fact_dir, _, _ = _flat_month_paths(out_root, ym)
    run_fact_dir.mkdir(parents=True, exist_ok=True)

    need_run_fact = dirty_preprocess or (not _all_outputs_ready(run_fact_paths))
    if not need_run_fact:
        print(f"[parallel_lda_mvp] skip run_fact for gaming_ym={ym} (outputs present, preprocess unchanged)")
        return

    import duckdb

    bet_inputs = _materialize_cleaned_bets_with_canonical_id(
        cleaned_paths=cleaned_paths,
        mapping_parquet=mapping_parquet,
    )

    from pipelines.layered_data_assets.core.run_fact_v1 import (
        RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
        RUN_BREAK_MIN_DEFAULT,
        SOURCE_NAMESPACE_DEFAULT,
        build_run_fact_manifest,
        materialize_run_boundary_temp_tables,
        materialize_run_fact_partition_from_staging,
    )
    from pipelines.layered_data_assets.io.atomic_parquet_manifest_v1 import (
        commit_parquet_and_manifest,
        remove_staged_outputs,
        staged_manifest_path,
        staged_parquet_path,
    )
    from pipelines.layered_data_assets.io.ingestion_delay_summary_v1 import (
        DEFAULT_LATE_THRESHOLD_SEC,
        compute_ingestion_delay_summary_preview,
    )
    from pipelines.layered_data_assets.io.manifest_lineage_v1 import merge_source_hashes_into_manifest
    from pipelines.layered_data_assets.orchestration.oom_runner_v1 import run_duckdb_job_with_oom_retries

    anchor = repo_root()
    inputs_resolved = [p.resolve() for p in bet_inputs]
    delay_src = inputs_resolved[0] if inputs_resolved else None
    print(
        f"[parallel_lda_mvp] run_fact batch ym={ym} days={len(days)} "
        f"cleaned_inputs={len(inputs_resolved)}"
    )
    rf_prog = _SequentialProgress(label=f"run_fact ym={ym}", total=len(days))

    def _work(con: object) -> None:
        t_stage = time.perf_counter()
        materialize_run_boundary_temp_tables(
            con,
            input_paths=inputs_resolved,
            run_break_min=RUN_BREAK_MIN_DEFAULT,
            run_definition_version=RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
            source_namespace=SOURCE_NAMESPACE_DEFAULT,
        )
        print(f"[parallel_lda_mvp] run_fact staging ym={ym} {time.perf_counter() - t_stage:.1f}s")
        id_summary_once: dict[str, Any] | None = None
        if delay_src is not None and delay_src.is_file():
            id_summary_once = compute_ingestion_delay_summary_preview(
                con, delay_src, late_threshold_sec=DEFAULT_LATE_THRESHOLD_SEC
            )
        try:
            rf_prog.start()
            for gd in days:
                out_pq = _run_fact_parquet_flat(out_root, gd)
                trip_manifest = out_pq.with_name(out_pq.stem + ".manifest.json")
                st_pq = staged_parquet_path(out_pq)
                st_mf = staged_manifest_path(trip_manifest)
                remove_staged_outputs(st_pq, st_mf)
                stats = materialize_run_fact_partition_from_staging(
                    con=con,
                    output_parquet=st_pq,
                    run_end_gaming_day=gd,
                )
                manifest = build_run_fact_manifest(
                    source_snapshot_id=snap,
                    run_end_gaming_day=gd,
                    l0_fingerprint_path=None,
                    l1_preprocess_gaming_day=gd,
                    output_parquet=out_pq,
                    manifest_uri_anchor=anchor,
                    stats=stats,
                    ingestion_delay_summary=id_summary_once,
                )
                manifest = merge_source_hashes_into_manifest(manifest, None)
                commit_parquet_and_manifest(
                    staged_parquet=st_pq,
                    final_parquet=out_pq,
                    manifest_text=json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
                    final_manifest=trip_manifest,
                )
                rf_prog.advance()
        finally:
            rf_prog.finish()

    t_rf = time.perf_counter()
    run_duckdb_job_with_oom_retries(
        connect=lambda: duckdb.connect(database=":memory:"),
        work=_work,
        input_paths=inputs_resolved,
        job_name=f"run_mvp_run_fact_month_{ym}",
        run_log_path=None,
        failure_context_path=None,
        max_attempts=3,
        initial_memory_limit_mb=None,
    )
    print(
        f"[parallel_lda_mvp] run_fact gaming_ym={ym} batch OK days={len(days)} "
        f"duckdb_wall={time.perf_counter() - t_rf:.1f}s"
    )


def _run_trip_span_batched(
    *,
    gaming_yms: list[str],
    snap_root: Path,
    snap: str,
    span_run_fact_paths: list[Path],
    coverage_end_day: str,
    span_run_fact_input_fingerprint: str,
    month_bet_sha_by_ym: dict[str, str],
    map_fp: str,
    ingest_sha: str,
    preprocess_stamp: str,
    prior_summary_by_ym: dict[str, dict[str, Any]],
) -> None:
    """One full-span trip build, then per-month/day writes (cross-month correct, minimal rescans)."""
    cov = date.fromisoformat(coverage_end_day.strip())

    def _paths_for_month(ym: str) -> tuple[list[Path], list[Path]]:
        days = gaming_days_in_month(ym)
        out_root = snap_root / f"gaming_ym={ym}"
        tf = [_trip_fact_parquet_flat(out_root, gd) for gd in days]
        tm = [_trip_run_map_parquet_flat(out_root, gd) for gd in days]
        return tf, tm

    all_skippable = True
    for ym in gaming_yms:
        out_root = snap_root / f"gaming_ym={ym}"
        tf, tm = _paths_for_month(ym)
        prior = prior_summary_by_ym.get(ym)
        if not _month_trip_phase_skip_ok(
            out_root,
            span_run_fact_input_fingerprint=span_run_fact_input_fingerprint,
            month_bet_sha=month_bet_sha_by_ym[ym],
            map_fp=map_fp,
            ingest_sha=ingest_sha,
            preprocess_stamp=preprocess_stamp,
            trip_fact_paths=tf,
            trip_map_paths=tm,
            prior_summary=prior,
        ):
            all_skippable = False
            break
    if all_skippable:
        print("[parallel_lda_mvp] skip trip_fact span (all months unchanged)")
        return

    print(
        f"[parallel_lda_mvp] trip_fact batch run_fact_parts={len(span_run_fact_paths)} "
        f"months={len(gaming_yms)}"
    )
    import duckdb

    from pipelines.layered_data_assets.core.trip_fact_v1 import (
        TRIP_DEFINITION_VERSION_DEFAULT,
        SOURCE_NAMESPACE_DEFAULT,
        build_trip_fact_manifest,
        build_trip_run_map_manifest,
        load_runs_build_trip_frames,
        materialize_trip_partition_from_frames,
    )
    from pipelines.layered_data_assets.io.atomic_parquet_manifest_v1 import (
        commit_parquet_and_manifest,
        remove_staged_outputs,
        staged_manifest_path,
        staged_parquet_path,
    )
    from pipelines.layered_data_assets.io.manifest_lineage_v1 import merge_source_hashes_into_manifest
    from pipelines.layered_data_assets.orchestration.oom_runner_v1 import run_duckdb_job_with_oom_retries

    anchor = repo_root()
    span_inputs = [p.resolve() for p in span_run_fact_paths]

    def _trip_work(con: object) -> None:
        print("[parallel_lda_mvp] trip_fact load span run_fact …", flush=True)
        t_load = time.perf_counter()
        trip_all, map_all, effective_cov, runs = load_runs_build_trip_frames(
            con=con,
            run_fact_paths=span_inputs,
            source_snapshot_id=snap,
            trip_definition_version=TRIP_DEFINITION_VERSION_DEFAULT,
            source_namespace=SOURCE_NAMESPACE_DEFAULT,
            coverage_end=cov,
        )
        print(
            f"[parallel_lda_mvp] trip frames trip={len(trip_all)} map={len(map_all)} "
            f"runs={len(runs)} eff_cov={effective_cov} load={time.perf_counter() - t_load:.1f}s",
            flush=True,
        )
        to_write: list[tuple[str, str]] = []
        for ym in gaming_yms:
            out_root = snap_root / f"gaming_ym={ym}"
            days = gaming_days_in_month(ym)
            _, _, trip_fact_dir, trip_run_map_dir = _flat_month_paths(out_root, ym)
            trip_fact_dir.mkdir(parents=True, exist_ok=True)
            trip_run_map_dir.mkdir(parents=True, exist_ok=True)
            trip_paths = [_trip_fact_parquet_flat(out_root, gd) for gd in days]
            map_paths = [_trip_run_map_parquet_flat(out_root, gd) for gd in days]
            prior = prior_summary_by_ym.get(ym)
            if _month_trip_phase_skip_ok(
                out_root,
                span_run_fact_input_fingerprint=span_run_fact_input_fingerprint,
                month_bet_sha=month_bet_sha_by_ym[ym],
                map_fp=map_fp,
                ingest_sha=ingest_sha,
                preprocess_stamp=preprocess_stamp,
                trip_fact_paths=trip_paths,
                trip_map_paths=map_paths,
                prior_summary=prior,
            ):
                print(f"[parallel_lda_mvp] skip trip_fact for gaming_ym={ym} (span run_fact + trip outputs unchanged)")
                continue
            to_write.extend((ym, gd) for gd in days)
        trip_prog = _SequentialProgress(label="trip_fact", total=len(to_write))
        try:
            trip_prog.start()
            for ym, gd in to_write:
                out_root = snap_root / f"gaming_ym={ym}"
                tf = _trip_fact_parquet_flat(out_root, gd)
                tm = _trip_run_map_parquet_flat(out_root, gd)
                mf_trip = tf.with_name(tf.stem + ".manifest.json")
                mf_map = tm.with_name(tm.stem + ".manifest.json")
                st_tf = staged_parquet_path(tf)
                st_tm = staged_parquet_path(tm)
                st_mf_t = staged_manifest_path(mf_trip)
                st_mf_m = staged_manifest_path(mf_map)
                remove_staged_outputs(st_tf, st_mf_t, st_tm, st_mf_m)
                stats = materialize_trip_partition_from_frames(
                    con=con,
                    trip_start_gaming_day=gd,
                    trip_fact_out=st_tf,
                    trip_run_map_out=st_tm,
                    trip_all=trip_all,
                    map_all=map_all,
                    effective_cov=effective_cov,
                    runs=runs,
                )
                sparts = list(stats["source_partitions"])
                mf_trip_obj = build_trip_fact_manifest(
                    source_snapshot_id=snap,
                    trip_start_gaming_day=gd,
                    l0_fingerprint_path=None,
                    output_parquet=tf,
                    manifest_uri_anchor=anchor,
                    stats=stats,
                    source_partitions=sparts,
                    ingestion_delay_summary=None,
                )
                mf_trip_obj = merge_source_hashes_into_manifest(mf_trip_obj, None)
                mf_map_obj = build_trip_run_map_manifest(
                    source_snapshot_id=snap,
                    trip_start_gaming_day=gd,
                    l0_fingerprint_path=None,
                    output_parquet=tm,
                    manifest_uri_anchor=anchor,
                    stats=stats,
                    source_partitions=sparts,
                    ingestion_delay_summary=None,
                )
                mf_map_obj = merge_source_hashes_into_manifest(mf_map_obj, None)
                commit_parquet_and_manifest(
                    staged_parquet=st_tf,
                    final_parquet=tf,
                    manifest_text=json.dumps(mf_trip_obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
                    final_manifest=mf_trip,
                )
                commit_parquet_and_manifest(
                    staged_parquet=st_tm,
                    final_parquet=tm,
                    manifest_text=json.dumps(mf_map_obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
                    final_manifest=mf_map,
                )
                trip_prog.advance()
        finally:
            trip_prog.finish()
        print("[parallel_lda_mvp] trip_fact span batch OK")

    t_trip = time.perf_counter()
    run_duckdb_job_with_oom_retries(
        connect=lambda: duckdb.connect(database=":memory:"),
        work=_trip_work,
        input_paths=span_inputs,
        job_name="run_mvp_trip_span_batch",
        run_log_path=None,
        failure_context_path=None,
        max_attempts=3,
        initial_memory_limit_mb=None,
    )
    print(f"[parallel_lda_mvp] trip_fact duckdb_wall={time.perf_counter() - t_trip:.1f}s", flush=True)


def _run_subprocess(argv: Sequence[str], *, cwd: Path) -> None:
    """Run subprocess; raise on non-zero exit."""
    p = subprocess.run(list(argv), cwd=str(cwd), env=os.environ.copy())
    if p.returncode != 0:
        raise RuntimeError(f"command failed rc={p.returncode}: {' '.join(argv)}")


def _day_materialize_worker_count(day_count: int) -> int:
    """Return max concurrent subprocesses for per-day materialization (capped by ``day_count``)."""
    if day_count < 1:
        return 1
    cpu = os.cpu_count() or 1
    cap = max(1, min(DAY_MATERIALIZE_MAX_WORKERS, cpu))
    return min(day_count, cap)


def _ascii_progress_bar(done: int, total: int, width: int = 18) -> str:
    """Return ``[====------]`` for ``done`` of ``total`` (clamped to ``0..total``)."""
    if total <= 0:
        return "[" + "-" * width + "]"
    d = min(max(done, 0), total)
    filled = int(round(width * d / total))
    filled = min(width, max(0, filled))
    return "[" + "=" * filled + "-" * (width - filled) + "]"


class _SequentialProgress:
    """Single-line ``\\r`` progress on stderr for one thread (e.g. run_fact / trip day loop)."""

    def __init__(self, *, label: str, total: int) -> None:
        if total < 0:
            raise ValueError(f"total must be >= 0, got {total}")
        self._label = label
        self._total = total
        self._done = 0
        self._tty = sys.stderr.isatty()
        self._last_len = 0

    def start(self) -> None:
        """Draw ``0/total`` (no-op when ``total==0``)."""
        self._done = 0
        if self._total <= 0:
            return
        self._emit(force=True)

    def advance(self) -> None:
        """Increment done count and refresh the bar."""
        if self._total <= 0:
            return
        self._done += 1
        self._emit(force=False)

    def finish(self) -> None:
        """End the ``\\r`` line (TTY) so following logs do not overwrite the bar."""
        if self._total <= 0:
            return
        if self._tty:
            sys.stderr.write("\n")
            sys.stderr.flush()

    def _emit(self, *, force: bool) -> None:
        bar = _ascii_progress_bar(self._done, self._total)
        line = f"[parallel_lda_mvp] {self._label} {bar} {self._done}/{self._total}"
        err = sys.stderr
        pad = max(0, self._last_len - len(line))
        self._last_len = max(self._last_len, len(line))
        if self._tty:
            err.write(line + (" " * pad) + "\r")
            err.flush()
            return
        step = max(1, self._total // 10)
        if not force and self._done % step != 0 and self._done != self._total:
            return
        err.write(line + "\n")
        err.flush()


def _format_active_days(active: set[str]) -> str:
    """Compact comma list of gaming_day values currently in flight."""
    if not active:
        return "—"
    ordered = sorted(active)
    if len(ordered) <= 3:
        return ",".join(ordered)
    return f"{ordered[0]},{ordered[1]},...+{len(ordered) - 2}"


class _DayParallelProgress:
    """Single-line (TTY ``\\r``) progress for concurrent per-day subprocesses."""

    def __init__(self, *, ym: str, phase: str, total: int, workers: int) -> None:
        self._ym = ym
        self._phase = phase
        self._total = total
        self._workers = workers
        self._done = 0
        self._active: set[str] = set()
        self._lock = threading.Lock()
        self._tty = sys.stderr.isatty()
        self._last_len = 0

    def draw_initial(self) -> None:
        """Print first progress line (TTY) or a one-line banner (non-TTY)."""
        with self._lock:
            if not self._tty:
                sys.stderr.write(
                    f"[parallel_lda_mvp] ym={self._ym} {self._phase} "
                    f"{self._total} days w={self._workers}\n"
                )
                sys.stderr.flush()
                return
            self._emit(running_only=False)

    def start_day(self, gd: str) -> None:
        """Mark ``gd`` as running (updates the progress line)."""
        with self._lock:
            self._active.add(gd)
            self._emit(running_only=True)

    def finish_day(self, gd: str) -> None:
        """Mark ``gd`` finished (success or failure); bump completed count."""
        with self._lock:
            self._active.discard(gd)
            self._done += 1
            self._emit(running_only=False)

    def end_phase_newline(self) -> None:
        """End the ``\\r`` line after all futures complete (TTY only)."""
        with self._lock:
            if self._tty:
                sys.stderr.write("\n")
                sys.stderr.flush()

    def abort_newline(self) -> None:
        """Leave the terminal on a fresh line before traceback or stderr from children."""
        with self._lock:
            sys.stderr.write("\n")
            sys.stderr.flush()

    def _emit(self, *, running_only: bool) -> None:
        bar = _ascii_progress_bar(self._done, self._total)
        act = _format_active_days(self._active)
        line = (
            f"[parallel_lda_mvp] ym={self._ym} {self._phase} w={self._workers} "
            f"{bar} {self._done}/{self._total} active:{act}"
        )
        err = sys.stderr
        pad = max(0, self._last_len - len(line))
        self._last_len = max(self._last_len, len(line))
        if self._tty:
            err.write(line + (" " * pad) + "\r")
            err.flush()
            return
        if running_only:
            return
        step = max(1, self._total // 10)
        if self._done % step != 0 and self._done != self._total:
            return
        err.write(line + "\n")
        err.flush()


def _run_subprocess_tracked(
    gd: str,
    argv: list[str],
    root: Path,
    prog: _DayParallelProgress,
) -> None:
    """Run ``argv`` for ``gd`` and update ``prog`` while the subprocess is in flight."""
    prog.start_day(gd)
    try:
        _run_subprocess(argv, cwd=root)
    finally:
        prog.finish_day(gd)


def _parallel_day_subprocesses(
    *,
    ym: str,
    phase: str,
    day_cmds: list[tuple[str, list[str]]],
    root: Path,
) -> None:
    """Run subprocesses for each calendar day using a bounded ``ThreadPoolExecutor``."""
    n = len(day_cmds)
    workers = _day_materialize_worker_count(n)
    prog = _DayParallelProgress(ym=ym, phase=phase, total=n, workers=workers)
    prog.draw_initial()
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut_to_gd = {
                ex.submit(_run_subprocess_tracked, gd, cmd, root, prog): gd for gd, cmd in day_cmds
            }
            for fut in as_completed(fut_to_gd):
                gd = fut_to_gd[fut]
                try:
                    fut.result()
                except Exception as exc:
                    raise RuntimeError(f"{ym} {phase} failed for gaming_day={gd}") from exc
    except BaseException:
        prog.abort_newline()
        raise
    prog.end_phase_newline()


def _preprocess_argv(
    *,
    py: str,
    gaming_day: str | None,
    gaming_ym: str | None,
    output_parquet: Path,
    t_bet_paths: list[Path],
    snap: str,
    data_root: Path,
    eligible: Path,
    ingest_yaml: Path | None,
) -> list[str]:
    """Build argv for ``scripts/preprocess_bet_v1.py``."""
    if (gaming_day is None) == (gaming_ym is None):
        raise ValueError("_preprocess_argv requires exactly one of gaming_day or gaming_ym")
    cmd: list[str] = [
        py,
        str(repo_root() / "scripts" / "preprocess_bet_v1.py"),
        "--data-root",
        str(data_root),
        "--source-snapshot-id",
        snap,
        "--eligible-player-ids-parquet",
        str(eligible),
    ]
    if gaming_ym is not None:
        cmd.extend(["--gaming-ym", gaming_ym])
    else:
        cmd.extend(["--gaming-day", str(gaming_day)])
    for p in t_bet_paths:
        cmd.extend(["--input", str(p.resolve())])
    if ingest_yaml is not None:
        cmd.extend(["--ingestion-fix-registry-yaml", str(ingest_yaml.resolve())])
    cmd.extend(["--output-parquet", str(output_parquet.resolve())])
    return cmd


def _run_fact_argv(
    *,
    py: str,
    run_end_day: str,
    output_parquet: Path,
    cleaned_paths: list[Path],
    snap: str,
    data_root: Path,
) -> list[str]:
    """Build argv for ``scripts/materialize_run_fact_v1.py``."""
    cmd: list[str] = [
        py,
        str(repo_root() / "scripts" / "materialize_run_fact_v1.py"),
        "--data-root",
        str(data_root),
        "--source-snapshot-id",
        snap,
        "--run-end-gaming-day",
        run_end_day,
        "--l1-preprocess-gaming-day",
        run_end_day,
    ]
    for p in cleaned_paths:
        cmd.extend(["--input", str(p.resolve())])
    cmd.extend(["--output-parquet", str(output_parquet.resolve())])
    return cmd


def _trip_argv(
    *,
    py: str,
    trip_start_day: str,
    trip_fact_parquet: Path,
    trip_run_map_parquet: Path,
    run_fact_paths: list[Path],
    snap: str,
    data_root: Path,
    coverage_end_day: str | None = None,
) -> list[str]:
    """Build argv for ``scripts/materialize_trip_fact_v1.py``."""
    cmd: list[str] = [
        py,
        str(repo_root() / "scripts" / "materialize_trip_fact_v1.py"),
        "--data-root",
        str(data_root),
        "--source-snapshot-id",
        snap,
        "--trip-start-gaming-day",
        trip_start_day,
    ]
    for p in run_fact_paths:
        cmd.extend(["--input-run-fact", str(p.resolve())])
    if coverage_end_day is not None and str(coverage_end_day).strip():
        cmd.extend(["--coverage-end", str(coverage_end_day).strip()])
    cmd.extend(
        [
            "--trip-fact-output-parquet",
            str(trip_fact_parquet.resolve()),
            "--trip-run-map-output-parquet",
            str(trip_run_map_parquet.resolve()),
        ]
    )
    return cmd


def _parse_run_mvp_argv(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Parse optional CLI flags; return options dict and unknown tokens (should be empty)."""
    opts: dict[str, Any] = {
        "emit_trainer_local_parquet": False,
        "trainer_bridge_emit_only": False,
        "snapshot_id": None,
        "help": False,
    }
    unknown: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            opts["help"] = True
        elif a == "--emit-trainer-local-parquet":
            opts["emit_trainer_local_parquet"] = True
        elif a == "--trainer-bridge-emit-only":
            opts["trainer_bridge_emit_only"] = True
        elif a == "--snapshot-id":
            i += 1
            if i >= len(argv):
                raise ValueError("--snapshot-id requires a value")
            opts["snapshot_id"] = str(argv[i]).strip()
        else:
            unknown.append(a)
        i += 1
    return opts, unknown


def main(argv: list[str] | None = None) -> int:
    """Entry: resolve defaults, then preprocess / run_fact / trip_fact."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        opts, unknown = _parse_run_mvp_argv(argv)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if opts.get("help"):
        print(__doc__, end="")
        return 0
    if unknown:
        print(f"Unknown arguments: {unknown!r}", file=sys.stderr)
        print("Use -h / --help.", file=sys.stderr)
        return 2

    root = repo_root()
    data_root = (root / "data").resolve()

    if opts.get("trainer_bridge_emit_only"):
        sid = (opts.get("snapshot_id") or os.environ.get(_ENV_SNAPSHOT_ID, "").strip()).strip()
        if not sid:
            print(
                "bridge-emit-only requires --snapshot-id or PARALLEL_LDA_MVP_SNAPSHOT_ID.",
                file=sys.stderr,
            )
            return 2
        snap_root = data_root / "parallel_lda_mvp" / sid
        if not snap_root.is_dir():
            print(f"snapshot root not found: {snap_root}", file=sys.stderr)
            return 2
        from parallel_lda_mvp.trainer_bridge_mvp import emit_trainer_local_parquet

        print(
            f"[parallel_lda_mvp] trainer bridge emit-only -> {data_root / 'mvp_trainer_bridge'} "
            f"(snap={sid})",
            flush=True,
        )
        emit_trainer_local_parquet(
            snap_root=snap_root,
            data_dir=data_root,
        )
        return 0

    t_bet_paths = resolve_t_bet_paths(data_root)
    t_session = resolve_t_session(data_root)
    gaming_yms = resolve_gaming_ym_list(t_bet_paths)
    ym_last = gaming_yms[-1]
    coverage_end_day = gaming_days_in_month(ym_last)[-1]
    snap = infer_snapshot_id(t_bet_paths)
    cutoff = resolve_cutoff(ym_last)

    print(f"[parallel_lda_mvp] gaming_ym_span={gaming_yms} source_snapshot_id={snap}")
    print(f"[parallel_lda_mvp] cutoff_dtm={cutoff.isoformat()} (from last month in span)")
    print(f"[parallel_lda_mvp] t_session={t_session}")
    preview = [str(p) for p in t_bet_paths[:5]]
    more = f" (+{len(t_bet_paths) - 5} more)" if len(t_bet_paths) > 5 else ""
    print(f"[parallel_lda_mvp] t_bet files ({len(t_bet_paths)}): {preview}{more}")

    snap_root = data_root / "parallel_lda_mvp" / snap
    snap_root.mkdir(parents=True, exist_ok=True)
    eligible_path = snap_root / "eligible_player_ids.parquet"

    ingest_default = root / "schema" / "preprocess_l0_data_contract_registry.yaml"
    ingest_yaml = ingest_default if ingest_default.is_file() else None

    from parallel_lda_mvp.eligible_builder import (
        build_eligible_player_ids_parquet,
        mapping_input_fingerprint,
        resolve_canonical_mapping_parquet_path,
    )
    from parallel_lda_mvp.session_for_mapping import (
        SESSION_MAPPING_CLEAN_LOGIC_VERSION,
        prepare_session_parquet_for_canonical_mapping,
    )
    from pipelines.layered_data_assets.core.preprocess_bet_v1 import PREPROCESS_MONTH_BATCH_STAMP

    session_for_mapping = prepare_session_parquet_for_canonical_mapping(t_session)
    print(
        f"[parallel_lda_mvp] session_for_mapping={session_for_mapping} "
        f"(clean_logic={SESSION_MAPPING_CLEAN_LOGIC_VERSION})"
    )

    print("[parallel_lda_mvp] mapping_input_fingerprint (stream session) …", flush=True)
    t0 = time.perf_counter()
    map_fp, _ = mapping_input_fingerprint(
        session_for_mapping,
        cutoff,
        cleaning_logic_version=SESSION_MAPPING_CLEAN_LOGIC_VERSION,
    )
    print(f"[parallel_lda_mvp] mapping_input_fingerprint ok {time.perf_counter() - t0:.1f}s", flush=True)

    t0 = time.perf_counter()
    ingest_sha = _ingest_yaml_content_sha256(ingest_yaml)
    print(f"[parallel_lda_mvp] ingest_yaml_sha ok {time.perf_counter() - t0:.2f}s", flush=True)
    scratch_dir = snap_root / ".mvp_scratch"

    print("[parallel_lda_mvp] eligible_player_ids (mapping cache / trainer) …", flush=True)
    t0 = time.perf_counter()
    _, mapping_identity_health = build_eligible_player_ids_parquet(
        session_parquet=t_session,
        cutoff_dtm=cutoff,
        output_parquet=eligible_path,
    )
    print(f"[parallel_lda_mvp] eligible_player_ids ok {time.perf_counter() - t0:.1f}s -> {eligible_path.name}", flush=True)

    mapping_parquet_path = resolve_canonical_mapping_parquet_path(t_session, cutoff)
    print(f"[parallel_lda_mvp] canonical_mapping_parquet={mapping_parquet_path}", flush=True)

    py = sys.executable
    force = _force_recompute_months()
    print(
        f"[parallel_lda_mvp] {_ENV_FORCE_RECOMPUTE}={os.environ.get(_ENV_FORCE_RECOMPUTE, '')!r} "
        f"-> force_recompute_months={force}",
        flush=True,
    )
    preprocess_dirty_by_ym: dict[str, bool] = {}
    n_ym = len(gaming_yms)
    print(
        f"[parallel_lda_mvp] month_bet_sha start n_months={n_ym} "
        f"(tail={_MONTH_BET_SHA_TAIL_MONTHS_DEFAULT}m + cache + single DuckDB conn) …",
        flush=True,
    )
    t_mb = time.perf_counter()
    month_bet_sha_by_ym, sha_stats = _compute_month_bet_shas_for_span(
        gaming_yms,
        t_bet_paths,
        scratch_dir,
        force=force,
        coverage_end_gaming_day=coverage_end_day,
    )
    print(
        f"[parallel_lda_mvp] month_bet_sha done n={len(month_bet_sha_by_ym)} "
        f"mode={sha_stats.get('mode')} esc_full={sha_stats.get('escalated_full_span')} "
        f"cache_hits={sha_stats['cache_hits']} recomputed={sha_stats['recomputed']} "
        f"fallback_conn={sha_stats['fallback_separate_connects']} "
        f"fp_algo={sha_stats.get('month_bet_sha_fingerprint_algo')} "
        f"force={sha_stats.get('force_recompute_used')} "
        f"total={time.perf_counter() - t_mb:.1f}s",
        flush=True,
    )

    print(f"[parallel_lda_mvp] phase preprocess+run_fact months={len(gaming_yms)} force={force}")
    for ym in gaming_yms:
        out_root = snap_root / f"gaming_ym={ym}"
        month_bet_sha = month_bet_sha_by_ym[ym]
        cached = _month_skip_keys_cached(out_root)
        if (
            not force
            and cached is not None
            and month_bet_sha == cached[0]
            and map_fp == cached[1]
            and ingest_sha == cached[2]
            and PREPROCESS_MONTH_BATCH_STAMP == cached[3]
        ):
            print(
                f"[parallel_lda_mvp] skip gaming_ym={ym} "
                f"(L0 month slice + mapping + ingest + preprocess batch stamp unchanged; "
                f"set {_ENV_FORCE_RECOMPUTE}=1 to rebuild)"
            )
            sum_p = out_root / "mvp_summary.json"
            if sum_p.is_file():
                try:
                    prior = json.loads(sum_p.read_text(encoding="utf-8"))
                    preprocess_dirty_by_ym[ym] = bool(prior.get("preprocess_dirty_any", False))
                except (OSError, json.JSONDecodeError):
                    preprocess_dirty_by_ym[ym] = False
            else:
                preprocess_dirty_by_ym[ym] = False
            continue

        out_root.mkdir(parents=True, exist_ok=True)
        print(f"[parallel_lda_mvp] ym={ym} preprocess (then run_fact)")
        t_pp = time.perf_counter()
        cleaned_paths, dirty_preprocess = _preprocess_cleaned_paths_for_month(
            ym=ym,
            out_root=out_root,
            snap=snap,
            data_root=data_root,
            root=root,
            t_bet_paths=t_bet_paths,
            eligible_path=eligible_path,
            ingest_yaml=ingest_yaml,
            py=py,
        )
        print(
            f"[parallel_lda_mvp] ym={ym} preprocess+split ok {time.perf_counter() - t_pp:.1f}s "
            f"cleaned_days={len(cleaned_paths)}",
            flush=True,
        )
        preprocess_dirty_by_ym[ym] = bool(dirty_preprocess)
        _run_runfact_for_month(
            ym=ym,
            out_root=out_root,
            snap=snap,
            data_root=data_root,
            root=root,
            cleaned_paths=cleaned_paths,
            mapping_parquet=mapping_parquet_path,
            py=py,
            dirty_preprocess=dirty_preprocess,
        )

    print("[parallel_lda_mvp] span_run_fact fingerprint …", flush=True)
    t_sp = time.perf_counter()
    span_paths = _expected_span_run_fact_paths(snap_root=snap_root, gaming_yms=gaming_yms)
    span_fp = _span_run_fact_input_fingerprint(span_paths)
    print(
        f"[parallel_lda_mvp] span_fp={span_fp[:10]}… run_fact_parts={len(span_paths)} "
        f"coverage_end={coverage_end_day} {time.perf_counter() - t_sp:.2f}s",
        flush=True,
    )

    print("[parallel_lda_mvp] prior mvp_summary json …", flush=True)
    t_pr = time.perf_counter()
    prior_summary_by_ym: dict[str, dict[str, Any]] = {}
    for ym in gaming_yms:
        sum_p = snap_root / f"gaming_ym={ym}" / "mvp_summary.json"
        if sum_p.is_file():
            try:
                prior_summary_by_ym[ym] = json.loads(sum_p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
    print(f"[parallel_lda_mvp] prior_summaries n={len(prior_summary_by_ym)} {time.perf_counter() - t_pr:.2f}s", flush=True)
    for ym in gaming_yms:
        (snap_root / f"gaming_ym={ym}").mkdir(parents=True, exist_ok=True)
    print(f"[parallel_lda_mvp] phase trip_fact prior_summaries={len(prior_summary_by_ym)}")
    _run_trip_span_batched(
        gaming_yms=gaming_yms,
        snap_root=snap_root,
        snap=snap,
        span_run_fact_paths=span_paths,
        coverage_end_day=coverage_end_day,
        span_run_fact_input_fingerprint=span_fp,
        month_bet_sha_by_ym=month_bet_sha_by_ym,
        map_fp=map_fp,
        ingest_sha=ingest_sha,
        preprocess_stamp=PREPROCESS_MONTH_BATCH_STAMP,
        prior_summary_by_ym=prior_summary_by_ym,
    )

    print(f"[parallel_lda_mvp] phase mvp_summary months={len(gaming_yms)}")
    from parallel_lda_mvp.canonical_mapping_runtime_config import (
        LATE_ARRIVAL_RECOMPUTE_HOURS,
        MAPPING_HARD_STALE_LIMIT_MIN,
        MAPPING_MAX_STALENESS_MIN,
        MAPPING_REFRESH_INTERVAL_MIN,
    )
    for ym in gaming_yms:
        out_root = snap_root / f"gaming_ym={ym}"
        out_root.mkdir(parents=True, exist_ok=True)
        month_bet_sha = month_bet_sha_by_ym[ym]
        days = gaming_days_in_month(ym)
        summary: dict[str, Any] = {
            "coverage_end_gaming_day_trip": coverage_end_day,
            "cutoff_dtm": cutoff.isoformat(),
            "days": days,
            "eligible_parquet": str(eligible_path.as_posix()),
            "gaming_ym": ym,
            "gaming_ym_span_default": gaming_yms,
            "ingest_yaml_content_sha256": ingest_sha,
            "ingestion_fix_registry_yaml": str(ingest_yaml.as_posix()) if ingest_yaml else None,
            "l1_partition_layout": "flat_month_v1",
            "late_arrival_recompute_hours": int(LATE_ARRIVAL_RECOMPUTE_HOURS),
            "late_arrival_window_days": sha_stats["late_arrival_window_days"],
            "mapping_cache_fingerprint": map_fp,
            "mapping_hard_stale_limit_min": int(MAPPING_HARD_STALE_LIMIT_MIN),
            "mapping_identity_health": dict(mapping_identity_health),
            "mapping_max_staleness_min": int(MAPPING_MAX_STALENESS_MIN),
            "mapping_refresh_interval_min": int(MAPPING_REFRESH_INTERVAL_MIN),
            "month_bet_sha_escalated_full_span": bool(sha_stats.get("escalated_full_span")),
            "month_bet_sha_fingerprint_algo": sha_stats.get("month_bet_sha_fingerprint_algo"),
            "month_bet_sha_mode": sha_stats.get("mode"),
            "month_bet_sha_policy_version": sha_stats.get("month_bet_sha_policy_version"),
            "month_bet_sha_strategy": sha_stats.get("month_bet_sha_strategy"),
            "month_bet_sha_tail_months_default": sha_stats.get("tail_months_default"),
            "force_recompute_used": bool(sha_stats.get("force_recompute_used")),
            "output_root": str(out_root.as_posix()),
            "preprocess_dirty_any": preprocess_dirty_by_ym.get(ym, False),
            "preprocess_month_batch_stamp": PREPROCESS_MONTH_BATCH_STAMP,
            "session_mapping_clean_logic_version": SESSION_MAPPING_CLEAN_LOGIC_VERSION,
            "session_parquet_for_mapping": str(session_for_mapping.as_posix()),
            "snap_root": str(snap_root.as_posix()),
            "source_snapshot_id": snap,
            "span_run_fact_input_fingerprint": span_fp,
            "t_bet_month_content_sha256": month_bet_sha,
            "t_bet_paths": [str(p.as_posix()) for p in t_bet_paths],
            "t_session": str(t_session.as_posix()),
        }
        (out_root / "mvp_summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"[parallel_lda_mvp] OK gaming_ym={ym} -> {out_root / 'mvp_summary.json'}")

    print(f"OK MVP finished span under {snap_root}")
    if opts.get("emit_trainer_local_parquet"):
        from parallel_lda_mvp.trainer_bridge_mvp import emit_trainer_local_parquet

        print(
            f"[parallel_lda_mvp] trainer bridge after MVP -> {data_root / 'mvp_trainer_bridge'} "
            f"(snap={snap})",
            flush=True,
        )
        emit_trainer_local_parquet(
            snap_root=snap_root,
            data_dir=data_root,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
