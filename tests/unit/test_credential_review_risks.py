"""
Code Review: Credential folder 整合 — 最小可重現測試（STATUS § Code Review）。

對應 STATUS.md「Code Review：Credential folder 整合變更 — 高可靠性標準」各風險點：
§1 config 載入無 try/except
§3 GOOGLE_APPLICATION_CREDENTIALS 相對路徑語義（文件契約）
§5 .gitignore credential 規則契約

僅新增 tests，不修改 production code。
"""

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


# --- §1: config 載入失敗時 process 不 crash（期望行為；目前 config 未包 try/except 會 fail）---


def test_credential_review_config_import_succeeds_when_load_dotenv_raises():
    """Code Review §1: When load_dotenv raises on first call, import trainer.core.config should still succeed (resilient)."""
    code = r"""
import os
import sys
repo_root = sys.argv[1]
os.chdir(repo_root)
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
import dotenv
_orig = dotenv.load_dotenv
_call_count = [0]
def _raising(*a, **k):
    _call_count[0] += 1
    if _call_count[0] == 1:
        raise PermissionError("access denied")
    return _orig(*a, **k)
dotenv.load_dotenv = _raising
from trainer.core import config
sys.exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(_REPO_ROOT)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"config import failed when load_dotenv raises: {result.stderr!r}"
    )


# --- §3: GOOGLE_APPLICATION_CREDENTIALS 路徑語義 — 文件契約 ---


def test_credential_review_mlflow_env_example_mentions_absolute_or_cwd():
    """Code Review §3: credential/mlflow.env.example must recommend absolute path or clarify cwd for GOOGLE_APPLICATION_CREDENTIALS."""
    example_path = _REPO_ROOT / "credential" / "mlflow.env.example"
    if not example_path.exists():
        pytest.skip("credential/mlflow.env.example not in repo (e.g. deploy-only)")
    content = example_path.read_text(encoding="utf-8")
    assert "GOOGLE_APPLICATION_CREDENTIALS" in content
    assert "absolute" in content or "cwd" in content or "working directory" in content.lower(), (
        "Code Review §3: mlflow.env.example should recommend absolute path or state that relative path is relative to cwd."
    )


# --- §5: .gitignore credential 規則契約 ---


def test_credential_review_gitignore_ignores_secrets_keeps_examples():
    """Code Review §5: .gitignore must ignore credential secrets and keep .env.example / mlflow.env.example."""
    gitignore_path = _REPO_ROOT / ".gitignore"
    content = gitignore_path.read_text(encoding="utf-8")
    # Must ignore sensitive files
    assert "credential/.env" in content or "credential" in content, (
        ".gitignore should ignore credential/.env or credential/"
    )
    assert "credential/mlflow.env" in content or "credential" in content
    assert ".env.example" in content or "!credential/.env.example" in content, (
        ".gitignore should either allow .env.example or explicitly !credential/.env.example"
    )
    assert "!credential/.env.example" in content, (
        ".gitignore must have !credential/.env.example to track example"
    )
    assert "!credential/mlflow.env.example" in content, (
        ".gitignore must have !credential/mlflow.env.example to track example"
    )
