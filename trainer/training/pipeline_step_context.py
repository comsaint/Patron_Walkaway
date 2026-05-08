"""Pipeline step label for trainer stdout + structured logs (Workstream: operator grep).

Binds a short label such as ``Step 3/11`` in a :class:`contextvars.ContextVar` while the
trainer pipeline runs. :func:`ensure_pipeline_step_log_filter_installed` attaches a root
handler filter so ``trainer`` / ``trainer.*`` log records are prefixed when the label is
set. :func:`pipeline_echo` in ``trainer.training.trainer`` prepends the same label when
the message does not already start with a canonical ``Step N/11`` marker.
"""

from __future__ import annotations

import contextvars
import logging
import re
from typing import Optional

_PIPELINE_STEP_LABEL: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "trainer_pipeline_step_label", default=None
)
_ACTIVE_STEP_TOKEN: Optional[contextvars.Token[Optional[str]]] = None

_CANONICAL_STEP_PREFIX_RE = re.compile(r"^Step\s+\d+[a-z]?/11(\s|—|-|–)", re.UNICODE)

_LOG_FILTER_INSTALLED = False


def get_pipeline_step_label() -> Optional[str]:
    """Return the active ``Step N/11`` label for this context, or ``None``."""
    return _PIPELINE_STEP_LABEL.get()


def pipeline_step_set(label: Optional[str]) -> None:
    """Bind *label* for log/echo prefixing, or clear the label when *label* is ``None``."""
    global _ACTIVE_STEP_TOKEN
    if _ACTIVE_STEP_TOKEN is not None:
        _PIPELINE_STEP_LABEL.reset(_ACTIVE_STEP_TOKEN)
        _ACTIVE_STEP_TOKEN = None
    if label is not None:
        _ACTIVE_STEP_TOKEN = _PIPELINE_STEP_LABEL.set(label)


def message_already_has_pipeline_step_prefix(message: str) -> bool:
    """Return True if *message* already starts with ``Step N/11`` (optional suffix)."""
    return bool(_CANONICAL_STEP_PREFIX_RE.match(message.strip()))


class _PipelineStepPrefixFilter(logging.Filter):
    """Prefix ``trainer`` / ``trainer.*`` log lines with the bound step label."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True and optionally rewrite *record* with a leading step label."""
        label = _PIPELINE_STEP_LABEL.get()
        if not label:
            return True
        if not (record.name == "trainer" or record.name.startswith("trainer.")):
            return True
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        if rendered.startswith(label):
            _tail = rendered[len(label) :]
            if not _tail or _tail[0] in (" ", "—", "-", "–"):
                return True
        if message_already_has_pipeline_step_prefix(rendered):
            return True
        record.msg = f"{label} {rendered}"
        record.args = ()
        return True


def ensure_pipeline_step_log_filter_installed() -> None:
    """Attach :class:`_PipelineStepPrefixFilter` to existing root handlers (idempotent)."""
    global _LOG_FILTER_INSTALLED
    if _LOG_FILTER_INSTALLED:
        return
    filt = _PipelineStepPrefixFilter()
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(filt)
    if root.handlers:
        _LOG_FILTER_INSTALLED = True
