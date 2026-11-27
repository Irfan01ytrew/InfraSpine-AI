"""RAG_OCR_URDU: each page is read by the default and the Arabic-script recognizer; the better script wins."""

from types import SimpleNamespace

from app.rag import ocr
from app.rag.ocr import ScriptAwareOCR, to_urdu_forms


def _engine(*texts: str):
    return lambda image: SimpleNamespace(txts=list(texts), scores=[0.9] * len(texts))


def test_urdu_pages_keep_the_arabic_script_reading_in_urdu_letter_forms():
    # What the default recognizer makes of an Urdu scan, and what the Arabic-script recognizer reads.
    reader = ScriptAwareOCR(_engine("I", ""), _engine("كل رقم ایك لاكه", "ادائيگی تیس دن"))
    result = reader(object())
    assert result.txts == ["کل رقم ایک لاکہ", "ادائیگی تیس دن"]


def test_english_pages_keep_the_default_reading():
    reader = ScriptAwareOCR(_engine("Invoice number INV-2026-0457", "Total 128,450 PKR"), _engine("ﻻ", ""))
    assert reader(object()).txts == ["Invoice number INV-2026-0457", "Total 128,450 PKR"]


def test_urdu_letter_forms():
    assert to_urdu_forms("يك ى ك ه") == "یک ی ک ہ"
    assert to_urdu_forms("Invoice 42") == "Invoice 42"


def test_the_arabic_recognizer_is_used_only_when_enabled(monkeypatch):
    built: list[bool] = []
    monkeypatch.setattr(ocr, "_rapidocr_engine", lambda *, arabic: built.append(arabic) or _engine("x"))
    monkeypatch.setattr("app.core.config.get_settings", lambda: SimpleNamespace(rag_ocr_urdu=False, rag_ocr_cpu_threads=1))
    assert not isinstance(ocr._local_engine(), ScriptAwareOCR) and built == [False]
    built.clear()
    monkeypatch.setattr("app.core.config.get_settings", lambda: SimpleNamespace(rag_ocr_urdu=True, rag_ocr_cpu_threads=1))
    assert isinstance(ocr._local_engine(), ScriptAwareOCR) and built == [False, True]
