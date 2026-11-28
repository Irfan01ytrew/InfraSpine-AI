"""Receipt-photo intake (Step AC, slice 2): a photographed receipt becomes a `pending`
invoice in the approval queue — never an auto-payment.

Pipeline: validate the image (type/size/magic bytes) -> OCR text -> the same
deterministic `facts_from_text()` extractor as every other intake path -> the
same 17 verification checks (run by the invoice workflow, not here) -> `pending`.

Extracted text is untrusted data: it fills invoice fields only. It cannot choose
a payee account (the vendor master does), cannot grant a permission and cannot
change what an approval covers. An instruction written on a receipt is just text.

The OCR engine is injected (`ocr_provider`) so tests can pass a fake reader; the
default provider is the local offline RapidOCR engine already used for document
ingestion (`app.rag.ocr`). If that engine is unavailable on this host, intake
fails with a clear error instead of guessing an invoice.
"""

from __future__ import annotations

import hashlib
from typing import Callable

from app.core.errors import InvalidInputError
from app.finance import extraction

MAX_RECEIPT_BYTES = 10 * 1024 * 1024

# extension -> (magic-byte prefix, mime)
_IMAGE_TYPES = {
    '.png': (b'\x89PNG\r\n\x1a\n', 'image/png'),
    '.jpg': (b'\xff\xd8\xff', 'image/jpeg'),
    '.jpeg': (b'\xff\xd8\xff', 'image/jpeg'),
    '.webp': (b'RIFF', 'image/webp'),
}
_ALLOWED_EXTENSIONS = frozenset(_IMAGE_TYPES)

OcrProvider = Callable[[bytes], str]


def _extension(filename: str) -> str:
    name = (filename or '').strip().lower()
    dot = name.rfind('.')
    return name[dot:] if dot >= 0 else ''


def validate_receipt_image(filename: str, content: bytes) -> str:
    """Check the upload is really an image photo. Returns the normalised extension."""
    if not content:
        raise InvalidInputError('The receipt photo is empty.')
    if len(content) > MAX_RECEIPT_BYTES:
        raise InvalidInputError('Receipt photos are limited to 10 MB.')
    ext = _extension(filename)
    if ext not in _ALLOWED_EXTENSIONS:
        raise InvalidInputError('Receipt photos must be a PNG, JPG or WebP image.')
    magic, _mime = _IMAGE_TYPES[ext]
    if not content.startswith(magic):
        raise InvalidInputError('The file does not look like the image type its name claims.')
    if ext == '.webp' and content[8:12] != b'WEBP':
        raise InvalidInputError('The file does not look like a WebP image.')
    return ext


def ocr_receipt_text(content: bytes) -> str:
    """Read a receipt photo with the local offline OCR engine (no network, no upload).

    Raises InvalidInputError with a plain-language reason when OCR is unavailable
    or the photo contains no readable text — the caller must surface that to the
    user, never invent an invoice from it.
    """
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise InvalidInputError('Receipt OCR is not available on this server.') from exc
    from app.rag.ocr import ParseError, _local_engine

    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise InvalidInputError('The receipt photo could not be decoded as an image.')
    try:
        engine = _local_engine()
    except ParseError as exc:
        raise InvalidInputError('Receipt OCR is not available on this server.') from exc
    result = engine(image)
    lines = [str(part).strip() for part in (getattr(result, 'txts', None) or ()) if str(part).strip()]
    return '\n'.join(lines)


def extract_receipt_facts(content: bytes, filename: str, *, date_order: str = 'dmy',
                          ocr_provider: OcrProvider | None = None) -> dict:
    """Image bytes -> invoice facts, through the same extractor as text/file intake.

    A photo OCR cannot read (blank, blurry, no text) is an error, never a guessed
    invoice: the caller reports it and creates nothing.
    """
    validate_receipt_image(filename, content)
    provider = ocr_provider or ocr_receipt_text
    text = provider(content)
    if not isinstance(text, str) or not text.strip():
        raise InvalidInputError('The receipt photo could not be read. '
                                'Try a clearer, well-lit photo of the whole receipt.')
    return extraction.facts_from_text(text, date_order=date_order, filename='receipt.jpg')


def receipt_digest(content: bytes) -> str:
    """Source digest for idempotency: re-sending the same photo returns the same invoice."""
    return hashlib.sha256(content).hexdigest()
