"""Redact secrets and connection details from recorder artifacts."""

from __future__ import annotations

import re
from typing import Any

_PASSWORD_KV = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api_key|access_key)\s*=\s*['\"]?[^'\"\s;]+",
)
_CONNECTION_URI = re.compile(
    r"(?i)\b(?:clickhouse|mysql|postgresql|redis)://[^\s'\"]+",
)
_HOST_KV = re.compile(
    r"(?i)(host|hostname|server)\s*=\s*['\"]?[^'\"\s;,]+",
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_REDACTED_MARKER = re.compile(r"(?i)REDACTED")


def redact_sql(text: str, *, redact_hostnames: bool = True) -> str:
    """Redact sensitive fragments from SQL or query text."""
    out = _PASSWORD_KV.sub(r"\1=REDACTED", text)
    out = _CONNECTION_URI.sub("REDACTED_CONNECTION_URI", out)
    out = _BEARER.sub("Bearer REDACTED", out)
    if redact_hostnames:
        out = _HOST_KV.sub(r"\1=REDACTED_HOST", out)
    return out


def redact_value(value: Any, *, redact_hostnames: bool = True) -> Any:
    """Recursively redact strings inside mappings and sequences."""
    if isinstance(value, str):
        return redact_sql(value, redact_hostnames=redact_hostnames)
    if isinstance(value, dict):
        return {
            str(k): redact_value(v, redact_hostnames=redact_hostnames)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_value(v, redact_hostnames=redact_hostnames) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v, redact_hostnames=redact_hostnames) for v in value)
    return value


def contains_forbidden_secret(text: str) -> bool:
    """Return True if *text* still looks like it contains a credential."""
    if _REDACTED_MARKER.search(text):
        return False
    if _PASSWORD_KV.search(text):
        return True
    if _CONNECTION_URI.search(text):
        return True
    if _BEARER.search(text):
        return True
    return False
