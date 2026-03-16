"""Minimal reproducible guardrail tests for Round 42 review risks (R400-R404).

Tests-only: no production code changes.
"""

from __future__ import annotations

import ast
import inspect
import re
import unittest


class TestR400WindowsCp1252LogSafety(unittest.TestCase):
    """R400: logger/help text should avoid chars that crash cp1252 terminals."""

    _FORBIDDEN = {"\u2192", "\u2190", "\u2264", "\u2265", "\u2208"}  # ->, <-, <=, >=, element

    def _string_literals_in_logger_calls(self, module_src: str) -> list[str]:
        tree = ast.parse(module_src)
        out: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"info", "warning", "error", "exception"}:
                continue
            if not node.args:
                continue
            msg = node.args[0]
            if isinstance(msg, ast.Constant) and isinstance(msg.value, str):
                out.append(msg.value)
        return out

    def test_logger_messages_are_cp1252_safe(self):
        import trainer.backtester as backtester_mod
        import trainer.etl_player_profile as etl_mod
        import trainer.scorer as scorer_mod
        import trainer.trainer as trainer_mod

        modules = [trainer_mod, etl_mod, scorer_mod, backtester_mod]
        bad_messages: list[str] = []

        for mod in modules:
            src = inspect.getsource(mod)
            for msg in self._string_literals_in_logger_calls(src):
                if any(ch in msg for ch in self._FORBIDDEN):
                    bad_messages.append(msg)

        self.assertFalse(
            bad_messages,
            "Logger messages must avoid cp1252-unsafe symbols on Windows terminals. "
            f"Found: {bad_messages[:5]}",
        )


class TestR401IdentityImportFallbackGuardrail(unittest.TestCase):
    """R401: run_pipeline inline identity imports should support trainer.* fallback."""

    def test_run_pipeline_should_not_use_bare_identity_inline_imports(self):
        import trainer.trainer as trainer_mod

        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertNotRegex(
            src,
            r"from\s+identity\s+import\s+get_dummy_player_ids_from_df",
            "Inline import should not use bare 'from identity ...' in -m execution mode.",
        )
        self.assertNotRegex(
            src,
            r"from\s+identity\s+import\s+build_canonical_mapping,\s*get_dummy_player_ids",
            "Inline import should not use bare 'from identity ...' in -m execution mode.",
        )


class TestR402SessionsOnlyDQParityGuardrail(unittest.TestCase):
    """R402: sessions_only column list should include turnover for FND-04 parity."""

    def test_sessions_only_columns_include_turnover(self):
        import trainer.trainer as trainer_mod

        src = inspect.getsource(trainer_mod.load_local_parquet)
        self.assertIn(
            "turnover",
            src,
            "sessions_only column set should include 'turnover' so FND-04 logic "
            "matches full-column path.",
        )


class TestR403SessionsAllReleaseGuardrail(unittest.TestCase):
    """R403: use_local path should release sessions_all after canonical map build."""

    def test_use_local_branch_releases_sessions_all(self):
        import trainer.trainer as trainer_mod

        src = inspect.getsource(trainer_mod.run_pipeline)
        m = re.search(r"if use_local:(?P<body>.*?)\n\s*else:", src, flags=re.S)
        self.assertIsNotNone(m, "Could not find use_local branch in run_pipeline source.")
        local_branch = m.group("body") if m else ""
        self.assertIn(
            "sessions_all = None",
            local_branch,
            "use_local branch should clear sessions_all to reduce peak memory.",
        )


class TestR404CanonicalColsContractGuardrail(unittest.TestCase):
    """R404: canonical-map session columns should be module-level and cover identity contract."""

    def test_module_level_canonical_cols_exist_and_cover_identity_required(self):
        import trainer.identity as identity_mod
        import trainer.trainer as trainer_mod

        self.assertTrue(
            hasattr(trainer_mod, "_CANONICAL_MAP_SESSION_COLS"),
            "Expected module-level _CANONICAL_MAP_SESSION_COLS for cross-module contract checks.",
        )
        cols = set(getattr(trainer_mod, "_CANONICAL_MAP_SESSION_COLS", []))
        self.assertTrue(
            set(identity_mod._REQUIRED_SESSION_COLS).issubset(cols),
            "_CANONICAL_MAP_SESSION_COLS should cover identity._REQUIRED_SESSION_COLS.",
        )


if __name__ == "__main__":
    unittest.main()
