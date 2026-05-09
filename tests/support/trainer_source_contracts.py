"""Shared helpers for trainer source-contract tests (multi-module refactor).

After the thin ``run_pipeline`` wrapper split, contracts that need the *full*
pipeline body must read ``_run_pipeline_core`` (or later ``pipeline_run_core``),
not ``inspect.getsource(run_pipeline)``.
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TRAINER_TRAINING_PY = _REPO_ROOT / "trainer" / "training" / "trainer.py"
_ARTIFACT_BUNDLE_PY = _REPO_ROOT / "trainer" / "core" / "training_artifact_bundle.py"
_PIPELINE_RUN_CORE_PY = _REPO_ROOT / "trainer" / "training" / "pipeline_run_core.py"
_STEP7_SPLIT_RUNTIME_PY = _REPO_ROOT / "trainer" / "training" / "step7_split_runtime.py"


def _trainer_mod():
    from trainer.training import trainer as trainer_mod

    return trainer_mod


def pipeline_implementation_source() -> str:
    """Return source of the full pipeline implementation (not the thin ``run_pipeline`` shell)."""
    if _PIPELINE_RUN_CORE_PY.exists():
        try:
            from trainer.training import pipeline_run_core as prc

            core = getattr(prc, "run_pipeline_core", None)
            if core is not None:
                return inspect.getsource(core)
        except Exception:
            pass
    m = _trainer_mod()
    core = getattr(m, "_run_pipeline_core", None)
    if core is not None:
        return inspect.getsource(core)
    return inspect.getsource(m.run_pipeline)


def pipeline_implementation_ast_module() -> ast.Module:
    """Parse the pipeline implementation as a standalone module (single top-level def)."""
    return ast.parse(textwrap.dedent(pipeline_implementation_source()))


def first_pipeline_function_def() -> ast.FunctionDef:
    """Return the implementation ``FunctionDef`` (``_run_pipeline_core`` or legacy ``run_pipeline``)."""
    mod = pipeline_implementation_ast_module()
    for node in mod.body:
        if isinstance(node, ast.FunctionDef) and node.name in (
            "run_pipeline_core",
            "_run_pipeline_core",
            "run_pipeline",
        ):
            return node
    raise AssertionError(
        "expected run_pipeline_core, _run_pipeline_core, or run_pipeline in pipeline implementation source"
    )


def count_name_calls_in_pipeline_impl(*, callee_id: str) -> int:
    """Count ``callee_id(...)`` calls in the pipeline implementation AST."""

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.n = 0

        def visit_Call(self, node: ast.Call) -> None:
            f = node.func
            if isinstance(f, ast.Name) and f.id == callee_id:
                self.n += 1
            self.generic_visit(node)

    v = _Visitor()
    v.visit(first_pipeline_function_def())
    return v.n


def step7_split_runtime_source() -> str:
    """Return ``trainer.training.step7_split_runtime`` module source (Step 7 helpers)."""
    return _STEP7_SPLIT_RUNTIME_PY.read_text(encoding="utf-8")


def module_level_def_body(source: str, func_name: str) -> str | None:
    """Return source slice ``def func_name(...`` through the next top-level ``def`` or EOF."""
    needle = f"def {func_name}("
    start = source.find(needle)
    if start == -1:
        return None
    rest = source[start:]
    m = re.search(r"\n^def ", rest[1:], re.MULTILINE)
    end = 1 + m.start() if m else len(rest)
    return rest[:end]


def combined_contract_text(*, include_artifact_bundle: bool = True) -> str:
    """Concatenate on-disk sources for substring contracts (B/D moves across files)."""
    parts: list[str] = []
    if _PIPELINE_RUN_CORE_PY.exists():
        parts.append(_PIPELINE_RUN_CORE_PY.read_text(encoding="utf-8"))
    if _STEP7_SPLIT_RUNTIME_PY.exists():
        parts.append(_STEP7_SPLIT_RUNTIME_PY.read_text(encoding="utf-8"))
    parts.append(_TRAINER_TRAINING_PY.read_text(encoding="utf-8"))
    if include_artifact_bundle and _ARTIFACT_BUNDLE_PY.exists():
        parts.append(_ARTIFACT_BUNDLE_PY.read_text(encoding="utf-8"))
    return "\n".join(parts)


def find_def_source(func_name: str, *, prefer_paths: Optional[list[Path]] = None) -> str:
    """Return source segment of ``def func_name`` from the first file that defines it."""
    paths = prefer_paths or [
        _ARTIFACT_BUNDLE_PY,
        _PIPELINE_RUN_CORE_PY,
        _STEP7_SPLIT_RUNTIME_PY,
        _TRAINER_TRAINING_PY,
    ]
    for p in paths:
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                return ast.get_source_segment(src, node) or ""
    return ""
