"""Step AB slice 1 — tests for app/rag/auto_enrich.py.

Pure stdlib module: no DB, no network, no app imports beyond the module
itself. Run with pytest and log capture on.
"""

from datetime import date

import pytest

from app.rag.auto_enrich import (
    answer_language,
    detect_language,
    expiry_alerts,
    extract_expiry_dates,
    extract_tags,
    summarize_text,
)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

CONTRACT_TEXT = """
This agreement is entered into between Acme Ltd and Beta Corp. The parties
hereby agree to the terms and conditions set out below. The governing law
is the law of Pakistan. Either party may terminate this contract with
thirty days written notice.
"""

INVOICE_TEXT = """
Tax invoice number INV-2026-114. Bill to: Acme Ltd. Amount due: PKR 250,000.
Payment due within 30 days. Bank account number PK12 ABCD 0000 1234 5678.
"""

HR_TEXT = """
Employee leave policy. Every employee is entitled to 14 days of annual
leave. Attendance is recorded daily. Salary is paid on the 1st of each
month. Probation lasts three months from the joining date.
"""

MEETING_TEXT = """
Meeting minutes, 20 November 2026. Attendees: Irfan, Sara, Bilal. Agenda:
quarterly review. Action items: Sara to share the report by Friday.
Decisions: the budget is approved.
"""


class TestExtractTags:
    def test_contract(self):
        tags = extract_tags(CONTRACT_TEXT)
        assert "contract" in tags

    def test_invoice(self):
        tags = extract_tags(INVOICE_TEXT)
        assert "invoice" in tags
        assert "finance" in tags

    def test_hr_policy(self):
        tags = extract_tags(HR_TEXT)
        assert "hr" in tags
        assert "policy" in tags

    def test_meeting(self):
        tags = extract_tags(MEETING_TEXT)
        assert "meeting" in tags

    def test_best_evidenced_first(self):
        # Meeting text mentions "report" once but "meeting" terms many times.
        tags = extract_tags(MEETING_TEXT)
        assert tags.index("meeting") < tags.index("report")

    def test_empty_text_has_no_tags(self):
        assert extract_tags("") == []
        assert extract_tags("   ") == []

    def test_gibberish_has_no_tags(self):
        assert extract_tags("zxqv jklp wxyz 12345") == []

    def test_tag_count_capped(self):
        tags = extract_tags(CONTRACT_TEXT + INVOICE_TEXT + HR_TEXT + MEETING_TEXT, max_tags=3)
        assert len(tags) == 3

    def test_word_boundaries(self):
        # "contractor" alone must not fire the "contract" tag.
        assert extract_tags("The contractor arrived at 9am.") == []


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class TestSummarizeText:
    def test_returns_at_most_three_sentences(self):
        text = (
            "This agreement covers the supply of IT services. "
            "The parties agree to a twelve month term. "
            "Payment is due within thirty days of invoice. "
            "The governing law is the law of Pakistan. "
            "Either party may terminate with notice."
        )
        summary = summarize_text(text)
        assert 1 <= len(summary.split(". ")) <= 3 or summary.count(".") <= 3

    def test_every_sentence_is_verbatim_from_source(self):
        text = (
            "The vendor will deliver the hardware by 1 December. "
            "The buyer will inspect the goods on arrival. "
            "Payment follows acceptance of the delivery."
        )
        summary = summarize_text(text, max_sentences=2)
        for sentence in summary.split(". "):
            sentence = sentence.strip().rstrip(".")
            assert sentence in text

    def test_short_text_returned_as_is(self):
        text = "One sentence only."
        assert summarize_text(text) == text

    def test_empty_text_empty_summary(self):
        assert summarize_text("") == ""
        assert summarize_text("   ") == ""

    def test_leading_sentence_preferred(self):
        # The first sentence states the point; it should survive the cut.
        text = (
            "This contract appoints Beta Corp as the supplier. "
            "The supplier shall keep records. "
            "The supplier shall attend meetings. "
            "The supplier shall file reports. "
            "The supplier shall answer emails."
        )
        summary = summarize_text(text, max_sentences=2)
        assert "This contract appoints Beta Corp as the supplier" in summary


# ---------------------------------------------------------------------------
# Expiry dates
# ---------------------------------------------------------------------------

class TestExtractExpiryDates:
    def test_valid_until_long_form(self):
        found = extract_expiry_dates("This licence is valid until 31 December 2026.")
        assert [d.date for d in found] == [date(2026, 12, 31)]

    def test_expiry_iso(self):
        found = extract_expiry_dates("Contract expiry: 2026-03-15.")
        assert [d.date for d in found] == [date(2026, 3, 15)]

    def test_expires_day_first_slashes(self):
        found = extract_expiry_dates("The agreement expires on 12/03/2026.")
        assert [d.date for d in found] == [date(2026, 3, 12)]

    def test_month_first_long_form(self):
        found = extract_expiry_dates("Expiry date: March 5, 2027.")
        assert [d.date for d in found] == [date(2027, 3, 5)]

    def test_bare_date_without_keyword_is_ignored(self):
        # An invoice date is not an expiry date.
        assert extract_expiry_dates("Invoice date: 12 March 2026. Amount due now.") == []

    def test_no_date_no_expiry(self):
        assert extract_expiry_dates("This contract has no end date written here.") == []

    def test_invalid_date_not_invented(self):
        assert extract_expiry_dates("Expiry: 32 December 2026.") == []

    def test_label_and_context_kept(self):
        found = extract_expiry_dates("The service is valid until 30 June 2026.")
        assert found[0].label == "valid until"
        assert "30 June 2026" in found[0].context

    def test_duplicates_deduped(self):
        found = extract_expiry_dates(
            "Expiry: 2026-03-15. The contract expiry is 15 March 2026."
        )
        assert [d.date for d in found] == [date(2026, 3, 15)]


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

class TestExpiryAlerts:
    def test_expired(self):
        alerts = expiry_alerts([date(2026, 1, 1)], reference_date=date(2026, 11, 28))
        assert alerts[0].urgency == "expired"
        assert alerts[0].days_left < 0

    def test_due_soon(self):
        alerts = expiry_alerts([date(2026, 12, 10)], reference_date=date(2026, 11, 28))
        assert alerts[0].urgency == "due_soon"
        assert alerts[0].days_left == 12

    def test_ok(self):
        alerts = expiry_alerts([date(2027, 6, 1)], reference_date=date(2026, 11, 28))
        assert alerts[0].urgency == "ok"

    def test_warn_window_respected(self):
        alerts = expiry_alerts(
            [date(2026, 12, 20)], reference_date=date(2026, 11, 28), warn_days=7
        )
        assert alerts[0].urgency == "ok"


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------

class TestDetectLanguage:
    @pytest.mark.parametrize(
        "question",
        [
            "pak a ka next match kb hai?",
            "ye contract kb khatam ho raha hai?",
            "mera invoice kitna hai?",
            "is document mein kya likha hai?",
        ],
    )
    def test_roman_urdu(self, question):
        assert detect_language(question) == "roman_urdu"

    def test_urdu_script(self):
        assert detect_language("یہ معاہدہ کب ختم ہو رہا ہے؟") == "urdu"

    def test_english(self):
        assert detect_language("When does this contract expire?") == "english"

    def test_mixed_roman_urdu_and_english(self):
        assert detect_language("contract ki expiry kb hai?") == "roman_urdu"

    def test_empty_is_english(self):
        assert detect_language("") == "english"

    def test_answer_language_mirrors_question(self):
        assert answer_language("mera invoice kitna hai?") == "roman_urdu"
        assert answer_language("What is the total amount?") == "english"
