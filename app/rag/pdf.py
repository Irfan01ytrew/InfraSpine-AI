"""PDF inspection, page-by-page text extraction and page rendering. Blocking: run in a thread.

Permissively licensed, commercially usable components only:
* pypdf (BSD-3-Clause, pure Python) - validation, encryption check, text extraction.
* pypdfium2 (Apache-2.0 / BSD-3-Clause, Google PDFium) - rendering pages to images.
"""

import io
import logging
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import pypdf
import pypdfium2
from pypdf import PasswordType

from app.rag.parsers.base import ParseError

# pypdf warns about recoverable syntax issues in real-world PDFs; keep that noise out of app logs.
logging.getLogger("pypdf").setLevel(logging.ERROR)


class PdfError(ParseError):
    """A permanent problem with the PDF itself; retrying will not help."""


@dataclass(frozen=True)
class PageText:
    number: int  # 1-based
    text: str


def _read(path: Path) -> tuple[bytes, pypdf.PdfReader]:
    # Parse from memory: no OS file handle stays open, so the file can be deleted or replaced
    # at any time (Windows locks open files). Size is capped at upload.
    data = path.read_bytes()
    try:
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            # Owner-password-only PDFs (e.g. "no printing") open with an empty password; real
            # password protection does not.
            if reader.decrypt("") == PasswordType.NOT_DECRYPTED:
                raise PdfError("encrypted_pdf", "Password-protected PDFs are not supported.")
        page_count = len(reader.pages)
    except PdfError:
        raise
    except Exception:
        raise PdfError("invalid_pdf", "The PDF could not be read. It may be damaged.") from None
    if page_count == 0:
        raise PdfError("empty_pdf", "The PDF has no pages.")
    return data, reader


def inspect_pdf(path: Path, max_pages: int) -> int:
    """Validate that the file is a readable, unencrypted PDF within limits. Returns the page count."""
    _, reader = _read(path)
    pages = len(reader.pages)
    if pages > max_pages:
        raise PdfError("too_many_pages", f"The PDF has {pages} pages; the limit is {max_pages}.")
    return pages


def extract_pages(path: Path, max_pages: int, max_chars: int, *, require_text: bool = True) -> list[PageText]:
    _, reader = _read(path)
    if len(reader.pages) > max_pages:
        raise PdfError("too_many_pages", f"The PDF has {len(reader.pages)} pages; the limit is {max_pages}.")
    pages: list[PageText] = []
    total = 0
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""  # "plain" mode: natural reading order
        except Exception:
            raise PdfError("invalid_pdf", "The PDF could not be read. It may be damaged.") from None
        total += len(text)
        if total > max_chars:
            raise PdfError("text_too_large", "The PDF contains more text than the processing limit allows.")
        pages.append(PageText(number=index + 1, text=text))
    if require_text and not any(p.text.strip() for p in pages):
        raise PdfError("no_text", "No extractable text found. Scanned PDFs need OCR, which is not supported yet.")
    return pages


MAX_RENDER_HEIGHT = 4000  # px; guards against huge renders from oddly shaped pages


def _png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode 8-bit RGB pixels as PNG (no imaging library needed)."""
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride : (y + 1) * stride] for y in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit, truecolour RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def render_page_png(path: Path, page_number: int, width: int) -> bytes:
    """Render one page (1-based) to PNG on this machine. Blocking: run in a thread."""
    data, reader = _read(path)
    if not 1 <= page_number <= len(reader.pages):
        raise PdfError("page_not_found", "That page does not exist in this document.")
    pdf = pypdfium2.PdfDocument(data, password="" if reader.is_encrypted else None)
    try:
        page = pdf[page_number - 1]
        page_width, page_height = page.get_size()
        scale = min(width / page_width, 4.0, MAX_RENDER_HEIGHT / page_height)
        bitmap = page.render(scale=scale, rev_byteorder=True)  # RGB(A) byte order for PNG
        pixels = bitmap.to_numpy()
        rgb = pixels[:, :, :3] if pixels.shape[2] >= 3 else pixels.repeat(3, axis=2)
        return _png(rgb.shape[1], rgb.shape[0], rgb.tobytes())
    finally:
        pdf.close()
