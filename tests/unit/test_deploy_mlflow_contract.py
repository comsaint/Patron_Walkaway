"""
Phase 2 P0–P1: Contract tests for deploy mlflow dependency (Code Review §10).

Assert that both package/deploy/requirements.txt and build_deploy_package.REQUIREMENTS_DEPS
contain mlflow, so deploy_dist and deploy install paths stay in sync.
Tests only; no production code changes.
"""

from pathlib import Path

# Repo root: from tests/unit/ go up two levels.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_REQUIREMENTS = REPO_ROOT / "package" / "deploy" / "requirements.txt"


def test_deploy_requirements_txt_contains_mlflow():
    """Code Review §10: package/deploy/requirements.txt must list mlflow."""
    assert DEPLOY_REQUIREMENTS.exists(), f"Missing {DEPLOY_REQUIREMENTS}"
    text = DEPLOY_REQUIREMENTS.read_text(encoding="utf-8")
    lines = [line.strip().lower() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert any("mlflow" in line for line in lines), "package/deploy/requirements.txt should contain mlflow"


def test_build_deploy_package_requirements_deps_contains_mlflow():
    """Code Review §10: REQUIREMENTS_DEPS in build_deploy_package must contain mlflow."""
    from package.build_deploy_package import REQUIREMENTS_DEPS
    deps_lower = [d.split("==")[0].split(">=")[0].split("[")[0].strip().lower() for d in REQUIREMENTS_DEPS]
    assert "mlflow" in deps_lower, "REQUIREMENTS_DEPS should contain mlflow"
