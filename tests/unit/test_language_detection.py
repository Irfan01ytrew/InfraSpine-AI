"""Tests for app.language.detect — language detection of short user text."""

from app.language import contains_urdu_script, detect_language, is_roman_urdu


def test_empty_and_blank_are_unknown():
    assert detect_language("") == "unknown"
    assert detect_language("   ") == "unknown"
    assert detect_language("...") == "unknown"


def test_plain_english():
    assert detect_language("Hello, show me last month's invoices") == "en"
    assert detect_language("What is the status of invoice 42?") == "en"


def test_digits_only_are_unknown():
    assert detect_language("12345") == "unknown"
    assert detect_language("32/50") == "unknown"


def test_roman_urdu_user_question():
    # The owner's exact question from the Knowledge screenshot.
    assert detect_language("pak a ka next match kb hai?") == "roman_urdu"


def test_roman_urdu_variants():
    assert detect_language("aaj ka match kya hai") == "roman_urdu"
    assert detect_language("pichle mahine ke invoices dikhao") == "roman_urdu"
    assert detect_language("mujhe kal ki meeting batao") == "roman_urdu"
    assert detect_language("kitne invoices unpaid hain") == "roman_urdu"


def test_urdu_script():
    assert detect_language("پاک کا اگلا میچ کب ہے؟") == "urdu"
    assert detect_language("پچھلے مہینے کے انوائس دکھاؤ") == "urdu"


def test_mixed_script_is_mixed():
    assert detect_language("pak ka agla match کب ہے؟") == "mixed"
    assert detect_language("invoice 42 کب pay hua?") == "mixed"


def test_urdu_digit_alone_does_not_make_text_urdu():
    # Urdu-script digits are neutral: an English sentence stays English.
    assert detect_language("Invoice ۵ is ready") == "en"
    assert detect_language("۳۲ invoices verified") == "en"


def test_is_roman_urdu():
    assert is_roman_urdu("pak a ka next match kb hai?")
    assert not is_roman_urdu("پاک کا اگلا میچ کب ہے؟")
    assert not is_roman_urdu("Show me the invoices")
    assert not is_roman_urdu("")


def test_contains_urdu_script():
    assert contains_urdu_script("میچ")
    assert contains_urdu_script("Invoice ۵")
    assert not contains_urdu_script("pak a ka match kb hai?")
    assert not contains_urdu_script("")


def test_english_words_that_look_roman_urdu_are_english():
    # "main" and "do" are common English words and must not trigger detection.
    assert detect_language("Show the main dashboard") == "en"
    assert detect_language("Do you have the report?") == "en"
