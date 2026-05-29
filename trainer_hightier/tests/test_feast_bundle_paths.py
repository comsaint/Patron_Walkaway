"""Tests for bundle-local Feast path resolution (no dev-machine absolute paths)."""

from __future__ import annotations

from pathlib import Path

from trainer_hightier.serving.feast_online_adapter import (
    reset_feast_repo_runtime_state,
    resolve_feast_artifacts_dir,
)


def test_resolve_feast_artifacts_dir_is_sibling_of_feast_repo(tmp_path: Path) -> None:
    repo = tmp_path / "deploy_bundle" / "feast_repo"
    repo.mkdir(parents=True)
    art = resolve_feast_artifacts_dir(repo)
    assert art == (tmp_path / "deploy_bundle" / "artifacts" / "feast").resolve()
    assert "site-packages" not in str(art).lower()


def test_reset_feast_repo_runtime_state_removes_stale_dbs(tmp_path: Path) -> None:
    repo = tmp_path / "feast_repo"
    data = repo / "data"
    data.mkdir(parents=True)
    (data / "registry.db").write_bytes(b"stale")
    (data / "online_store.db").write_bytes(b"stale")
    reset_feast_repo_runtime_state(repo)
    assert not (data / "registry.db").is_file()
    assert not (data / "online_store.db").is_file()
