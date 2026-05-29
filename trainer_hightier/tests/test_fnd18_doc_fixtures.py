"""FND-18 doc fixtures: clone-and-run DuckDB appendix SQL must stay reproducible."""

from __future__ import annotations

from pathlib import Path

import duckdb

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FND18_DIR = _REPO_ROOT / "doc" / "fixtures" / "fnd18"
_REQUIRED_FIXTURES = (
    _FND18_DIR / "canonical_player_mapping.parquet",
    _FND18_DIR / "canonical_patron_profile.csv",
    _FND18_DIR / "cleaned__gmwds_t_bet.parquet",
)
_MIN_BET_PARQUET_BYTES = 1_000_000
_HIGH_THEO_GAP_SQL = """
WITH mp AS (
  SELECT CAST(player_id AS BIGINT) AS player_id,
         CAST(canonical_id AS BIGINT) AS canonical_id
  FROM read_parquet(?)
),
p AS (
  SELECT CAST(canonical_id AS BIGINT) AS canonical_id,
         CAST(total_theo_win AS DOUBLE) AS p_theo
  FROM read_csv_auto(?, header=true)
),
b AS (
  SELECT mp.canonical_id,
         SUM(COALESCE(CAST(bt.theo_win AS DOUBLE), 0.0)) AS b_theo
  FROM read_parquet(?) bt
  JOIN mp ON CAST(bt.player_id AS BIGINT) = mp.player_id
  GROUP BY 1
)
SELECT COUNT(*)::BIGINT AS n
FROM p
JOIN b USING (canonical_id)
WHERE ABS(p.p_theo - b.b_theo) >= 100000
"""


def test_fnd18_fixture_files_tracked_and_non_empty() -> None:
    """All three appendix fixtures must exist (FINDINGS.md [FND-18] table)."""
    missing = [p for p in _REQUIRED_FIXTURES if not p.is_file()]
    assert not missing, f"missing FND-18 fixtures: {missing}"
    bet_path = _FND18_DIR / "cleaned__gmwds_t_bet.parquet"
    size = bet_path.stat().st_size
    assert size >= _MIN_BET_PARQUET_BYTES, (
        f"expected real bet parquet at {bet_path}, got {size} bytes "
        f"(min {_MIN_BET_PARQUET_BYTES}); pointer or stub file?"
    )


def test_fnd18_appendix_sql_returns_42_high_gap_patrons() -> None:
    """DuckDB SQL in doc/FINDINGS.md [FND-18] must return 42 rows on fixtures."""
    mapping = str(_FND18_DIR / "canonical_player_mapping.parquet")
    profile = str(_FND18_DIR / "canonical_patron_profile.csv")
    bet = str(_FND18_DIR / "cleaned__gmwds_t_bet.parquet")
    con = duckdb.connect()
    try:
        (n,) = con.execute(_HIGH_THEO_GAP_SQL, [mapping, profile, bet]).fetchone()
    finally:
        con.close()
    assert n == 42, f"expected 42 high-theo-gap patrons, got {n}"
