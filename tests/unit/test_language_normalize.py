"""Tests for app.language.normalize — canonical forms for matching."""

from app.language import (
    normalize_command_words,
    normalize_date_words,
    normalize_digits,
    normalize_for_matching,
    normalize_number_words,
    roman_urdu_to_urdu_script,
    urdu_script_to_roman,
)


def test_normalize_digits_urdu_and_arabic():
    assert normalize_digits("۵") == "5"
    assert normalize_digits("۳۲/۵۰ invoices verified") == "32/50 invoices verified"
    assert normalize_digits("١٢٣") == "123"
    assert normalize_digits("no digits here") == "no digits here"
    assert normalize_digits("") == ""


def test_number_words_single():
    assert normalize_number_words("das invoices") == "10 invoices"
    assert normalize_number_words("teen") == "3"
    assert normalize_number_words("تیس invoices") == "30 invoices"
    assert normalize_number_words("نو") == "9"


def test_number_words_multipliers():
    assert normalize_number_words("do sau") == "200"
    assert normalize_number_words("paanch hazaar") == "5000"
    assert normalize_number_words("ek lakh") == "100000"
    assert normalize_number_words("do crore") == "20000000"
    assert normalize_number_words("دو سو") == "200"
    assert normalize_number_words("پانچ ہزار") == "5000"
    assert normalize_number_words("sau") == "100"


def test_number_words_keep_punctuation_and_spacing():
    assert normalize_number_words("do sau, paanch hazaar.") == "200, 5000."
    assert normalize_number_words("total: do sau invoices") == "total: 200 invoices"


def test_number_words_leave_plain_words_alone():
    assert normalize_number_words("show me the report") == "show me the report"
    # "saath" means "with" far more often than 60 — never mapped.
    assert normalize_number_words("mere saath") == "mere saath"


def test_date_phrases():
    assert normalize_date_words("pichle mahine ke invoices") == "last month ke invoices"
    assert normalize_date_words("پچھلے مہینے کے انوائس") == "last month کے انوائس"
    assert normalize_date_words("is hafte ki meetings") == "this week ki meetings"
    assert normalize_date_words("agle saal ka plan") == "next year ka plan"
    assert normalize_date_words("پچھلے ہفتے") == "last week"


def test_date_words_single():
    assert normalize_date_words("aaj") == "today"
    assert normalize_date_words("آج") == "today"
    assert normalize_date_words("abhi") == "now"
    assert normalize_date_words("pichla hafta") == "last week"
    assert normalize_date_words("agla mahina") == "next mahina"


def test_kal_is_kept_ambiguous():
    # "kal" means yesterday AND tomorrow — only the caller may resolve it.
    assert normalize_date_words("kal ki meeting") == "kal ki meeting"
    assert normalize_date_words("کل کی میٹنگ") == "کل کی میٹنگ"


def test_command_words():
    assert normalize_command_words("invoices dikhao") == "invoices show"
    assert normalize_command_words("mujhe batao") == "me tell"
    assert normalize_command_words("kitne invoices hain") == "how many invoices are"
    assert normalize_command_words("match kab hai") == "match when is"
    assert normalize_command_words("یہ کب ہوا") == "یہ when ہوا"


def test_roman_urdu_to_urdu_script():
    assert roman_urdu_to_urdu_script("aaj ka match kab hai") == "آج ka میچ کب ہے"
    assert roman_urdu_to_urdu_script("pichle mahine") == "پچھلے مہینے"
    # Unknown words are left untouched.
    assert roman_urdu_to_urdu_script("hello dunya") == "hello dunya"


def test_urdu_script_to_roman():
    # "کا" has no entry in the key-phrase map, so it stays in Urdu script.
    assert urdu_script_to_roman("آج کا میچ کب ہے") == "aaj کا match kab hai"
    assert urdu_script_to_roman("پچھلے مہینے") == "pichle mahine"
    assert urdu_script_to_roman("hello dunya") == "hello dunya"


def test_normalize_for_matching_roman_urdu_question():
    assert (
        normalize_for_matching("pak a ka next match kb hai?")
        == "pak a ka next match when is?"
    )
    assert (
        normalize_for_matching("pichle mahine ke invoices dikhao")
        == "last month ke invoices show"
    )


def test_normalize_for_matching_urdu_script():
    assert normalize_for_matching("پچھلے مہینے کے انوائس دکھاؤ") == "last month کے انوائس show"
    assert normalize_for_matching("میں نے ۵ انوائس دیکھیں") == "میں نے 5 انوائس دیکھیں"


def test_normalize_for_matching_numbers():
    assert normalize_for_matching("do sau invoices verify hue") == "200 invoices verify hue"
    assert normalize_for_matching("۳۲/۵۰ invoices") == "32/50 invoices"


def test_normalize_for_matching_leaves_english_alone():
    # English "do" must never become the number 2.
    assert normalize_for_matching("Do you have the report?") == "do you have the report?"
    assert normalize_for_matching("Show the main dashboard") == "show the main dashboard"


def test_normalize_for_matching_empty():
    assert normalize_for_matching("") == ""
    assert normalize_for_matching("   ") == ""
