"""tests/test_identity.py
=======================
Unit tests for trainer/identity.py — pure-pandas path only.
No ClickHouse connection required.

Coverage
--------
* FND-01 dedup (keep latest lud_dtm per session_id)
* FND-03 casino_player_id cleaning ('null' string, whitespace)
* FND-12 fake-account exclusion (1 session, <=1 game)
* D2 M:N Case 1 (断链重发: multi player_id → same canonical)
* D2 M:N Case 2 (换卡: multi casino_player_id → keep most recent)
* B1 cutoff_dtm leakage prevention
* resolve_canonical_id three-step fallback
* Edge cases: PLACEHOLDER_PLAYER_ID, empty input, all-dummy data
"""

import sys
import pathlib
import unittest
from datetime import datetime

import pandas as pd

# ---------------------------------------------------------------------------
# Import helper — works from project root and from trainer/ directory
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TRAINER_DIR = _REPO_ROOT / "trainer"

def _import_identity():
    """Import identity module via the trainer package (project-root path).

    We must NOT add trainer/ to sys.path because that would make
    trainer/trainer.py importable as "trainer", colliding with the
    trainer/ package itself.  Using the project root and importing
    trainer.identity as a package member is always safe.
    """
    repo_root = str(_REPO_ROOT)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    import importlib
    return importlib.import_module("trainer.identity")


IDENTITY = _import_identity()
build_from_df = IDENTITY.build_canonical_mapping_from_df
resolve = IDENTITY.resolve_canonical_id
_clean = IDENTITY._clean_casino_player_id
_fnd01 = IDENTITY._fnd01_dedup_pandas
_dummy = IDENTITY._identify_dummy_player_ids
_mn = IDENTITY._apply_mn_resolution


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

T0 = datetime(2025, 1, 1)
T1 = datetime(2025, 6, 1)   # cutoff_dtm used in most tests
T2 = datetime(2026, 1, 1)   # after cutoff


_SESSION_DEFAULTS = dict(
    is_manual=0,
    is_deleted=0,
    is_canceled=0,
    num_games_with_wager=5,
    turnover=100.0,  # FND-04: required for ghost-session filter
    lud_dtm=T0,
    __etl_insert_Dtm=T0,
    session_end_dtm=None,
)


def _make_sessions(rows):
    """Build a minimal sessions DataFrame from a list of dicts.

    Returns an empty DataFrame with the correct columns when ``rows`` is empty.
    """
    if not rows:
        cols = list(_SESSION_DEFAULTS.keys()) + ["session_id", "player_id", "casino_player_id"]
        return pd.DataFrame(columns=cols)
    defaults = _SESSION_DEFAULTS
    records = []
    for row in rows:
        r = dict(defaults)
        r.update(row)
        records.append(r)
    df = pd.DataFrame(records)
    for col in ("lud_dtm", "__etl_insert_Dtm", "session_end_dtm"):
        df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# _clean_casino_player_id
# ---------------------------------------------------------------------------

class TestCleanCasinoPlayerId(unittest.TestCase):
    def test_null_string_becomes_nan(self):
        s = pd.Series(["null", "NULL", "Null"])
        result = _clean(s)
        self.assertTrue(result.isna().all())

    def test_whitespace_only_becomes_nan(self):
        s = pd.Series(["  ", "\t", ""])
        result = _clean(s)
        self.assertTrue(result.isna().all())

    def test_valid_id_preserved(self):
        s = pd.Series(["12345678", "  ABC99  "])
        result = _clean(s)
        # The function strips leading/trailing whitespace by detecting stripped
        # empty string; non-empty values pass through unchanged (original value).
        self.assertEqual(result.iloc[0], "12345678")
        self.assertFalse(pd.isna(result.iloc[1]))

    def test_actual_null_stays_null(self):
        s = pd.Series([None, pd.NA])
        result = _clean(s)
        self.assertTrue(result.isna().all())


# ---------------------------------------------------------------------------
# _fnd01_dedup_pandas
# ---------------------------------------------------------------------------

class TestFnd01Dedup(unittest.TestCase):
    def test_keeps_latest_lud_dtm(self):
        older = datetime(2025, 1, 1)
        newer = datetime(2025, 3, 1)
        df = _make_sessions([
            {"session_id": "S1", "player_id": 1, "lud_dtm": older, "casino_player_id": "old"},
            {"session_id": "S1", "player_id": 1, "lud_dtm": newer, "casino_player_id": "new"},
        ])
        result = _fnd01(df)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["casino_player_id"], "new")

    def test_tiebreak_by_etl_insert(self):
        t = datetime(2025, 1, 1)
        earlier_etl = datetime(2025, 1, 1, 0, 0)
        later_etl = datetime(2025, 1, 1, 1, 0)
        df = _make_sessions([
            {"session_id": "S1", "player_id": 1, "lud_dtm": t,
             "__etl_insert_Dtm": earlier_etl, "casino_player_id": "A"},
            {"session_id": "S1", "player_id": 1, "lud_dtm": t,
             "__etl_insert_Dtm": later_etl, "casino_player_id": "B"},
        ])
        result = _fnd01(df)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["casino_player_id"], "B")

    def test_different_sessions_both_kept(self):
        df = _make_sessions([
            {"session_id": "S1", "player_id": 1},
            {"session_id": "S2", "player_id": 2},
        ])
        result = _fnd01(df)
        self.assertEqual(len(result), 2)


# ---------------------------------------------------------------------------
# _identify_dummy_player_ids (FND-12)
# ---------------------------------------------------------------------------

class TestFnd12DummyIds(unittest.TestCase):
    def test_single_session_zero_games_is_dummy(self):
        df = _make_sessions([
            {"session_id": "S1", "player_id": 99, "num_games_with_wager": 0},
        ])
        dummies = _dummy(df)
        self.assertIn(99, dummies)

    def test_single_session_one_game_is_dummy(self):
        df = _make_sessions([
            {"session_id": "S1", "player_id": 99, "num_games_with_wager": 1},
        ])
        dummies = _dummy(df)
        self.assertIn(99, dummies)

    def test_single_session_two_games_not_dummy(self):
        df = _make_sessions([
            {"session_id": "S1", "player_id": 99, "num_games_with_wager": 2},
        ])
        dummies = _dummy(df)
        self.assertNotIn(99, dummies)

    def test_two_sessions_not_dummy(self):
        df = _make_sessions([
            {"session_id": "S1", "player_id": 99, "num_games_with_wager": 0},
            {"session_id": "S2", "player_id": 99, "num_games_with_wager": 0},
        ])
        dummies = _dummy(df)
        self.assertNotIn(99, dummies)

    def test_null_games_coalesced_to_zero(self):
        df = _make_sessions([
            {"session_id": "S1", "player_id": 99, "num_games_with_wager": None},
        ])
        dummies = _dummy(df)
        self.assertIn(99, dummies)

    def test_manual_session_excluded_from_dummy_count(self):
        # is_manual=1 rows don't count toward session_cnt
        df = _make_sessions([
            {"session_id": "S1", "player_id": 99, "is_manual": 1,
             "num_games_with_wager": 5},
        ])
        dummies = _dummy(df)
        # player_id 99 has no valid (non-manual) sessions → not even in agg → not dummy
        self.assertNotIn(99, dummies)


# ---------------------------------------------------------------------------
# _apply_mn_resolution — D2 conflict rules
# ---------------------------------------------------------------------------

class TestApplyMnResolution(unittest.TestCase):
    def _links(self, rows):
        df = pd.DataFrame(rows, columns=["player_id", "casino_player_id", "lud_dtm"])
        df["lud_dtm"] = pd.to_datetime(df["lud_dtm"])
        return df

    def test_simple_1to1_mapping(self):
        links = self._links([
            (1, "CARD_A", T0),
            (2, "CARD_B", T0),
        ])
        result = _mn(links, dummy_player_ids=set())
        self.assertEqual(set(result["player_id"]), {1, 2})
        row1 = result.loc[result["player_id"] == 1, "canonical_id"].iloc[0]
        self.assertEqual(row1, "CARD_A")

    def test_case1_multiple_player_ids_same_card(self):
        # 断链重发: player 1 and 2 both map to same casino_player_id "CARD_A"
        links = self._links([
            (1, "CARD_A", T0),
            (2, "CARD_A", T0),
        ])
        result = _mn(links, dummy_player_ids=set())
        cids = result.set_index("player_id")["canonical_id"]
        # Both should converge on the same canonical_id
        self.assertEqual(cids[1], cids[2])
        self.assertEqual(cids[1], "CARD_A")

    def test_case2_card_swap_keeps_most_recent(self):
        # 换卡: player 1 had CARD_OLD then CARD_NEW
        old = datetime(2024, 1, 1)
        new = datetime(2025, 1, 1)
        links = self._links([
            (1, "CARD_OLD", old),
            (1, "CARD_NEW", new),
        ])
        result = _mn(links, dummy_player_ids=set())
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["canonical_id"], "CARD_NEW")

    def test_dummy_player_ids_excluded(self):
        links = self._links([
            (1, "CARD_A", T0),
            (2, "CARD_B", T0),  # player 2 is a dummy
        ])
        result = _mn(links, dummy_player_ids={2})
        self.assertNotIn(2, result["player_id"].values)
        self.assertIn(1, result["player_id"].values)

    def test_empty_links_returns_empty_df(self):
        links = self._links([])
        result = _mn(links, dummy_player_ids=set())
        self.assertEqual(len(result), 0)
        self.assertIn("player_id", result.columns)
        self.assertIn("canonical_id", result.columns)


# ---------------------------------------------------------------------------
# build_canonical_mapping_from_df — integration
# ---------------------------------------------------------------------------

class TestBuildCanonicalMappingFromDf(unittest.TestCase):
    def _base_session(self, session_id, player_id, casino_player_id, **kw):
        row = dict(
            session_id=session_id,
            player_id=player_id,
            casino_player_id=casino_player_id,
            lud_dtm=T0,
            __etl_insert_Dtm=T0,
            session_end_dtm=datetime(2025, 3, 1),  # before T1 cutoff
            is_manual=0,
            is_deleted=0,
            is_canceled=0,
            num_games_with_wager=10,
        )
        row.update(kw)
        return row

    def test_basic_mapping_returned(self):
        df = _make_sessions([
            self._base_session("S1", 1, "CARD_A"),
            self._base_session("S2", 2, "CARD_B"),
        ])
        result = build_from_df(df, T1)
        self.assertIn("player_id", result.columns)
        self.assertIn("canonical_id", result.columns)
        self.assertEqual(len(result), 2)

    def test_b1_cutoff_excludes_future_sessions(self):
        # Session ends AFTER cutoff_dtm (T1) — must be excluded (B1)
        df = _make_sessions([
            self._base_session("S1", 1, "CARD_A",
                               session_end_dtm=datetime(2025, 3, 1)),
            self._base_session("S2", 2, "CARD_FUTURE",
                               session_end_dtm=datetime(2025, 7, 1)),  # after T1
        ])
        result = build_from_df(df, T1)
        pids = set(result["player_id"])
        self.assertIn(1, pids)
        self.assertNotIn(2, pids)

    def test_placeholder_player_id_excluded(self):
        placeholder = IDENTITY.PLACEHOLDER_PLAYER_ID
        df = _make_sessions([
            self._base_session("S1", placeholder, "CARD_X"),
        ])
        result = build_from_df(df, T1)
        self.assertEqual(len(result), 0)

    def test_manual_sessions_excluded(self):
        df = _make_sessions([
            self._base_session("S1", 1, "CARD_A", is_manual=1),
        ])
        result = build_from_df(df, T1)
        self.assertEqual(len(result), 0)

    def test_deleted_sessions_excluded(self):
        df = _make_sessions([
            self._base_session("S1", 1, "CARD_A", is_deleted=1),
        ])
        result = build_from_df(df, T1)
        self.assertEqual(len(result), 0)

    def test_null_casino_player_id_excluded(self):
        df = _make_sessions([
            self._base_session("S1", 1, None),
        ])
        result = build_from_df(df, T1)
        self.assertEqual(len(result), 0)

    def test_string_null_casino_player_id_excluded(self):
        df = _make_sessions([
            self._base_session("S1", 1, "null"),
        ])
        result = build_from_df(df, T1)
        self.assertEqual(len(result), 0)

    def test_fnd12_dummy_excluded(self):
        df = _make_sessions([
            self._base_session("S1", 99, "CARD_DUMMY",
                               num_games_with_wager=0),
        ])
        result = build_from_df(df, T1)
        self.assertNotIn(99, result["player_id"].values)

    def test_empty_input_returns_empty_df(self):
        df = _make_sessions([])
        result = build_from_df(df, T1)
        self.assertEqual(len(result), 0)

    def test_canonical_id_values_are_python_str(self):
        """Every canonical_id value must be a plain Python str so downstream
        code can do ``cid == "CARD_A"`` comparisons.  We do NOT assert a
        specific column dtype because pandas 3.x infer_string=True stores
        strings as StringDtype, which is equally correct."""
        df = _make_sessions([
            self._base_session("S1", 1, "CARD_A"),
        ])
        result = build_from_df(df, T1)
        # Verify dtype is string-like (accepts both object and StringDtype)
        self.assertTrue(pd.api.types.is_string_dtype(result["canonical_id"]))
        # Verify actual value is a Python str (works for both dtypes)
        self.assertIsInstance(result["canonical_id"].iloc[0], str)


# ---------------------------------------------------------------------------
# resolve_canonical_id — three-step fallback
# ---------------------------------------------------------------------------

class TestResolveCanonicalId(unittest.TestCase):
    def _mapping(self, pairs):
        return pd.DataFrame(pairs, columns=["player_id", "canonical_id"])

    def test_step1_session_lookup_returns_card(self):
        avail = datetime(2025, 1, 1, 0, 0)
        obs = datetime(2025, 1, 1, 1, 0)  # after avail

        def lookup(sid):
            return {"casino_player_id": "CARD_X", "session_avail_dtm": avail}

        mapping = self._mapping([])
        result = resolve(1, "S1", mapping, lookup, obs_time=obs)
        self.assertEqual(result, "CARD_X")

    def test_step1_skipped_when_session_not_yet_available(self):
        avail = datetime(2025, 1, 1, 2, 0)  # future
        obs = datetime(2025, 1, 1, 0, 0)

        def lookup(sid):
            return {"casino_player_id": "CARD_X", "session_avail_dtm": avail}

        mapping = self._mapping([(1, "CACHE_CARD")])
        result = resolve(1, "S1", mapping, lookup, obs_time=obs)
        # Step 1 fails (not available yet) → falls to step 2
        self.assertEqual(result, "CACHE_CARD")

    def test_step2_mapping_cache_used(self):
        mapping = self._mapping([(1, "CACHE_CARD")])
        result = resolve(1, "S1", mapping, session_lookup=None)
        self.assertEqual(result, "CACHE_CARD")

    def test_step3_fallback_to_player_id_string(self):
        mapping = self._mapping([])
        result = resolve(42, "S1", mapping, session_lookup=None)
        self.assertEqual(result, "42")

    def test_placeholder_player_id_returns_empty(self):
        placeholder = IDENTITY.PLACEHOLDER_PLAYER_ID
        mapping = self._mapping([])
        result = resolve(placeholder, "S1", mapping, session_lookup=None)
        self.assertIsNone(result)

    def test_none_player_id_returns_empty(self):
        mapping = self._mapping([])
        result = resolve(None, "S1", mapping, session_lookup=None)
        self.assertIsNone(result)

    def test_step1_skipped_when_no_session_id(self):
        called = []

        def lookup(sid):
            called.append(sid)
            return None

        mapping = self._mapping([(1, "CACHE")])
        result = resolve(1, None, mapping, lookup)
        self.assertFalse(called)
        self.assertEqual(result, "CACHE")


if __name__ == "__main__":
    unittest.main()
