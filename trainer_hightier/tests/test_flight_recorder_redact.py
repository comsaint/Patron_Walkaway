"""Tests for flight recorder redaction helpers."""

from __future__ import annotations

from trainer_hightier.serving.flight_recorder.redact import (
    contains_forbidden_secret,
    redact_sql,
    redact_value,
)


def test_redact_sql_strips_password_and_uri() -> None:
    """Password and connection URI must not survive redaction."""
    raw = (
        "SELECT 1 FROM t WHERE password='s3cret' "
        "AND clickhouse://user:pass@host:9000/default"
    )
    out = redact_sql(raw)
    assert "s3cret" not in out
    assert "pass@" not in out
    assert not contains_forbidden_secret(out)


def test_redact_value_nested_mapping() -> None:
    """Nested dict values are redacted recursively."""
    payload = {"query": "host=my.internal.example", "nested": ["token=abc123"]}
    cleaned = redact_value(payload, redact_hostnames=True)
    assert "my.internal.example" not in str(cleaned)
    assert "abc123" not in str(cleaned)


def test_contains_forbidden_secret_detects_bearer() -> None:
    """Bearer tokens are flagged as forbidden."""
    assert contains_forbidden_secret("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9")
    safe = redact_sql("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9")
    assert not contains_forbidden_secret(safe)
