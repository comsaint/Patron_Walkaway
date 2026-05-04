"""MVP orchestrator: preprocess (rated-only) -> run_fact -> trip_fact under ``gaming_ym`` layout.

Calls existing ``scripts/*.py`` only (does not edit ``pipelines.layered_data_assets``).

**No CLI arguments.** Run from repo root:

    python -m parallel_lda_mvp.run_mvp

Defaults (first match wins for paths):

- ``t_bet``: ``PARALLEL_LDA_MVP_T_BET`` if set, else ``data/gmwds_t_bet.parquet``, else all
  ``data/l0_layered/*/t_bet/**/*.parquet`` (sorted).
- ``t_session``: ``PARALLEL_LDA_MVP_T_SESSION`` if set, else ``data/gmwds_t_session.parquet``.
- ``gaming_ym``: ``PARALLEL_LDA_MVP_GAMING_YM`` if set, else calendar month of
  ``MAX(gaming_day)`` in the resolved ``t_bet`` files (DuckDB).
- ``source_snapshot_id``: ``PARALLEL_LDA_MVP_SNAPSHOT_ID`` if set, else parent folder after
  ``l0_layered/`` when path matches, else ``snap_mvp_<sha16>`` from bet path fingerprints.
- ``cutoff_dtm``: ``PARALLEL_LDA_MVP_CUTOFF_DTM`` if set (ISO), else last microsecond of that
  month in ``Asia/Hong_Kong``.

Optional: ``python -m parallel_lda_mvp.run_mvp -h`` / ``--help`` prints this text.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

_ENV_T_BET = "PARALLEL_LDA_MVP_T_BET"
_ENV_T_SESSION = "PARALLEL_LDA_MVP_T_SESSION"
_ENV_GAMING_YM = "PARALLEL_LDA_MVP_GAMING_YM"
_ENV_SNAPSHOT_ID = "PARALLEL_LDA_MVP_SNAPSHOT_ID"
_ENV_CUTOFF = "PARALLEL_LDA_MVP_CUTOFF_DTM"


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


def infer_gaming_ym_from_t_bet(paths: list[Path]) -> str:
    """Infer ``YYYY-MM`` from ``MAX(gaming_day)`` across given Parquet files."""
    import duckdb

    if not paths:
        raise ValueError("infer_gaming_ym_from_t_bet requires non-empty paths")
    rp_list = ", ".join(f"'{p.resolve().as_posix().replace(chr(39), chr(39) + chr(39))}'" for p in paths)
    con = duckdb.connect()
    try:
        row = con.execute(
            f"""
            SELECT strftime(
              date_trunc('month', MAX(TRY_CAST(gaming_day AS DATE))),
              '%Y-%m'
            )
            FROM read_parquet([{rp_list}])
            """
        ).fetchone()
    finally:
        con.close()
    if not row or row[0] is None:
        raise RuntimeError("Could not infer gaming_ym: gaming_day missing or null in t_bet inputs")
    ym = str(row[0]).strip()
    if not re.fullmatch(r"\d{4}-\d{2}", ym):
        raise RuntimeError(f"Inferred gaming_ym invalid: {ym!r}")
    return ym


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


def resolve_gaming_ym(bet_paths: list[Path]) -> str:
    """Return ``gaming_ym`` from env or inference."""
    raw = os.environ.get(_ENV_GAMING_YM, "").strip()
    if raw:
        if not re.fullmatch(r"\d{4}-\d{2}", raw):
            raise ValueError(f"{_ENV_GAMING_YM} must be YYYY-MM, got {raw!r}")
        return raw
    return infer_gaming_ym_from_t_bet(bet_paths)


def resolve_cutoff(gaming_ym: str) -> datetime:
    """Return cutoff from env or end-of-month HK default."""
    raw = os.environ.get(_ENV_CUTOFF, "").strip()
    if raw:
        return _parse_cutoff_dtm(raw)
    return default_cutoff_for_gaming_ym(gaming_ym)


def _run_subprocess(argv: Sequence[str], *, cwd: Path) -> None:
    """Run subprocess; raise on non-zero exit."""
    p = subprocess.run(list(argv), cwd=str(cwd), env=os.environ.copy())
    if p.returncode != 0:
        raise RuntimeError(f"command failed rc={p.returncode}: {' '.join(argv)}")


def _preprocess_argv(
    *,
    py: str,
    gaming_day: str,
    out_dir: Path,
    t_bet_paths: list[Path],
    snap: str,
    data_root: Path,
    eligible: Path,
    ingest_yaml: Path | None,
) -> list[str]:
    """Build argv for ``scripts/preprocess_bet_v1.py``."""
    cmd: list[str] = [
        py,
        str(repo_root() / "scripts" / "preprocess_bet_v1.py"),
        "--data-root",
        str(data_root),
        "--source-snapshot-id",
        snap,
        "--gaming-day",
        gaming_day,
        "--eligible-player-ids-parquet",
        str(eligible),
    ]
    for p in t_bet_paths:
        cmd.extend(["--input", str(p.resolve())])
    if ingest_yaml is not None:
        cmd.extend(["--ingestion-fix-registry-yaml", str(ingest_yaml.resolve())])
    cmd.extend(["--output-dir", str(out_dir.resolve())])
    return cmd


def _run_fact_argv(
    *,
    py: str,
    run_end_day: str,
    out_dir: Path,
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
    cmd.extend(["--output-dir", str(out_dir.resolve())])
    return cmd


def _trip_argv(
    *,
    py: str,
    trip_start_day: str,
    trip_out: Path,
    map_out: Path,
    run_fact_paths: list[Path],
    snap: str,
    data_root: Path,
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
    cmd.extend(
        [
            "--trip-fact-output-dir",
            str(trip_out.resolve()),
            "--trip-run-map-output-dir",
            str(map_out.resolve()),
        ]
    )
    return cmd


def main(argv: list[str] | None = None) -> int:
    """Entry: resolve defaults, then preprocess / run_fact / trip_fact."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__, end="")
        return 0
    if argv:
        print("This runner accepts no arguments (only -h / --help).", file=sys.stderr)
        print("Override paths or ids with environment variables listed in the module docstring.", file=sys.stderr)
        return 2

    root = repo_root()
    data_root = (root / "data").resolve()
    t_bet_paths = resolve_t_bet_paths(data_root)
    t_session = resolve_t_session(data_root)
    ym = resolve_gaming_ym(t_bet_paths)
    snap = infer_snapshot_id(t_bet_paths)
    cutoff = resolve_cutoff(ym)
    days = gaming_days_in_month(ym)

    print(f"[parallel_lda_mvp] gaming_ym={ym} source_snapshot_id={snap}")
    print(f"[parallel_lda_mvp] cutoff_dtm={cutoff.isoformat()}")
    print(f"[parallel_lda_mvp] t_session={t_session}")
    preview = [str(p) for p in t_bet_paths[:5]]
    more = f" (+{len(t_bet_paths) - 5} more)" if len(t_bet_paths) > 5 else ""
    print(f"[parallel_lda_mvp] t_bet files ({len(t_bet_paths)}): {preview}{more}")

    out_root = data_root / "parallel_lda_mvp" / snap / f"gaming_ym={ym}"
    out_root.mkdir(parents=True, exist_ok=True)
    eligible_path = out_root / "eligible_player_ids.parquet"

    ingest_default = root / "schema" / "preprocess_bet_ingestion_fix_registry.yaml"
    ingest_yaml = ingest_default if ingest_default.is_file() else None

    from parallel_lda_mvp.eligible_builder import build_eligible_player_ids_parquet
    from parallel_lda_mvp.session_for_mapping import (
        SESSION_MAPPING_CLEAN_LOGIC_VERSION,
        prepare_session_parquet_for_canonical_mapping,
    )

    session_for_mapping = prepare_session_parquet_for_canonical_mapping(t_session)
    print(
        f"[parallel_lda_mvp] session_for_mapping={session_for_mapping} "
        f"(clean_logic={SESSION_MAPPING_CLEAN_LOGIC_VERSION})"
    )

    build_eligible_player_ids_parquet(
        session_parquet=t_session,
        cutoff_dtm=cutoff,
        output_parquet=eligible_path,
    )

    py = sys.executable

    cleaned_paths: list[Path] = []
    for gd in days:
        part_dir = out_root / "t_bet" / f"gaming_day={gd}"
        cmd = _preprocess_argv(
            py=py,
            gaming_day=gd,
            out_dir=part_dir,
            t_bet_paths=t_bet_paths,
            snap=snap,
            data_root=data_root,
            eligible=eligible_path,
            ingest_yaml=ingest_yaml,
        )
        _run_subprocess(cmd, cwd=root)
        cleaned_paths.append(part_dir / "cleaned.parquet")

    run_fact_paths: list[Path] = []
    for gd in days:
        rdir = out_root / "run_fact" / f"run_end_gaming_day={gd}"
        cmd = _run_fact_argv(
            py=py,
            run_end_day=gd,
            out_dir=rdir,
            cleaned_paths=cleaned_paths,
            snap=snap,
            data_root=data_root,
        )
        _run_subprocess(cmd, cwd=root)
        run_fact_paths.append(rdir / "run_fact.parquet")

    for gd in days:
        tdir = out_root / "trip_fact" / f"trip_start_gaming_day={gd}"
        mdir = out_root / "trip_run_map" / f"trip_start_gaming_day={gd}"
        cmd = _trip_argv(
            py=py,
            trip_start_day=gd,
            trip_out=tdir,
            map_out=mdir,
            run_fact_paths=run_fact_paths,
            snap=snap,
            data_root=data_root,
        )
        _run_subprocess(cmd, cwd=root)

    summary: dict[str, Any] = {
        "gaming_ym": ym,
        "source_snapshot_id": snap,
        "cutoff_dtm": cutoff.isoformat(),
        "t_session": str(t_session.as_posix()),
        "session_mapping_clean_logic_version": SESSION_MAPPING_CLEAN_LOGIC_VERSION,
        "session_parquet_for_mapping": str(session_for_mapping.as_posix()),
        "t_bet_paths": [str(p.as_posix()) for p in t_bet_paths],
        "output_root": str(out_root.as_posix()),
        "eligible_parquet": str(eligible_path.as_posix()),
        "ingestion_fix_registry_yaml": str(ingest_yaml.as_posix()) if ingest_yaml else None,
        "days": days,
    }
    (out_root / "mvp_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"OK MVP wrote summary -> {out_root / 'mvp_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
