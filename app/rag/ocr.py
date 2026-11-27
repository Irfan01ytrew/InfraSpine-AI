"""Offline, page-at-a-time OCR for image-only PDF pages."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.rag.parsers.base import ParseError, ParseLimits, Section, TextBudget, page_locator


def _configure_cpu_threads() -> None:
    """Keep local OCR within its per-parser CPU budget on hosts with many services/workers."""
    try:
        import cv2
    except ImportError:
        return
    from app.core.config import get_settings

    cv2.setNumThreads(getattr(get_settings(), "rag_ocr_cpu_threads", 1))


ARABIC_REC_MODEL = "arabic_PP-OCRv5_rec_mobile.onnx"
# The Arabic-script recognizer emits Arabic letter forms; Urdu text uses these Urdu code points instead.
_URDU_FORMS = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ه": "ہ"})
_ARABIC_SCRIPT = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_LATIN_OR_DIGIT = re.compile(r"[A-Za-z0-9]")


def to_urdu_forms(text: str) -> str:
    """Map Arabic letter forms from the recognizer to the Urdu letters people type when searching."""
    return text.translate(_URDU_FORMS)


class ScriptAwareOCR:
    """Reads a page with the default (Latin/Chinese) and the Arabic-script recognizer and keeps the reading
    that recovered more of its own script. Pages that mix English and Urdu lines keep the dominant script."""

    def __init__(self, default, arabic) -> None:
        self._default = default
        self._arabic = arabic

    def __call__(self, image):
        first = self._default(image)
        second = self._arabic(image)
        latin = sum(len(_LATIN_OR_DIGIT.findall(t)) for t in (getattr(first, "txts", None) or ()))
        arabic = sum(len(_ARABIC_SCRIPT.findall(t)) for t in (getattr(second, "txts", None) or ()))
        if arabic > latin:
            texts = [to_urdu_forms(str(t)) for t in (getattr(second, "txts", None) or ())]
            return SimpleNamespace(txts=texts, scores=getattr(second, "scores", None))
        return first


def _local_engine():
    """Load RapidOCR from verified local weights only; ingestion must never trigger a model download.

    With RAG_OCR_URDU on and the Arabic-script model present, Urdu (and Arabic/Persian) scans are read too."""
    from app.core.config import get_settings

    default = _rapidocr_engine(arabic=False)
    if not getattr(get_settings(), "rag_ocr_urdu", False):
        return default
    return ScriptAwareOCR(default, _rapidocr_engine(arabic=True))


def _rapidocr_engine(*, arabic: bool):
    _configure_cpu_threads()
    from rapidocr import RapidOCR
    from rapidocr.utils.download_file import DownloadFile
    from rapidocr.utils.utils import get_file_sha256
    from rapidocr.utils.typings import EngineType, LangRec, ModelType, OCRVersion

    model_dir = Path(__import__("rapidocr").__file__).resolve().parent / "models"
    # RapidOCR's ONNX recognizer embeds its character set in the model. A separate
    # ppocrv6_dict.txt is only needed by non-ONNX backends; requiring it here falsely
    # disables valid offline ONNX installations.
    required = ("PP-OCRv6_det_small.onnx", ARABIC_REC_MODEL if arabic else "PP-OCRv6_rec_small.onnx")
    if any(not (model_dir / name).is_file() for name in required):
        raise ParseError("ocr_models_missing", "Local OCR model files are not available.")
    rec = (
        {"Rec.lang_type": LangRec.ARABIC, "Rec.model_type": ModelType.MOBILE, "Rec.ocr_version": OCRVersion.PPOCRV5}
        if arabic
        else {"Rec.model_type": ModelType.SMALL, "Rec.ocr_version": OCRVersion.PPOCRV6}
    )

    original_run = DownloadFile.__dict__["run"]

    def local_only_run(cls, input_params) -> None:
        model_path = Path(input_params.save_path)
        if not model_path.is_file():
            raise FileNotFoundError("Required OCR model weights are not available locally.")
        if input_params.sha256 and get_file_sha256(model_path) != input_params.sha256:
            raise FileNotFoundError("Required OCR model weights are invalid locally.")

    DownloadFile.run = classmethod(local_only_run)
    try:
        return RapidOCR(
            params={
                "Global.model_root_dir": str(model_dir),
                "Global.use_cls": False,
                "Det.engine_type": EngineType.ONNXRUNTIME,
                "Det.model_type": ModelType.SMALL,
                "Det.ocr_version": OCRVersion.PPOCRV6,
                "Rec.engine_type": EngineType.ONNXRUNTIME,
                **rec,
            }
        )
    except FileNotFoundError as exc:
        raise ParseError("ocr_models_missing", "Local OCR model files are missing or invalid.") from exc
    finally:
        DownloadFile.run = original_run


def _visually_blank(image: np.ndarray) -> bool:
    """Treat only a rendered page with no meaningful dark pixels as blank.

    This check keeps a blank separator page from failing an otherwise readable PDF while
    still refusing a nonblank scanned page that OCR could not read.
    """
    if image.ndim == 3:
        image = image[:, :, :3].astype(np.float32).mean(axis=2)
    # A nonblank scanned page has substantially more than a few anti-aliasing specks.
    return int(np.count_nonzero(image < 248)) < 32


def ocr_pdf_pages(path: Path, limits: ParseLimits, pages: list[int] | None = None) -> list[Section]:
    """OCR requested 1-based pages, or all pages, while keeping only one rendered page in memory."""
    import pypdfium2

    try:
        pdf = pypdfium2.PdfDocument(str(path))
    except Exception:
        raise ParseError("invalid_pdf", "The PDF could not be rendered for OCR.") from None
    try:
        count = len(pdf)
        if count > limits.max_units:
            raise ParseError("too_many_pages", f"The PDF has {count} pages; the limit is {limits.max_units}.")
        selected = set(pages) if pages is not None else set(range(1, count + 1))
        if not selected:
            return []
        if min(selected) < 1 or max(selected) > count:
            raise ParseError("page_not_found", "An OCR page number is outside the PDF.")

        engine = _local_engine()
        budget = TextBudget(limits.max_chars)
        sections: list[Section] = []
        for number in sorted(selected):
            page = pdf[number - 1]
            width, height = page.get_size()
            scale = min(2.5, 2400 / max(width, height), 1.0 if max(width, height) >= 2400 else 2.5)
            bitmap = page.render(scale=max(scale, 0.5), rev_byteorder=True)
            image = np.asarray(bitmap.to_numpy())
            if image.ndim == 3 and image.shape[2] > 3:
                image = image[:, :, :3]
            result = engine(np.ascontiguousarray(image))
            text = "\n".join(part.strip() for part in (getattr(result, "txts", None) or ()) if part.strip())
            if not text and not _visually_blank(image):
                raise ParseError("ocr_no_text", "Local OCR could not recognize text on a nonblank PDF page.")
            sections.append(Section(budget.take(text), page_locator(number)))
        return sections
    finally:
        pdf.close()


def ocr_page_text(path: Path, page_number: int, limits: ParseLimits) -> str:
    """Convenience wrapper used by the local OCR diagnostics."""
    sections = ocr_pdf_pages(path, limits, [page_number])
    return sections[0].text if sections else ""
