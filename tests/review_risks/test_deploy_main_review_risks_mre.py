"""
STATUS.md — Code Review（追加）：`package/deploy/main.py` flush CLI 與啟動路徑（2026-03-24）

將 reviewer 風險點轉成最小可重現／契約測試；**僅 tests，不修改 production**。
對應 STATUS「Code Review（追加）— package/deploy/main.py」#1–#6。

- #1：原始碼順序契約 +（可選）隔離 sandbox subprocess
- #2：原始碼契約 + subprocess 驗證錯拼旗標仍啟動並出現 Ignoring unrecognized
- #3–#5：以讀取 main.py 為主之契約（未實作保護即視為現況）
- #6：`SELECT *` 仍存在之文件化斷言
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_MAIN = _REPO_ROOT / "package" / "deploy" / "main.py"


def _main_text() -> str:
    return _DEPLOY_MAIN.read_text(encoding="utf-8")


def _has_flask_numpy_pandas() -> bool:
    for mod in ("flask", "numpy", "pandas"):
        if importlib.util.find_spec(mod) is None:
            return False
    return True


def _write_sandbox_deploy(deploy_dir: Path, *, with_env: bool) -> Path:
    deploy_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(_DEPLOY_MAIN, deploy_dir / "main.py")
    (deploy_dir / "models").mkdir(parents=True, exist_ok=True)
    (deploy_dir / "models" / "feature_spec.yaml").write_text("{}\n", encoding="utf-8")
    (deploy_dir / "local_state").mkdir(parents=True, exist_ok=True)
    env_body = (
        "CH_USER=u\n"
        "CH_PASS=p\n"
        "MODEL_DIR=models\n"
        "STATE_DB_PATH=local_state/state.db\n"
        "PREDICTION_LOG_DB_PATH=local_state/prediction_log.db\n"
    )
    if with_env:
        (deploy_dir / ".env").write_text(env_body, encoding="utf-8")
    return deploy_dir / "main.py"


def _run_deploy_main_subprocess(
    main_py: Path,
    deploy_dir: Path,
    argv_tail: list[str],
    *,
    timeout: float | None = 20,
) -> subprocess.CompletedProcess[str]:
    """Run deploy main via run_path; `walkaway_ml` aliased to `trainer` (no wheel required)."""
    argv_list = ["main.py"] + argv_tail
    code = textwrap.dedent(
        f"""
        import sys
        import runpy

        sys.path.insert(0, r"{_REPO_ROOT.as_posix()}")
        import trainer

        sys.modules["walkaway_ml"] = trainer
        sys.argv = {argv_list!r}
        runpy.run_path(r"{main_py.as_posix()}", run_name="__main__")
        """
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(deploy_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# #1 可用性：`ArgumentParser` 晚於 `.env` 強制檢查（help 在缺設時無法取得）
# ---------------------------------------------------------------------------


class TestReview1HelpBlockedUntilEnvChecksSourceOrder(unittest.TestCase):
    """MRE：`--help` 若未先解決 `.env`，理論上無法觸達 argparse（原始碼順序契約）。"""

    def test_argparser_only_after_main_guard_not_module_level(self) -> None:
        text = _main_text()
        idx_main = text.find('if __name__ == "__main__"')
        idx_parser = text.find("ArgumentParser(")
        self.assertNotEqual(idx_main, -1, "expected __main__ block")
        self.assertNotEqual(idx_parser, -1, "expected ArgumentParser")
        self.assertGreater(
            idx_parser,
            idx_main,
            "ArgumentParser should live under __main__, after all import-time env checks",
        )

    def test_env_exit_appears_before_main_block(self) -> None:
        text = _main_text()
        idx_main = text.find('if __name__ == "__main__"')
        idx_env_missing = text.find("not _env_path.exists()")
        self.assertNotEqual(idx_env_missing, -1)
        self.assertLess(idx_env_missing, idx_main, ".env gate should be before __main__")


@unittest.skipUnless(_has_flask_numpy_pandas(), "need flask, numpy, pandas for deploy main subprocess")
class TestReview1SubprocessHelpRequiresSandboxEnv(unittest.TestCase):
    """MRE：隔離目錄無 `.env` 時無法取得 argparse；具備最小 `.env` 時 `--help` 可退出且含 flush 旗標。"""

    def test_without_dotenv_help_cannot_run(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            deploy_dir = Path(td)
            main_copy = _write_sandbox_deploy(deploy_dir, with_env=False)
            r = _run_deploy_main_subprocess(main_copy, deploy_dir, ["--help"], timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(".env", r.stderr + r.stdout)

    def test_with_minimal_env_help_succeeds_and_lists_flush_flags(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            deploy_dir = Path(td)
            main_copy = _write_sandbox_deploy(deploy_dir, with_env=True)
            r = _run_deploy_main_subprocess(main_copy, deploy_dir, ["--help"], timeout=60)
        self.assertEqual(r.returncode, 0, msg=r.stderr + r.stdout)
        out = r.stdout + r.stderr
        self.assertIn("--flush-all", out)
        self.assertIn("--flush-state", out)
        self.assertIn("--flush-prediction", out)


# ---------------------------------------------------------------------------
# #2 `parse_known_args`：錯拼 flush 旗標僅 warning、不視為錯誤
# ---------------------------------------------------------------------------


class TestReview2ParseKnownArgsSilentTypoSource(unittest.TestCase):
    """契約：`parse_known_args` + `_unknown` warning（錯拼可靜默不 flush）。"""

    def test_uses_parse_known_args_and_warns_unknown_argv(self) -> None:
        text = _main_text()
        self.assertIn("parse_known_args()", text)
        self.assertIn("Ignoring unrecognized argv", text)


@unittest.skipUnless(_has_flask_numpy_pandas(), "need flask, numpy, pandas for deploy main subprocess")
class TestReview2SubprocessTypoFlushFlagStillStarts(unittest.TestCase):
    """MRE：`--flush-stake` 不觸發 argparse 錯誤；进程仍會往 Flask 前進（需 timeout 終止）。"""

    def test_typo_flush_flag_logged_as_unrecognized_then_blocks_on_flask(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            deploy_dir = Path(td)
            main_copy = _write_sandbox_deploy(deploy_dir, with_env=True)
            state_db = deploy_dir / "local_state" / "state.db"
            state_db.write_bytes(b"")
            code = textwrap.dedent(
                f"""
                import sys
                import runpy

                sys.path.insert(0, r"{_REPO_ROOT.as_posix()}")
                import trainer

                sys.modules["walkaway_ml"] = trainer
                sys.argv = ["main.py", "--flush-stake"]
                runpy.run_path(r"{main_copy.as_posix()}", run_name="__main__")
                """
            )
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            combined = ""
            try:
                subprocess.run(
                    [sys.executable, "-u", "-c", code],
                    cwd=str(deploy_dir),
                    capture_output=True,
                    text=True,
                    timeout=6,
                    env=env,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                combined = (exc.stdout or "") + (exc.stderr or "")
            else:
                self.fail("deploy main should block on Flask.run until timeout (unexpected early exit)")
            self.assertIn("Ignoring unrecognized argv", combined)
            self.assertTrue(state_db.is_file(), "typo flush must not delete state db")


# ---------------------------------------------------------------------------
# #3 同路徑：`STATE` / `PREDICTION_LOG` 碰撞防護（現況契約：無則不 assert 通過）
# ---------------------------------------------------------------------------


class TestReview3NoSameResolvedPathGuard(unittest.TestCase):
    """契約：目前原始碼未比對兩 DB resolve 後是否相同（STATUS #3 風險仍適用）。"""

    def test_flush_all_has_no_dual_path_resolve_guard(self) -> None:
        func_src = _main_text()
        start = func_src.find("def flush_all_sqlite_bundles")
        self.assertNotEqual(start, -1)
        end = func_src.find("\ndef ", start + 1)
        block = func_src[start:end]
        self.assertNotIn("resolve()", block)


# ---------------------------------------------------------------------------
# #4 嚴格 flush 失敗 exit：現況契約（unlink 僅 warning）
# ---------------------------------------------------------------------------


class TestReview4UnlinkOnlyLogsOSError(unittest.TestCase):
    """契約：`_unlink_sqlite_bundle` 捕獲 OSError 後僅 warning（無 strict exit）。"""

    def test_unlink_oserror_logged_not_re_risen(self) -> None:
        text = _main_text()
        start = text.find("def _unlink_sqlite_bundle")
        self.assertNotEqual(start, -1)
        end = text.find("\ndef ", start + 1)
        block = text[start:end]
        self.assertIn("except OSError", block)
        self.assertIn("log.warning", block)
        self.assertNotIn("sys.exit", block)


# ---------------------------------------------------------------------------
# #5 安全性：刪除路徑未限制在 DEPLOY_ROOT
# ---------------------------------------------------------------------------


class TestReview5NoDeployRootAllowlistOnUnlink(unittest.TestCase):
    """契約：刪除前未要求路徑落在 DEPLOY_ROOT（STATUS #5 風險仍適用）。"""

    def test_unlink_does_not_reference_deploy_root(self) -> None:
        text = _main_text()
        start = text.find("def _unlink_sqlite_bundle")
        end = text.find("\ndef ", start + 1)
        block = text[start:end]
        self.assertNotIn("DEPLOY_ROOT", block)
        self.assertNotIn("DEPLOY_ALLOW", _main_text())


# ---------------------------------------------------------------------------
# #6 效能：Flask 查詢仍 SELECT *
# ---------------------------------------------------------------------------


class TestReview6SelectStarStillPresent(unittest.TestCase):
    """文件化：全表載入風險仍存在（對齊 Task 3 / reviewer #6）。"""

    def test_alerts_and_validation_queries_use_select_star(self) -> None:
        text = _main_text()
        self.assertIn('SELECT * FROM alerts', text)
        self.assertIn("SELECT * FROM validation_results", text)


# ---------------------------------------------------------------------------
# 相容：舊 `--flush` 應已移除
# ---------------------------------------------------------------------------


class TestDeprecatedFlushFlagRemoved(unittest.TestCase):
    """契約：不再註冊單獨 `--flush`（避免與 PATCH 介面漂移）。"""

    def test_no_legacy_flush_add_argument(self) -> None:
        text = _main_text()
        self.assertNotIn('"--flush"', text)
        self.assertNotIn("'--flush'", text)


if __name__ == "__main__":
    unittest.main()
