from __future__ import annotations

import sqlite3

import trainer.serving.validator as validator_mod


def _mk_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE validation_results (
            bet_id TEXT PRIMARY KEY,
            alert_ts TEXT,
            validated_at TEXT,
            player_id TEXT,
            reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE validator_runtime_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    return conn


def test_incident_bootstrap_full_when_cache_empty_even_with_watermark() -> None:
    conn = _mk_conn()
    conn.execute(
        """
        INSERT INTO validation_results(bet_id, alert_ts, validated_at, player_id, reason)
        VALUES ('101', '2026-03-25T10:00:00+08:00', '2026-03-25T10:20:00+08:00', '1', 'MATCH')
        """
    )
    conn.execute(
        """
        INSERT INTO validator_runtime_meta(key, value)
        VALUES (?, ?)
        """,
        (validator_mod._VALIDATOR_META_KEY_LAST_ROWID, "999999"),
    )
    conn.commit()

    out = validator_mod.load_existing_results_incremental(conn, {})
    assert "101" in out


def test_incident_incremental_load_after_bootstrap() -> None:
    conn = _mk_conn()
    conn.execute(
        """
        INSERT INTO validation_results(bet_id, alert_ts, validated_at, player_id, reason)
        VALUES ('201', '2026-03-25T10:00:00+08:00', '2026-03-25T10:20:00+08:00', '1', 'MATCH')
        """
    )
    conn.commit()

    cache: dict[str, dict] = {}
    out1 = validator_mod.load_existing_results_incremental(conn, cache)
    assert "201" in out1

    conn.execute(
        """
        INSERT INTO validation_results(bet_id, alert_ts, validated_at, player_id, reason)
        VALUES ('202', '2026-03-25T10:02:00+08:00', '2026-03-25T10:22:00+08:00', '1', 'MISS')
        """
    )
    conn.commit()

    out2 = validator_mod.load_existing_results_incremental(conn, cache)
    assert "201" in out2
    assert "202" in out2
