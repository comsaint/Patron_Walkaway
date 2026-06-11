"""Audit-only PIT as-of join helpers for cleaned ``t_casino_txn`` sources."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import duckdb
import pandas as pd

DEFAULT_WINDOWS_MIN: Final[tuple[int, ...]] = (15, 60, 240)


@dataclass(frozen=True)
class TxnAsofSchema:
    """Column contract for cleaned casino transaction source-grain parquet."""

    player_id_col: str = "player_id"
    txn_id_col: str = "casino_txn_id"
    event_ts_col: str = "txn_event_ts"
    available_ts_col: str = "txn_available_ts"
    type_col: str = "type"
    sub_type_col: str = "sub_type"
    status_col: str = "status"
    action_col: str = "action"
    value_col: str = "txn_value"
    extra_required_cols: tuple[str, ...] = field(default_factory=tuple)

    @property
    def required_columns(self) -> tuple[str, ...]:
        """Return required columns for PIT-safe as-of aggregation."""

        return (
            self.player_id_col,
            self.txn_id_col,
            self.event_ts_col,
            self.available_ts_col,
            self.type_col,
            self.status_col,
            self.action_col,
            self.value_col,
            *self.extra_required_cols,
        )


def validate_txn_schema(txn_path: Path, schema: TxnAsofSchema) -> dict[str, object]:
    """Validate cleaned txn parquet columns and return a small schema report."""

    con = duckdb.connect()
    cols = con.execute(
        "DESCRIBE SELECT * FROM read_parquet(?)",
        [str(txn_path)],
    ).fetchdf()
    available = set(cols["column_name"].astype(str))
    required = set(schema.required_columns)
    missing = sorted(required.difference(available))
    return {
        "txn_path": str(txn_path),
        "required_columns": sorted(required),
        "missing_columns": missing,
        "is_valid": not missing,
    }


def _window_selects(windows_min: tuple[int, ...]) -> list[str]:
    """Build SQL aggregate expressions for configured windows."""

    selects: list[str] = []
    for minutes in windows_min:
        suffix = f"w{minutes}m"
        selects.extend(
            [
                (
                    f"COUNT(*) FILTER (WHERE t.txn_event_ts > b.prediction_ts - "
                    f"INTERVAL {int(minutes)} MINUTE) AS txn_audit__txn_cnt__{suffix}"
                ),
                (
                    f"SUM(t.txn_value) FILTER (WHERE t.txn_event_ts > b.prediction_ts - "
                    f"INTERVAL {int(minutes)} MINUTE) AS txn_audit__txn_value_sum__{suffix}"
                ),
                (
                    f"COUNT(*) FILTER (WHERE t.txn_type = 'BUYIN' AND "
                    f"t.txn_event_ts > b.prediction_ts - INTERVAL {int(minutes)} MINUTE) "
                    f"AS txn_audit__buyin_cnt__{suffix}"
                ),
                (
                    f"COUNT(*) FILTER (WHERE t.txn_type = 'CASHOUT' AND "
                    f"t.txn_event_ts > b.prediction_ts - INTERVAL {int(minutes)} MINUTE) "
                    f"AS txn_audit__cashout_cnt__{suffix}"
                ),
            ],
        )
    return selects


def build_txn_asof_query(
    *,
    bets_path: Path,
    txn_path: Path,
    schema: TxnAsofSchema,
    windows_min: tuple[int, ...] = DEFAULT_WINDOWS_MIN,
    bet_id_col: str = "bet_id",
    bet_player_id_col: str = "player_id",
    prediction_ts_col: str = "payout_complete_dtm",
) -> str:
    """Return a DuckDB query for PIT-safe txn audit aggregates at bet grain."""

    selects = ",\n      ".join(_window_selects(windows_min))
    return f"""
WITH bets AS (
  SELECT
    {bet_id_col} AS bet_id,
    {bet_player_id_col} AS player_id,
    TRY_CAST({prediction_ts_col} AS TIMESTAMP) AS prediction_ts
  FROM read_parquet('{bets_path.as_posix()}')
  WHERE {bet_id_col} IS NOT NULL
    AND {bet_player_id_col} IS NOT NULL
    AND TRY_CAST({prediction_ts_col} AS TIMESTAMP) IS NOT NULL
),
txns AS (
  SELECT
    {schema.player_id_col} AS player_id,
    {schema.txn_id_col} AS txn_id,
    TRY_CAST({schema.event_ts_col} AS TIMESTAMP) AS txn_event_ts,
    TRY_CAST({schema.available_ts_col} AS TIMESTAMP) AS txn_available_ts,
    {schema.type_col} AS txn_type,
    TRY_CAST({schema.value_col} AS DOUBLE) AS txn_value
  FROM read_parquet('{txn_path.as_posix()}')
  WHERE {schema.player_id_col} IS NOT NULL
    AND {schema.txn_id_col} IS NOT NULL
    AND TRY_CAST({schema.event_ts_col} AS TIMESTAMP) IS NOT NULL
    AND TRY_CAST({schema.available_ts_col} AS TIMESTAMP) IS NOT NULL
)
SELECT
  b.bet_id,
  {selects}
FROM bets b
LEFT JOIN txns t
  ON t.player_id = b.player_id
 AND t.txn_available_ts <= b.prediction_ts
 AND t.txn_event_ts <= b.prediction_ts
 AND t.txn_event_ts > b.prediction_ts - INTERVAL {int(max(windows_min))} MINUTE
GROUP BY b.bet_id
"""


def materialize_txn_asof_preview(
    *,
    bets_path: Path,
    txn_path: Path,
    output_path: Path,
    schema: TxnAsofSchema = TxnAsofSchema(),
    windows_min: tuple[int, ...] = DEFAULT_WINDOWS_MIN,
) -> dict[str, object]:
    """Materialize audit-only as-of aggregates and return a sidecar summary."""

    schema_report = validate_txn_schema(txn_path, schema)
    if not schema_report["is_valid"]:
        raise ValueError(f"txn source schema invalid: {schema_report}")
    con = duckdb.connect()
    query = build_txn_asof_query(
        bets_path=bets_path,
        txn_path=txn_path,
        schema=schema,
        windows_min=windows_min,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({query}) TO ? (FORMAT PARQUET)", [str(output_path)])
    row_count = con.execute("SELECT COUNT(*) FROM read_parquet(?)", [str(output_path)]).fetchone()[0]
    return {
        "bets_path": str(bets_path),
        "txn_path": str(txn_path),
        "output_path": str(output_path),
        "row_count": int(row_count),
        "windows_min": list(windows_min),
        "schema": schema_report,
        "not_model_eligible": True,
    }


def main() -> None:
    """CLI entrypoint for audit-only txn as-of preview materialization."""

    parser = argparse.ArgumentParser(description="Audit-only casino_txn PIT as-of preview")
    parser.add_argument("--bets-path", type=Path, required=True)
    parser.add_argument("--txn-path", type=Path, required=True)
    parser.add_argument("--output-path", type=Path, required=True)
    parser.add_argument("--windows-min", type=int, nargs="+", default=list(DEFAULT_WINDOWS_MIN))
    args = parser.parse_args()
    summary = materialize_txn_asof_preview(
        bets_path=args.bets_path,
        txn_path=args.txn_path,
        output_path=args.output_path,
        windows_min=tuple(args.windows_min),
    )
    sidecar = args.output_path.with_suffix(".summary.json")
    sidecar.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
