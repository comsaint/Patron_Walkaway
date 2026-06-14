"""Player-level DQ artifacts: known test accounts and abnormal game pace."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import duckdb

from trainer_hightier.config import (
    CASINO_PLAYER_ID_CLEAN_SQL,
    DuckDbRuntimeConfig,
    PlayerDqConfig,
)
from trainer_hightier.utils.bet_l0_preprocess import (
    _bet_artifact_manifest_block,
    _path_posix,
    cleaned_bet_dataset_has_any_parquet,
    resolved_cleaned_bet_read_parquet_sql,
)
from trainer_hightier.utils.duckdb_runtime import execute_sql_with_progress_oom_retry
from trainer_hightier.utils.source_manifest_v2 import sha256_file_bytes, write_json_atomic

logger = logging.getLogger("trainer_hightier")

PLAYER_DQ_KIND: Final[str] = "player_dq_v1"
PLAYER_DQ_MANIFEST_NAME: Final[str] = "player_dq.cache.json"
FLAGS_BASENAME: Final[str] = "player_dq_flags.parquet"
HARD_EXCLUDE_BASENAME: Final[str] = "player_dq_hard_exclude.parquet"


def default_player_dq_artifacts_dir(*, package_dir: Path | None = None) -> Path:
    """Return ``trainer_hightier/artifacts/dq``."""
    base = Path(__file__).resolve().parents[1] if package_dir is None else package_dir
    return (base / "artifacts" / "dq").resolve()


def _player_dq_module_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _policy_blob_sha256(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _bet_base_fingerprint_sha256_hex(base_cleaned_parquet: Path) -> str:
    block = _bet_artifact_manifest_block(Path(base_cleaned_parquet).resolve())
    blob = json.dumps(block, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def hard_exclude_policy_fingerprint_sha256_hex(cfg: PlayerDqConfig) -> str:
    """Fingerprint for rules that change segmented bet row universe."""
    if not cfg.enabled:
        return ""
    return _policy_blob_sha256(
        {
            "kind": "player_dq_hard_exclude_v1",
            "known_test_casino_player_id_prefixes": tuple(cfg.known_test_casino_player_id_prefixes),
            "hard_distinct_game_id_per_hour": int(cfg.hard_distinct_game_id_per_hour),
            "hard_distinct_game_id_per_day": int(cfg.hard_distinct_game_id_per_day),
            "player_dq_module_sha256": _player_dq_module_sha256(),
        },
    )


def flags_policy_fingerprint_sha256_hex(cfg: PlayerDqConfig) -> str:
    """Fingerprint for review thresholds and flags artifact sidecar."""
    if not cfg.enabled:
        return ""
    return _policy_blob_sha256(
        {
            "kind": "player_dq_flags_v1",
            "known_test_casino_player_id_prefixes": tuple(cfg.known_test_casino_player_id_prefixes),
            "hard_distinct_game_id_per_hour": int(cfg.hard_distinct_game_id_per_hour),
            "hard_distinct_game_id_per_day": int(cfg.hard_distinct_game_id_per_day),
            "review_distinct_game_id_per_hour": int(cfg.review_distinct_game_id_per_hour),
            "review_distinct_game_id_per_day": int(cfg.review_distinct_game_id_per_day),
            "player_dq_module_sha256": _player_dq_module_sha256(),
        },
    )


def _known_test_prefix_sql(prefixes: tuple[str, ...]) -> str:
    if not prefixes:
        return "FALSE"
    parts = [
        f"clean_casino LIKE '{str(p).replace(chr(39), chr(39) + chr(39))}%'"
        for p in prefixes
    ]
    return " OR ".join(parts)


def _compose_player_dq_sql(
    *,
    bet_from: str,
    map_esc: str,
    cfg: PlayerDqConfig,
) -> str:
    """Return DuckDB SQL producing one row per flagged player."""
    hard_h = int(cfg.hard_distinct_game_id_per_hour)
    hard_d = int(cfg.hard_distinct_game_id_per_day)
    rev_h = int(cfg.review_distinct_game_id_per_hour)
    rev_d = int(cfg.review_distinct_game_id_per_day)
    known_pred = _known_test_prefix_sql(cfg.known_test_casino_player_id_prefixes)
    clean_sql = CASINO_PLAYER_ID_CLEAN_SQL
    return f"""
WITH pace AS (
  SELECT
    TRY_CAST(player_id AS BIGINT) AS player_id,
    MAX(g) FILTER (WHERE grain = 'hour') AS max_distinct_game_id_per_hour,
    MAX(g) FILTER (WHERE grain = 'day') AS max_distinct_game_id_per_day
  FROM (
    SELECT
      TRY_CAST(player_id AS BIGINT) AS player_id,
      'hour' AS grain,
      date_trunc('hour', CAST(payout_complete_dtm AS TIMESTAMPTZ)) AS bucket,
      COUNT(DISTINCT TRY_CAST(game_id AS BIGINT)) AS g
    FROM {bet_from}
    WHERE player_id IS NOT NULL
      AND game_id IS NOT NULL
      AND payout_complete_dtm IS NOT NULL
    GROUP BY 1, 2, 3
    UNION ALL
    SELECT
      TRY_CAST(player_id AS BIGINT),
      'day',
      date_trunc('day', CAST(payout_complete_dtm AS TIMESTAMPTZ)),
      COUNT(DISTINCT TRY_CAST(game_id AS BIGINT))
    FROM {bet_from}
    WHERE player_id IS NOT NULL
      AND game_id IS NOT NULL
      AND payout_complete_dtm IS NOT NULL
    GROUP BY 1, 2, 3
  ) s
  GROUP BY 1
),
known AS (
  SELECT DISTINCT TRY_CAST(player_id AS BIGINT) AS player_id
  FROM (
    SELECT
      TRY_CAST(player_id AS BIGINT) AS player_id,
      {clean_sql} AS clean_casino
    FROM read_parquet('{map_esc}')
    WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
  ) m
  WHERE clean_casino IS NOT NULL
    AND ({known_pred})
),
players AS (
  SELECT player_id FROM pace
  UNION
  SELECT player_id FROM known
)
SELECT
  p.player_id,
  (k.player_id IS NOT NULL) AS is_known_test_account,
  (
    COALESCE(pc.max_distinct_game_id_per_hour, 0) > {hard_h}
    OR COALESCE(pc.max_distinct_game_id_per_day, 0) > {hard_d}
  ) AS is_hard_dq_pace,
  (
    (COALESCE(pc.max_distinct_game_id_per_hour, 0) > {rev_h}
     OR COALESCE(pc.max_distinct_game_id_per_day, 0) > {rev_d})
    AND NOT (
      COALESCE(pc.max_distinct_game_id_per_hour, 0) > {hard_h}
      OR COALESCE(pc.max_distinct_game_id_per_day, 0) > {hard_d}
    )
  ) AS is_review_pace,
  (
    (k.player_id IS NOT NULL)
    OR COALESCE(pc.max_distinct_game_id_per_hour, 0) > {hard_h}
    OR COALESCE(pc.max_distinct_game_id_per_day, 0) > {hard_d}
  ) AS exclude_hard,
  COALESCE(pc.max_distinct_game_id_per_hour, 0)::BIGINT AS max_distinct_game_id_per_hour,
  COALESCE(pc.max_distinct_game_id_per_day, 0)::BIGINT AS max_distinct_game_id_per_day
FROM players p
LEFT JOIN pace pc ON p.player_id = pc.player_id
LEFT JOIN known k ON p.player_id = k.player_id
WHERE (k.player_id IS NOT NULL)
   OR COALESCE(pc.max_distinct_game_id_per_hour, 0) > {rev_h}
   OR COALESCE(pc.max_distinct_game_id_per_day, 0) > {rev_d}
""".strip()


def hard_exclude_anti_join_sql(*, hard_exclude_parquet_esc: str, player_id_expr: str = "b.player_id") -> str:
    """Return SQL fragment excluding hard-DQ ``player_id`` values."""
    return (
        f" AND TRY_CAST({player_id_expr} AS BIGINT) NOT IN ("
        f"SELECT TRY_CAST(player_id AS BIGINT) "
        f"FROM read_parquet('{hard_exclude_parquet_esc}') "
        f"WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL)"
    )


def _manifest_path(artifacts_dir: Path) -> Path:
    return Path(artifacts_dir).resolve() / PLAYER_DQ_MANIFEST_NAME


def player_dq_cache_is_hit(
    *,
    artifacts_dir: Path,
    bet_base_fingerprint_sha256_hex: str,
    canonical_mapping_sha256_hex: str,
    flags_policy_fingerprint: str,
) -> bool:
    """Return True when cached player DQ artifacts match inputs."""
    flags_p = Path(artifacts_dir).resolve() / FLAGS_BASENAME
    hard_p = Path(artifacts_dir).resolve() / HARD_EXCLUDE_BASENAME
    man_p = _manifest_path(artifacts_dir)
    if not flags_p.is_file() or not hard_p.is_file() or not man_p.is_file():
        return False
    try:
        prev = json.loads(man_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    return (
        str(prev.get("bet_base_fingerprint_sha256_hex", "")).strip()
        == str(bet_base_fingerprint_sha256_hex).strip()
        and str(prev.get("canonical_mapping_sha256_hex", "")).strip()
        == str(canonical_mapping_sha256_hex).strip()
        and str(prev.get("flags_policy_fingerprint_sha256_hex", "")).strip()
        == str(flags_policy_fingerprint).strip()
    )


def _write_manifest(
    *,
    artifacts_dir: Path,
    bet_base_fingerprint_sha256_hex: str,
    canonical_mapping_sha256_hex: str,
    hard_policy_fingerprint: str,
    flags_policy_fingerprint: str,
    hard_player_count: int,
    review_player_count: int,
    known_test_player_count: int,
) -> None:
    payload = {
        "kind": PLAYER_DQ_KIND,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "bet_base_fingerprint_sha256_hex": str(bet_base_fingerprint_sha256_hex).strip(),
        "canonical_mapping_sha256_hex": str(canonical_mapping_sha256_hex).strip(),
        "hard_exclude_policy_fingerprint_sha256_hex": str(hard_policy_fingerprint).strip(),
        "flags_policy_fingerprint_sha256_hex": str(flags_policy_fingerprint).strip(),
        "player_dq_module_sha256": _player_dq_module_sha256(),
        "hard_player_count": int(hard_player_count),
        "review_player_count": int(review_player_count),
        "known_test_player_count": int(known_test_player_count),
        "flags_parquet": str((artifacts_dir / FLAGS_BASENAME).resolve()),
        "hard_exclude_parquet": str((artifacts_dir / HARD_EXCLUDE_BASENAME).resolve()),
    }
    write_json_atomic(_manifest_path(artifacts_dir), payload)


def _materialize_player_dq_artifacts(
    *,
    bet_base_parquet: Path,
    canonical_mapping_parquet: Path,
    artifacts_dir: Path,
    cfg: PlayerDqConfig,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> dict[str, int]:
    """Compute and write flags + hard-exclude Parquet artifacts."""
    base = Path(bet_base_parquet).resolve()
    cmap = Path(canonical_mapping_parquet).resolve()
    out_dir = Path(artifacts_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not cleaned_bet_dataset_has_any_parquet(base):
        raise FileNotFoundError(f"bet base missing for player DQ: {base}")
    if not cmap.is_file():
        raise FileNotFoundError(f"canonical mapping missing for player DQ: {cmap}")
    bet_from = resolved_cleaned_bet_read_parquet_sql(base)
    map_esc = _path_posix(cmap).replace("'", "''")
    flags_p = out_dir / FLAGS_BASENAME
    hard_p = out_dir / HARD_EXCLUDE_BASENAME
    flags_esc = _path_posix(flags_p).replace("'", "''")
    hard_esc = _path_posix(hard_p).replace("'", "''")
    inner = _compose_player_dq_sql(bet_from=bet_from, map_esc=map_esc, cfg=cfg)
    sql_flags = f"COPY ({inner}) TO '{flags_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
    sql_hard = f"""
COPY (
  SELECT player_id
  FROM ({inner}) AS f
  WHERE exclude_hard
) TO '{hard_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    execute_sql_with_progress_oom_retry(
        duckdb_runtime,
        sql_flags,
        desc="[Step 2b] player DQ flags",
        join_timeout_s=7200.0,
    )
    execute_sql_with_progress_oom_retry(
        duckdb_runtime,
        sql_hard,
        desc="[Step 2b] player DQ hard exclude",
        join_timeout_s=7200.0,
    )
    con = duckdb.connect(database=":memory:")
    try:
        stats = con.execute(
            f"""
            SELECT
              COUNT(*) FILTER (WHERE exclude_hard) AS hard_cnt,
              COUNT(*) FILTER (WHERE is_review_pace) AS review_cnt,
              COUNT(*) FILTER (WHERE is_known_test_account) AS known_cnt
            FROM ({inner}) AS f
            """,
        ).fetchone()
    finally:
        con.close()
    return {
        "hard_player_count": int(stats[0] or 0),
        "review_player_count": int(stats[1] or 0),
        "known_test_player_count": int(stats[2] or 0),
    }


def materialize_player_dq_cached(
    *,
    bet_base_parquet: Path,
    canonical_mapping_parquet: Path,
    cfg: PlayerDqConfig,
    duckdb_runtime: DuckDbRuntimeConfig,
    artifacts_dir: Path | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Materialize player DQ flags and hard-exclude list with optional cache hit."""
    t0 = time.perf_counter()
    if not cfg.enabled:
        return {
            "player_dq_enabled": False,
            "player_dq_cache_hit": False,
            "player_dq_elapsed_seconds": 0.0,
            "hard_exclude_policy_fingerprint_sha256_hex": "",
            "flags_policy_fingerprint_sha256_hex": "",
            "player_dq_hard_exclude_parquet": None,
            "player_dq_flags_parquet": None,
            "player_dq_hard_player_count": 0,
            "player_dq_review_player_count": 0,
            "player_dq_known_test_player_count": 0,
        }
    out_dir = (
        Path(cfg.artifacts_dir).resolve()
        if cfg.artifacts_dir is not None
        else default_player_dq_artifacts_dir()
    )
    base_fp = _bet_base_fingerprint_sha256_hex(Path(bet_base_parquet))
    map_fp = sha256_file_bytes(Path(canonical_mapping_parquet))
    hard_fp = hard_exclude_policy_fingerprint_sha256_hex(cfg)
    flags_fp = flags_policy_fingerprint_sha256_hex(cfg)
    cache_hit = use_cache and player_dq_cache_is_hit(
        artifacts_dir=out_dir,
        bet_base_fingerprint_sha256_hex=base_fp,
        canonical_mapping_sha256_hex=map_fp,
        flags_policy_fingerprint=flags_fp,
    )
    if not cache_hit:
        counts = _materialize_player_dq_artifacts(
            bet_base_parquet=bet_base_parquet,
            canonical_mapping_parquet=canonical_mapping_parquet,
            artifacts_dir=out_dir,
            cfg=cfg,
            duckdb_runtime=duckdb_runtime,
        )
        _write_manifest(
            artifacts_dir=out_dir,
            bet_base_fingerprint_sha256_hex=base_fp,
            canonical_mapping_sha256_hex=map_fp,
            hard_policy_fingerprint=hard_fp,
            flags_policy_fingerprint=flags_fp,
            hard_player_count=counts["hard_player_count"],
            review_player_count=counts["review_player_count"],
            known_test_player_count=counts["known_test_player_count"],
        )
    else:
        man = json.loads(_manifest_path(out_dir).read_text(encoding="utf-8"))
        counts = {
            "hard_player_count": int(man.get("hard_player_count") or 0),
            "review_player_count": int(man.get("review_player_count") or 0),
            "known_test_player_count": int(man.get("known_test_player_count") or 0),
        }
    flags_p = out_dir / FLAGS_BASENAME
    hard_p = out_dir / HARD_EXCLUDE_BASENAME
    logger.info(
        "[Step 2b] player DQ OK: hard=%d review=%d known_test=%d cache_hit=%s -> %s",
        counts["hard_player_count"],
        counts["review_player_count"],
        counts["known_test_player_count"],
        cache_hit,
        out_dir.resolve(),
    )
    return {
        "player_dq_enabled": True,
        "player_dq_cache_hit": bool(cache_hit),
        "player_dq_elapsed_seconds": round(time.perf_counter() - t0, 6),
        "hard_exclude_policy_fingerprint_sha256_hex": hard_fp,
        "flags_policy_fingerprint_sha256_hex": flags_fp,
        "player_dq_hard_exclude_parquet": str(hard_p.resolve()),
        "player_dq_flags_parquet": str(flags_p.resolve()),
        "player_dq_hard_player_count": counts["hard_player_count"],
        "player_dq_review_player_count": counts["review_player_count"],
        "player_dq_known_test_player_count": counts["known_test_player_count"],
    }
