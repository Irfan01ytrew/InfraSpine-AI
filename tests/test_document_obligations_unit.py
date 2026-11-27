from app.rag.parsers.base import Locator, Section
from app.services.document_obligations import normalize_source_text, obligation_status_transition
from app.services.document_versions import (
    compare_extracted_sections,
    source_quote_present_in_sections,
    validate_comparison_format,
)
from app.core.errors import InvalidInputError


def test_text_diff_reports_actual_added_and_removed_lines_with_locations():
    before = [Section("Old payment term\nRenewal every year", Locator("page", 1, "Page 1"))]
    after = [Section("Updated payment term\nRenewal every year\nNotice in writing", Locator("page", 2, "Page 2"))]

    result = compare_extracted_sections(before, after)

    assert result["added_lines"] == 2
    assert result["removed_lines"] == 1
    assert result["truncated"] is False
    assert result["changes"] == [
        {"kind": "removed", "text": "[Page 1] Old payment term"},
        {"kind": "added", "text": "[Page 2] Updated payment term"},
        {"kind": "added", "text": "[Page 2] Notice in writing"},
    ]


def test_diff_is_bounded_and_reports_truncation():
    before = [Section("\n".join(f"line {i}" for i in range(120)), Locator("lines", 1, "Lines 1-120"))]
    after = [Section("\n".join(f"changed {i}" for i in range(120)), Locator("lines", 1, "Lines 1-120"))]

    result = compare_extracted_sections(before, after)

    assert result["added_lines"] == 120
    assert result["removed_lines"] == 120
    assert len(result["changes"]) == 100
    assert result["truncated"] is True


def test_quote_normalization_only_changes_whitespace():
    assert normalize_source_text("  A  stated\n  duty ") == "A stated duty"
    assert normalize_source_text("Clause A") != normalize_source_text("clause a")


def test_version_comparison_checks_obligation_quote_presence_without_semantic_claims():
    quote = "Supplier shall provide a replacement within five business days."
    revised_with_quote = [Section(
        "The supplier shall provide a replacement within five business days. Context may differ.",
        Locator("page", 1, "Page 1"),
    )]
    revised_without_quote = [Section(
        "Supplier shall provide a replacement within ten business days.",
        Locator("page", 1, "Page 1"),
    )]

    assert source_quote_present_in_sections(quote, revised_with_quote) is False
    assert source_quote_present_in_sections(quote, revised_without_quote) is False
    assert source_quote_present_in_sections("Supplier   shall provide\na replacement within ten business days.", revised_without_quote)


def test_obligation_status_transitions_are_explicit():
    assert obligation_status_transition("open", "acknowledged") == "acknowledged"
    assert obligation_status_transition("acknowledged", "completed") == "completed"
    assert obligation_status_transition("completed", "open") == "open"
    try:
        obligation_status_transition("open", "open")
    except InvalidInputError:
        pass
    else:
        raise AssertionError("No-op obligation transition was accepted.")


def test_only_text_parsable_formats_are_comparable():
    validate_comparison_format("pdf")
    try:
        validate_comparison_format("video")
    except InvalidInputError:
        pass
    else:
        raise AssertionError("Video comparison should be reported unsupported.")
