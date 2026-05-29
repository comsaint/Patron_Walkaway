"""Guard: ``trainer_hightier`` must not import legacy ``trainer.*`` or ``pipelines.*``."""

from __future__ import annotations

import ast
from pathlib import Path


def _forbidden_import_hits(path: Path) -> list[str]:
    """Return human-readable forbidden import messages for ``path`` (single file)."""

    hits: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "trainer" or mod.startswith("trainer."):
                if not mod.startswith("trainer_hightier"):
                    hits.append(f"from {mod} import ...")
            elif mod == "pipelines" or mod.startswith("pipelines."):
                hits.append(f"from {mod} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                base = (alias.name or "").split(".", maxsplit=1)[0]
                if base == "trainer":
                    hits.append(f"import {alias.name}")
                elif base == "pipelines":
                    hits.append(f"import {alias.name}")
    return hits


def _collect_forbidden_modules(pkg_root: Path) -> list[tuple[Path, str]]:
    """Scan ``trainer_hightier/**/*.py`` for forbidden cross-package imports."""

    bad: list[tuple[Path, str]] = []
    for path in sorted(pkg_root.rglob("*.py")):
        rel = path.relative_to(pkg_root)
        if "__pycache__" in rel.parts:
            continue
        for msg in _forbidden_import_hits(path):
            bad.append((path, msg))
    return bad


def test_trainer_hightier_tree_has_no_legacy_project_imports() -> None:
    """Fail when ``trainer.*`` (excluding ``trainer_hightier``) or ``pipelines.*`` appears."""

    root = Path(__file__).resolve().parents[1]
    bad = _collect_forbidden_modules(root)
    if bad:
        lines = "\n".join(f"{p}: {msg}" for p, msg in bad)
        raise AssertionError("Forbidden legacy project imports detected:\n" + lines)
