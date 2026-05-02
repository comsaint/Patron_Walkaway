"""Contract test for LDA-E0-01 registry validation."""

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "validate_time_semantics_registry.py"
_REGISTRY = _REPO / "schema" / "time_semantics_registry.yaml"


def _run_validator(*args: str) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(_SCRIPT), *args]
    return subprocess.run(
        cmd,
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        check=False,
    )


def test_validate_time_semantics_registry_exits_zero() -> None:
    proc = _run_validator()
    assert proc.returncode == 0, proc.stderr


def test_validate_time_semantics_registry_rejects_unknown_column(
    tmp_path: Path,
) -> None:
    text = _REGISTRY.read_text(encoding="utf-8")
    injected = text.replace(
        'event_time_col: "payout_complete_dtm"',
        'event_time_col: "not_a_real_column_for_ci"',
        1,
    )
    assert "not_a_real_column_for_ci" in injected
    bad = tmp_path / "bad_registry.yaml"
    bad.write_text(injected, encoding="utf-8")
    proc = _run_validator("--registry", str(bad))
    assert proc.returncode == 1
    assert "not_a_real_column_for_ci" in proc.stderr
