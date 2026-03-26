"""Task 9C reviewer risks -> minimal reproducible tests (tests-only).

Encodes STATUS.md Code Review items for bet_id TBET lookup without changing
production code: failing paths / contracts / config parsing.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

import trainer.serving.validator as validator_mod
from trainer.serving.validator import HK_TZ, fetch_bet_payout_times_by_bet_ids


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = REPO_ROOT / "trainer" / "serving" / "validator.py"


def _validator_text() -> str:
    return VALIDATOR_PATH.read_text(encoding="utf-8")


def _func_block(src: str, func_name: str) -> str:
    pattern = rf"def {re.escape(func_name)}\("
    m = re.search(pattern, src)
    if not m:
        return ""
    start = m.start()
    nxt = re.search(r"\n\ndef [A-Za-z_]\w*\(", src[start + 1 :])
    end = (start + 1 + nxt.start()) if nxt else len(src)
    return src[start:end]


class TestRisk1DuplicateIndexMre(unittest.TestCase):
    """#1: idxmax + loc when group subframe has duplicate index -> exception."""

    @patch("trainer.serving.validator.get_clickhouse_client")
    def test_duplicate_index_raises_value_error_ambiguous_series(self, mock_gc: MagicMock) -> None:
        client = MagicMock()
        mock_gc.return_value = client

        def fake_query_df(query: str, parameters: dict) -> pd.DataFrame:
            return pd.DataFrame(
                {
                    "bet_id": [111, 111],
                    "payout_complete_dtm": [
                        pd.Timestamp("2026-03-25 10:00:00", tz=HK_TZ),
                        pd.Timestamp("2026-03-25 12:00:00", tz=HK_TZ),
                    ],
                    "player_id": [1, 999999],
                },
                index=[0, 0],
            )

        client.query_df.side_effect = fake_query_df

        with self.assertRaises(ValueError) as ctx:
            fetch_bet_payout_times_by_bet_ids([111], chunk_size=50)
        self.assertIn("ambiguous", str(ctx.exception).lower())


class TestRisk3MissingColumnsMre(unittest.TestCase):
    """#3: CH returns DataFrame without expected columns -> KeyError (not in failed_queries)."""

    @patch("trainer.serving.validator.get_clickhouse_client")
    def test_missing_bet_id_column_raises_keyerror(self, mock_gc: MagicMock) -> None:
        client = MagicMock()
        mock_gc.return_value = client
        client.query_df.return_value = pd.DataFrame(
            {"payout_complete_dtm": [pd.Timestamp("2026-01-01 12:00:00", tz=HK_TZ)]}
        )

        with self.assertRaises(KeyError) as ctx:
            fetch_bet_payout_times_by_bet_ids([42], chunk_size=50)
        self.assertEqual(ctx.exception.args[0], "bet_id")


class TestRisk5SqlInjectionContract(unittest.TestCase):
    """#5: bet_id values must stay parameterized (no string concat of ids into SQL)."""

    def test_bet_id_uses_parameterized_in_clause(self) -> None:
        block = _func_block(_validator_text(), "fetch_bet_payout_times_by_bet_ids")
        self.assertIn("bet_id IN %(ids)s", block)
        # Guardrail: do not build IN (1,2,3) via f-string over user-facing ids in this function.
        self.assertNotRegex(block, r"IN\s*\(\s*f[\"']")


class TestRisk7BoolParsingMre(unittest.TestCase):
    """#7: _no_bet_bet_id_lookup_enabled common sentinel values."""

    def test_string_false_disables(self) -> None:
        cfg = validator_mod.config
        old = getattr(cfg, "VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED", True)
        try:
            cfg.VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED = "false"
            self.assertFalse(validator_mod._no_bet_bet_id_lookup_enabled())
        finally:
            cfg.VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED = old

    def test_empty_string_disables(self) -> None:
        cfg = validator_mod.config
        old = getattr(cfg, "VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED", True)
        try:
            cfg.VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED = ""
            self.assertFalse(validator_mod._no_bet_bet_id_lookup_enabled())
        finally:
            cfg.VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED = old

    def test_int_zero_disables(self) -> None:
        cfg = validator_mod.config
        old = getattr(cfg, "VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED", True)
        try:
            cfg.VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED = 0
            self.assertFalse(validator_mod._no_bet_bet_id_lookup_enabled())
        finally:
            cfg.VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED = old

    def test_int_one_enables(self) -> None:
        cfg = validator_mod.config
        old = getattr(cfg, "VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED", True)
        try:
            cfg.VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED = 1
            self.assertTrue(validator_mod._no_bet_bet_id_lookup_enabled())
        finally:
            cfg.VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED = old

    def test_arbitrary_object_is_truthy_via_bool(self) -> None:
        """Reviewer #7: non-bool non-str falls through to bool(raw) — often always on."""
        cfg = validator_mod.config
        old = getattr(cfg, "VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED", True)
        try:
            cfg.VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED = object()
            self.assertTrue(validator_mod._no_bet_bet_id_lookup_enabled())
        finally:
            cfg.VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED = old


class TestRisk2MaxPayoutTieMre(unittest.TestCase):
    """#2: tie on max payout — document idxmax choice (first occurrence in group)."""

    @patch("trainer.serving.validator.get_clickhouse_client")
    def test_equal_payouts_first_row_player_id_wins_per_idxmax(self, mock_gc: MagicMock) -> None:
        client = MagicMock()
        mock_gc.return_value = client
        ts = pd.Timestamp("2026-03-25 12:00:00", tz=HK_TZ)

        def fake_query_df(query: str, parameters: dict) -> pd.DataFrame:
            return pd.DataFrame(
                {
                    "bet_id": [111, 111],
                    "payout_complete_dtm": [ts, ts],
                    "player_id": [111111, 222222],
                },
            )

        client.query_df.side_effect = fake_query_df
        m, _, _, failed = fetch_bet_payout_times_by_bet_ids([111], chunk_size=50)
        self.assertEqual(failed, 0)
        self.assertIn("111", m)
        _payout, ch_pid = m["111"]
        self.assertEqual(ch_pid, 111111)


class TestRisk4ChunkingQueryCountMre(unittest.TestCase):
    """#4: chunk_size splits bet_ids into multiple CH calls (observable QPS)."""

    @patch("trainer.serving.validator.get_clickhouse_client")
    def test_three_chunks_for_six_ids_chunk_size_two(self, mock_gc: MagicMock) -> None:
        client = MagicMock()
        mock_gc.return_value = client
        client.query_df.return_value = pd.DataFrame(
            {
                "bet_id": [1],
                "payout_complete_dtm": [pd.Timestamp("2026-01-01", tz=HK_TZ)],
                "player_id": [1],
            }
        )

        fetch_bet_payout_times_by_bet_ids([10, 20, 30, 40, 50, 60], chunk_size=2)
        self.assertEqual(client.query_df.call_count, 3)


if __name__ == "__main__":
    unittest.main()
