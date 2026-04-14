#!/usr/bin/env python3
"""Build a draft crosscheck report from STATUS.md history."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable

KEYWORDS = (
    "label noise",
    "delayed label",
    "label delay",
    "censored",
    "leakage",
    "lookahead",
    "point-in-time",
    "label contract",
    "contract drift",
    "標註噪音",
    "延遲標註",
    "標註延遲",
    "標籤噪音",
    "標籤延遲",
    "截尾",
    "時點對齊",
    "契約不一致",
)


def _compile_pattern(keywords: Iterable[str]) -> re.Pattern[str]:
    """Compile keyword regex for case-insensitive line matching."""
    escaped = [re.escape(word) for word in keywords]
    return re.compile(rf"({'|'.join(escaped)})", re.IGNORECASE)


def _extract_candidates(status_text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    """Return (section, evidence) pairs containing targeted keywords."""
    section = "Uncategorized"
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in status_text.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            section = line.removeprefix("## ").strip()
            continue
        if not line or len(line) < 12:
            continue
        if pattern.search(line):
            evidence = line[:220]
            key = (section, evidence)
            if key not in seen:
                seen.add(key)
                candidates.append(key)
    return candidates


def _render_markdown(candidates: list[tuple[str, str]]) -> str:
    """Render markdown table for manual phase-1 judgment."""
    lines = [
        "# status_history_crosscheck (draft from STATUS.md)",
        "",
        "此檔為自動擷取草稿；`本輪動作` 與 `暫緩原因/是否解除` 需人工判定。",
        "",
        "| 章節 | 證據片段 | 當時決策 | 暫緩原因 | 現況是否解除 | 本輪動作 | 備註 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for section, evidence in candidates:
        snippet = evidence.replace("|", "\\|")
        lines.append(
            f"| {section} | {snippet} | TBD | TBD | 否/是 | 沿用/重驗/已失效 | - |"
        )
    if not candidates:
        lines.append("| N/A | 無符合關鍵字的候選段落 | - | - | - | - | - |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    """Parse args, extract candidates, and write draft markdown."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=Path, required=True, help="Path to STATUS.md")
    parser.add_argument("--output", type=Path, required=True, help="Path to output markdown")
    args = parser.parse_args()

    status_text = args.status.read_text(encoding="utf-8")
    pattern = _compile_pattern(KEYWORDS)
    draft = _render_markdown(_extract_candidates(status_text, pattern))
    args.output.write_text(draft, encoding="utf-8")
    print(f"Draft written: {args.output}")


if __name__ == "__main__":
    main()
