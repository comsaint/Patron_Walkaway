"""Training pipeline orchestration.

``run_pipeline_impl`` is the indirection boundary so ``trainer.training.trainer``
can keep a thin decorated entry while the main body lives as
``trainer.training.pipeline_run_core.run_pipeline_core`` (single globals namespace).
"""
from __future__ import annotations

from typing import Any


def run_pipeline_impl(args: Any) -> None:
    """Invoke the full pipeline implementation (lazy import avoids cycles at trainer import)."""
    from trainer.training.pipeline_run_core import run_pipeline_core

    run_pipeline_core(args)
