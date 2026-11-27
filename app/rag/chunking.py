"""Character-based chunking with overlap. Chunks never cross page or section boundaries, so every
chunk maps to exactly one source location (page, section, sheet range, slide, line or row range)."""

import re
from dataclasses import dataclass

from app.rag.parsers.base import Section
from app.rag.pdf import PageText

# Preferred break points, strongest first. A break is only taken in the second half
# of the window, so chunks don't become tiny.
_SEPARATORS = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ")


@dataclass(frozen=True)
class TextChunk:
    chunk_index: int
    page_number: int  # the locator's position (the page, for PDFs)
    text: str
    locator: dict | None = None


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    if not 0 <= overlap < chunk_size // 2:
        raise ValueError("overlap must be >= 0 and less than half of chunk_size")
    if len(text) <= chunk_size:
        return [text] if text else []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            window = text[start:end]
            for separator in _SEPARATORS:
                position = window.rfind(separator, chunk_size // 2)
                if position != -1:
                    end = start + position + len(separator)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        next_start = end - overlap
        # Start the overlap at a word boundary rather than mid-word.
        space = text.find(" ", next_start, end)
        if overlap and space != -1:
            next_start = space + 1
        start = max(next_start, start + 1)
    return chunks


def chunk_pages(pages: list[PageText], chunk_size: int, overlap: int) -> list[TextChunk]:
    chunks: list[TextChunk] = []
    for page in pages:
        for piece in split_text(normalize_text(page.text), chunk_size, overlap):
            chunks.append(TextChunk(chunk_index=len(chunks), page_number=page.number, text=piece))
    return chunks


def chunk_sections(sections: list[Section], chunk_size: int, overlap: int) -> list[TextChunk]:
    """Chunk parser sections; every chunk inherits its section's locator."""
    chunks: list[TextChunk] = []
    for section in sections:
        locator = section.locator.to_json()
        for piece in split_text(normalize_text(section.text), chunk_size, overlap):
            chunks.append(TextChunk(len(chunks), section.locator.position, piece, locator))
    return chunks
