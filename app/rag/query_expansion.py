"""Small offline Urdu/Roman Urdu query vocabulary for English-centric embeddings.

This is deliberately a lexical bridge, not a general-purpose translator. It changes only the
query passed to the embedder; source text, stored vectors, retrieval scope and answer prompts stay
unchanged. English words in mixed queries are retained, while unrecognized Urdu-script tokens are
omitted from the expanded query.
"""

from __future__ import annotations

import re
import unicodedata

_URDU_SCRIPT = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")
_TOKENS = re.compile(r"[\w]+", re.UNICODE)
_DIACRITICS = {"Mn", "Me"}

_URDU_PHRASES = {
    "دیکھ بھال": "maintenance",
    "وی پی این": "vpn",
}

# Keep values short and domain-neutral. Function words map to an empty string so Urdu grammar
# does not dominate the English sentence embedding.
_URDU_TERMS = {
    "انوائس": "invoice",
    "تنازعہ": "dispute",
    "رقم": "money",
    "واپسی": "refund return",
    "کتنا": "how much",
    "کتنی": "how many",
    "کتنے": "how many",
    "دن": "days",
    "سالانہ": "annual",
    "چھٹی": "leave",
    "چھٹیاں": "leave",
    "مستقل": "permanent",
    "عملہ": "staff",
    "عملے": "staff",
    "کام": "working work",
    "ملتا": "receive get",
    "ملتی": "receive get",
    "ملتے": "receive get",
    "لاہور": "lahore",
    "کور": "core",
    "سوئچ": "switch",
    "مینٹیننس": "maintenance",
    "کب": "when",
    "وقت": "time",
    "وی": "vpn",
    "پی": "",
    "این": "",
    "ٹوکن": "token",
    "تبدیل": "rotate change",
    "بعد": "after",
    "ہے": "",
    "ہیں": "",
    "ہوتا": "happen",
    "ہوتی": "happen",
    "ہوتے": "happen",
    "کے": "",
    "کی": "",
    "کا": "",
    "کو": "",
    "لیے": "for",
    "میں": "in",
    "پر": "on",
    "سے": "from",
    "کیا": "what",
    "کون": "who",
    "کہاں": "where",
    "ترجیحی": "priority",
    "سپورٹ": "support",
    "شرح": "rate",
    "گھنٹہ": "hour",
    "کارڈ": "card",
}

_ARABIC_TERMS = {
    "فاتورة": "invoice",
    "فواتير": "invoices",
    "نزاع": "dispute",
    "نزاعات": "disputes",
    "استرداد": "refund",
    "إرجاع": "refund return",
    "مبلغ": "amount money",
    "أموال": "money",
    "كم": "how much how many",
    "يوم": "day",
    "أيام": "days",
    "سنة": "year",
    "سنوات": "years",
    "سنوي": "annual",
    "سنوية": "annual",
    "إجازة": "leave",
    "إجازات": "leave",
    "دائم": "permanent",
    "موظف": "staff employee",
    "موظفون": "staff employees",
    "موظفين": "staff employees",
    "عمل": "work working",
    "تقرير": "report",
    "تقارير": "reports",
    "عقد": "contract",
    "عقود": "contracts",
    "سياسة": "policy",
    "سياسات": "policies",
    "صيانة": "maintenance",
    "دعم": "support",
    "أولوية": "priority",
    "حساب": "account",
    "حسابات": "accounts",
    "في": "in",
    "من": "from",
    "إلى": "to",
    "على": "on",
    "عن": "about",
    "مع": "with",
    "متى": "when",
    "أين": "where",
    "لماذا": "why",
    "كيف": "how",
    "ما": "what",
    "ماذا": "what",
}

_URDU_TERMS.update(_ARABIC_TERMS)


_ROMAN_URDU_TERMS = {
    "iss": "this",
    "ye": "this",
    "yeh": "this",
    # Common spellings of image questions. Keep the subject words so English-centric
    # embeddings and lexical search can match OCR text from an uploaded picture.
    "umage": "image",
    "imeg": "image",
    "tasveer": "image picture",
    "tasvir": "image picture",
    "mai": "in",
    "kya": "what",
    "kia": "what",
    "kiya": "what",
    "bat": "messages chats",
    "baat": "messages chats",
    "ho": "is",
    "rhi": "happening",
    "rahi": "happening",
    "rha": "happening",
    "raha": "happening",
    "rahe": "happening",
    "likha": "written text",
    "likhi": "written text",
    "likhe": "written text",
    "kitna": "how much",
    "kitni": "how many",
    "kitne": "how many",
    "din": "days",
    "saal": "year",
    "saalana": "annual",
    "chhutti": "leave",
    "chutti": "leave",
    "chuttiyan": "leave",
    "mustaqil": "permanent",
    "mustaql": "permanent",
    "mulaazim": "staff employee",
    "amalay": "staff",
    "kaam": "work working",
    "milta": "receive get",
    "milti": "receive get",
    "milte": "receive get",
    "wapsi": "refund return",
    "wapasi": "refund return",
    "raqam": "money amount",
    "kab": "when",
    "kahan": "where",
    "waqt": "time",
    "hoti": "happen",
    "hota": "happen",
    "hotay": "happen",
    "baad": "after",
    "liye": "for",
    "kis": "which",
    "ka": "",
    "ki": "",
    "ke": "",
    "ko": "",
    "mein": "in",
    "hai": "",
    "hain": "",
    "tha": "was",
    "thi": "was",
    "aur": "and",
}


def expand_multilingual_query(question: str) -> str:
    """Map recognized Urdu/Roman Urdu terms to English while preserving English query terms.

    If no Urdu term is recognized, return the original question byte-for-byte. This makes pure
    English retrieval a no-op and keeps the feature deterministic and network-free.
    """
    text = unicodedata.normalize("NFKC", question)
    text = "".join(char for char in text if unicodedata.category(char) not in _DIACRITICS)
    for phrase, replacement in _URDU_PHRASES.items():
        text = text.replace(phrase, f" {replacement} ")

    translated = False
    words: list[str] = []
    for token in _TOKENS.findall(text):
        if _URDU_SCRIPT.search(token):
            value = _URDU_TERMS.get(token)
            if value is None and token.startswith("ال") and len(token) > 3:
                value = _URDU_TERMS.get(token[2:])
            if value is None:
                continue
            translated = True
            words.extend(value.split())
        else:
            value = _ROMAN_URDU_TERMS.get(token.casefold())
            if value is None:
                words.append(token)
            else:
                translated = True
                words.extend(value.split())

    expanded = " ".join(words).strip()
    return expanded if translated and expanded else question


# English words the vocabulary produces for question and function words; they carry no topic on their own.
_FUNCTION_WORDS = {
    "what", "who", "where", "which", "when", "how", "many", "much", "for", "from", "after", "and", "was",
    "happen", "receive", "get", "time", "the", "is", "in", "on",
}


def retrieval_queries(question: str) -> list[str]:
    """The queries to search for a question, in order.

    * Nothing to translate: the question itself.
    * Every Urdu term translated: only the English expansion. MiniLM rates any two Urdu-script texts as
      similar, so the original would rank an unrelated Urdu document above the right English one.
    * The expansion kept too few topic words (unknown Urdu terms were dropped, e.g. "گودام کا کوڈ کس شفٹ
      کے لیے ہے؟" becomes "for"): only the original, which still matches documents written in Urdu.
    * Some Urdu terms untranslated but the expansion still names the topic: both, merged by retrieval.
    """
    expanded = expand_multilingual_query(question)
    if expanded == question:
        return [question]
    if not _untranslated_terms(question):
        return [expanded]
    topic = [word for word in expanded.casefold().split() if len(word) >= 3 and word not in _FUNCTION_WORDS]
    if len(topic) < 2:
        return [question]
    return [expanded, question]


def _untranslated_terms(question: str) -> list[str]:
    """Urdu-script words the vocabulary does not know; the expansion drops them."""
    text = unicodedata.normalize("NFKC", question)
    text = "".join(char for char in text if unicodedata.category(char) not in _DIACRITICS)
    for phrase in _URDU_PHRASES:
        text = text.replace(phrase, " ")
    def _is_known(token: str) -> bool:
        if token in _URDU_TERMS:
            return True
        if token.startswith("ال") and len(token) > 3 and token[2:] in _URDU_TERMS:
            return True
        return False
    return [token for token in _TOKENS.findall(text) if _URDU_SCRIPT.search(token) and not _is_known(token)]
