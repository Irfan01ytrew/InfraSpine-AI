"""Bounded, deterministic text comparisons for retained document versions."""

from __future__ import annotations

from difflib import SequenceMatcher

from app.core.errors import InvalidInputError
from app.rag.parsers.base import Section

MAX_COMPARE_LINES = 4_000
MAX_REPORTED_CHANGES = 100
MAX_CHANGE_CHARS = 1_200


def _comparison_lines(sections: list[Section]) -> tuple[list[tuple[str, str]], bool]:
    lines: list[tuple[str, str]] = []
    truncated = False
    for section in sections:
        label = section.locator.label[:120]
        for line in section.text.splitlines():
            normalized = " ".join(line.split())
            if not normalized:
                continue
            if len(lines) >= MAX_COMPARE_LINES:
                return lines, True
            if len(normalized) > MAX_CHANGE_CHARS:
                truncated = True
            lines.append((label, normalized))
    return lines, truncated


def compare_extracted_sections(
    before: list[Section], after: list[Section]
) -> dict[str, int | bool | list[dict[str, str]]]:
    """Compare actual parser output. This is a textual diff, not a legal/semantic opinion."""
    old_lines, old_truncated = _comparison_lines(before)
    new_lines, new_truncated = _comparison_lines(after)
    matcher = SequenceMatcher(
        a=[text for _, text in old_lines], b=[text for _, text in new_lines], autojunk=True
    )
    changes: list[dict[str, str]] = []
    additions = removals = 0
    for opcode, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if opcode in ("delete", "replace"):
            removed = old_lines[old_start:old_end]
            removals += len(removed)
            changes.extend(
                {"kind": "removed", "text": f"[{label}] {line}"[:MAX_CHANGE_CHARS]}
                for label, line in removed
            )
        if opcode in ("insert", "replace"):
            added = new_lines[new_start:new_end]
            additions += len(added)
            changes.extend(
                {"kind": "added", "text": f"[{label}] {line}"[:MAX_CHANGE_CHARS]}
                for label, line in added
            )
    was_truncated = old_truncated or new_truncated or len(changes) > MAX_REPORTED_CHANGES
    return {
        "added_lines": additions,
        "removed_lines": removals,
        "changes": changes[:MAX_REPORTED_CHANGES],
        "truncated": was_truncated,
    }


def source_quote_present_in_sections(quote: str, sections: list[Section]) -> bool:
    """Check exact whitespace-normalized quote continuity; this says nothing about legal meaning."""
    normalized_quote = " ".join(quote.split())
    if not normalized_quote:
        return False
    return any(normalized_quote in " ".join(section.text.split()) for section in sections)


def validate_comparison_format(file_format: str) -> None:
    if file_format not in {"pdf", "docx", "xlsx", "pptx", "txt", "csv"}:
        raise InvalidInputError(
            "Text comparison is available for PDF, Office, text, and CSV files; image and media comparisons are not supported."
        )
