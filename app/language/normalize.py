"""Normalize Roman Urdu / Urdu-script text to a canonical form.

Everything here is a pure function over text. The canonical form is
lowercase English tokens for dates, numbers and common command words, so
downstream routers and parsers (chat routing, NL reporting) only need to
understand one form.

Deliberate non-decisions (see docs/step-z-multilingual-plan.md):

- ``kal`` (کل) is kept as-is: it means both yesterday and tomorrow, and only
  the caller — which sees verb tense — may resolve it.
- ``saath`` is not mapped to 60: it usually means "with". Number words are
  mapped by :func:`normalize_number_words` unconditionally, so callers
  should only apply it to Roman Urdu / Urdu text; :func:`normalize_for_matching`
  already guards on the detected language.
- Transliteration is word-by-word over a key-phrase map; it is not a
  general transliterator and leaves unknown words untouched.
"""

from __future__ import annotations

import re

from .detect import detect_language

# Urdu-script (Extended Arabic-Indic) and Arabic-Indic digits → ASCII.
_DIGIT_MAP = str.maketrans(
    {
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    }
)

ROMAN_NUMBER_WORDS = {
    "ek": 1, "aik": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4,
    "paanch": 5, "panch": 5, "chhe": 6, "chay": 6, "saat": 7, "aath": 8,
    "nao": 9, "nau": 9, "das": 10, "gyarah": 11, "barah": 12, "terah": 13,
    "chaudah": 14, "pandrah": 15, "solah": 16, "satrah": 17, "atharah": 18,
    "unnees": 19, "bees": 20, "ikkees": 21, "baees": 22, "taees": 23,
    "chaubees": 24, "pachees": 25, "chhabbees": 26, "sataees": 27,
    "athaaees": 28, "untees": 29, "tees": 30, "chalees": 40, "pachaas": 50,
    "sattar": 70, "assi": 80, "nabbe": 90,
}

ROMAN_MULTIPLIERS = {
    "sau": 100, "so": 100, "hazaar": 1000, "hazar": 1000,
    "lakh": 100000, "crore": 10000000, "karor": 10000000,
}

URDU_NUMBER_WORDS = {
    "ایک": 1, "دو": 2, "تین": 3, "چار": 4, "پانچ": 5, "چھ": 6, "سات": 7,
    "آٹھ": 8, "نو": 9, "دس": 10, "گیارہ": 11, "بارہ": 12, "تیرہ": 13,
    "چودہ": 14, "پندرہ": 15, "سولہ": 16, "سترہ": 17, "اٹھارہ": 18,
    "انیس": 19, "بیس": 20, "تیس": 30, "چالیس": 40, "پچاس": 50, "ستر": 70,
    "اسی": 80, "نوے": 90,
}

URDU_MULTIPLIERS = {"سو": 100, "ہزار": 1000, "لاکھ": 100000, "کروڑ": 10000000}

# Multi-word date phrases first (longest first), then single words.
# "kal" / "کل" and "parson" / "پرسوں" are intentionally absent: ambiguous.
DATE_WORDS = {
    "pichle mahine": "last month", "پچھلے مہینے": "last month",
    "pichle hafte": "last week", "پچھلے ہفتے": "last week",
    "pichla hafta": "last week", "پچھلا ہفتہ": "last week",
    "is hafte": "this week", "اس ہفتے": "this week",
    "agle hafte": "next week", "اگلے ہفتے": "next week",
    "agla hafta": "next week", "اگلا ہفتہ": "next week",
    "is mahine": "this month", "اس مہینے": "this month",
    "agle mahine": "next month", "اگلے مہینے": "next month",
    "pichle saal": "last year", "پچھلے سال": "last year",
    "is saal": "this year", "اس سال": "this year",
    "agle saal": "next year", "اگلے سال": "next year",
    "aaj": "today", "آج": "today",
    "abhi": "now", "ابھی": "now",
    "pichla": "last", "پچھلا": "last",
    "pichle": "last", "پچھلے": "last",
    "pichli": "last", "پچھلی": "last",
    "agla": "next", "اگلا": "next",
    "agle": "next", "اگلے": "next",
    "agli": "next", "اگلی": "next",
    "guzra": "past", "گزرا": "past",
    "guzri": "past", "گذری": "past",
}

COMMAND_WORDS = {
    "dikhao": "show", "دکھاؤ": "show",
    "dikhaen": "show", "dikhayen": "show",
    "batao": "tell", "بتاؤ": "tell",
    "btao": "tell", "bataen": "tell", "بتائیں": "tell",
    "kitne": "how many", "کتنے": "how many",
    "kitna": "how much", "کتنا": "how much",
    "kitni": "how many", "کتنی": "how many",
    "kab": "when", "کب": "when", "kb": "when", "kub": "when",
    "kahan": "where", "کہاں": "where",
    "kidhar": "where", "کدھر": "where",
    "kaun": "who", "کون": "who", "kon": "who",
    "kyun": "why", "کیوں": "why",
    "kaise": "how", "کیسے": "how",
    "kya": "what", "کیا": "what", "kia": "what",
    "hai": "is", "ہے": "is",
    "hain": "are", "ہیں": "are",
    "mujhe": "me", "مجھے": "me",
    "mera": "my", "میرا": "my",
    "meri": "my", "میری": "my",
    "mere": "my", "میرے": "my",
    "chahiye": "need", "چاہیے": "need",
    "karo": "do", "کرو": "do",
}

# Key-phrase transliteration, Roman Urdu → Urdu script (word by word).
ROMAN_TO_URDU = {
    "aaj": "آج", "kal": "کل", "abhi": "ابھی", "parson": "پرسوں",
    "kya": "کیا", "kab": "کب", "kahan": "کہاں", "kaun": "کون",
    "kyun": "کیوں", "kaise": "کیسے", "kitna": "کتنا", "kitni": "کتنی",
    "kitne": "کتنے", "hai": "ہے", "hain": "ہیں", "mera": "میرا",
    "meri": "میری", "mere": "میرے", "mujhe": "مجھے", "dikhao": "دکھاؤ",
    "batao": "بتاؤ", "mahina": "مہینہ", "mahine": "مہینے",
    "hafta": "ہفتہ", "hafte": "ہفتے", "saal": "سال", "din": "دن",
    "match": "میچ", "cricket": "کرکٹ", "score": "اسکور", "jeeta": "جیتا",
    "pichla": "پچھلا", "pichle": "پچھلے", "agla": "اگلا", "agle": "اگلے",
    "agli": "اگلی", "invoice": "انوائس", "payment": "ادائیگی",
    "paisa": "پیسہ",
}

URDU_TO_ROMAN = {urdu: roman for roman, urdu in ROMAN_TO_URDU.items()}


def normalize_digits(text: str) -> str:
    """Urdu-script and Arabic-Indic digits → ASCII digits."""
    return text.translate(_DIGIT_MAP) if text else text


def _word_replacer(mapping: dict[str, str]):
    """Return a function replacing whole words from *mapping*.

    Longest keys first so multi-word phrases win over single words.
    Lookup is case-insensitive for Latin words; Urdu script has no case.
    """
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(key) for key in keys) + r")(?!\w)",
        re.IGNORECASE,
    )

    def replace(text: str) -> str:
        def _sub(match: re.Match[str]) -> str:
            word = match.group(1)
            return mapping.get(word, mapping.get(word.casefold(), word))

        return pattern.sub(_sub, text)

    return replace


_replace_date_words = _word_replacer(DATE_WORDS)
_replace_command_words = _word_replacer(COMMAND_WORDS)
_replace_roman_to_urdu = _word_replacer(ROMAN_TO_URDU)
_replace_urdu_to_roman = _word_replacer(URDU_TO_ROMAN)


def normalize_date_words(text: str) -> str:
    """Roman Urdu / Urdu date words → canonical English tokens.

    ``"pichle mahine"`` → ``"last month"``; ``"aaj"`` → ``"today"``.
    ``"kal"`` is left untouched (yesterday or tomorrow — ambiguous).
    """
    return _replace_date_words(text) if text else text


def normalize_command_words(text: str) -> str:
    """Common Roman Urdu / Urdu question and command words → English tokens.

    ``"dikhao"`` → ``"show"``, ``"kb"`` is handled by callers via ``"kab"``
    → ``"when"``; ``"kitne"`` → ``"how many"``.
    """
    return _replace_command_words(text) if text else text


_PUNCT_WRAP = re.compile(r"^(\W*)([\w]+)(\W*)$", re.UNICODE)


def normalize_number_words(text: str) -> str:
    """Roman Urdu / Urdu number words → digits, with multipliers.

    ``"do sau"`` → ``"200"``, ``"paanch hazaar"`` → ``"5000"``,
    ``"دو سو"`` → ``"200"``, ``"sau"`` alone → ``"100"``.
    Applies unconditionally: only call it on Roman Urdu / Urdu text (see
    :func:`normalize_for_matching`), because e.g. ``"do"`` is also English.
    """
    if not text:
        return text
    values = {**ROMAN_NUMBER_WORDS, **URDU_NUMBER_WORDS}
    multipliers = {**ROMAN_MULTIPLIERS, **URDU_MULTIPLIERS}
    chunks = re.split(r"(\s+)", text)
    parsed: list[tuple[tuple[str, str, str] | None, str]] = []
    for chunk in chunks:
        if not chunk or chunk.isspace():
            parsed.append((None, chunk))
            continue
        match = _PUNCT_WRAP.match(chunk)
        parsed.append((match.groups() if match else None, chunk))
    out: list[str] = []
    j = 0
    while j < len(parsed):
        item, raw = parsed[j]
        if item is None:
            out.append(raw)
            j += 1
            continue
        pre, core, suf = item
        low = core.casefold()
        # Find the next word chunk (skipping whitespace) for multipliers.
        k = j + 1
        while k < len(parsed) and parsed[k][0] is None:
            k += 1
        if low in values and k < len(parsed) and parsed[k][0] is not None:
            npre, ncore, nsuf = parsed[k][0]
            if ncore.casefold() in multipliers:
                total = values[low] * multipliers[ncore.casefold()]
                out.append(f"{pre}{total}{suf}{nsuf}")
                j = k + 1
                continue
        if low in multipliers:
            out.append(f"{pre}{multipliers[low]}{suf}")
        elif low in values:
            out.append(f"{pre}{values[low]}{suf}")
        else:
            out.append(raw)
        j += 1
    return "".join(out)


def roman_urdu_to_urdu_script(text: str) -> str:
    """Transliterate known Roman Urdu key words to Urdu script.

    Unknown words are left untouched, e.g. ``"aaj ka match kab hai"`` →
    ``"آج ka میچ کب ہے"``.
    """
    return _replace_roman_to_urdu(text) if text else text


def urdu_script_to_roman(text: str) -> str:
    """Transliterate known Urdu-script key words to Roman Urdu.

    Unknown words are left untouched, e.g. ``"آج کا میچ کب ہے"`` →
    ``"aaj ka match kab hai"``.
    """
    return _replace_urdu_to_roman(text) if text else text


def normalize_for_matching(text: str) -> str:
    """Full canonical form for routing / matching.

    Lowercases, converts Urdu digits, and — only for Roman Urdu, Urdu or
    mixed text — maps number, date and command words to canonical English
    tokens. English text is left alone apart from digit conversion, so
    English words such as ``"do"`` are never mistaken for the number 2.
    """
    if not text:
        return ""
    lang = detect_language(text)
    out = normalize_digits(text.casefold())
    if lang in ("roman_urdu", "urdu", "mixed"):
        out = normalize_number_words(out)
        out = normalize_date_words(out)
        out = normalize_command_words(out)
    return re.sub(r"\s+", " ", out).strip()
