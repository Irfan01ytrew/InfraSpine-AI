"""Private on-disk storage for uploaded documents.

Files are stored under random names inside a single directory that is never
mounted by the web server. User-supplied filenames are display metadata only
and never take part in building a filesystem path.
"""

import hashlib
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

PDF_MAGIC = b"%PDF-"
# Extensions a stored file may carry. The real format is detected from content by the parser registry.
STORED_EXTENSIONS = (
    ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".csv", ".md", ".markdown", ".json", ".html", ".htm",
    ".png", ".jpg", ".jpeg", ".webp",
    ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac",
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
)
_STORAGE_KEY = re.compile(
    r"^[0-9a-f]{32}\.(pdf|docx|xlsx|pptx|txt|csv|md|markdown|json|html|htm|png|jpg|jpeg|webp|wav|mp3|m4a|flac|ogg|opus|aac|mp4|mov|mkv|webm|avi|m4v)$"
)
_FORBIDDEN_NAME_CHARS = set('<>:"/\\|?*')
_COPY_BUFFER = 1024 * 1024


class UnsafeFileError(Exception):
    """The file or its name was rejected; `message` is safe to show to the client."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StoredFile:
    key: str
    size_bytes: int
    sha256: str


def sanitize_filename(raw: str | None, extensions: tuple[str, ...] = (".pdf",)) -> str:
    """Reduce a client-supplied name to a safe display name ending in one of `extensions` (lowercased)."""
    name = (raw or "").replace("\\", "/").split("/")[-1]  # drop any directory part
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if ch.isprintable() and ch not in _FORBIDDEN_NAME_CHARS)
    name = name.strip()
    extension = next((ext for ext in extensions if name.lower().endswith(ext)), None)
    if extension is None:
        message = (
            "Only PDF files (.pdf) are supported."
            if extensions == (".pdf",)
            else f"Supported file types: {', '.join(extensions)}."
        )
        raise UnsafeFileError("unsupported_file_type", message)
    stem = name[: -len(extension)].strip().strip(".").strip() or "document"  # no hidden/dot-only names
    return f"{stem[:200]}{extension}"


class DocumentStorage:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()

    def path_for(self, key: str) -> Path:
        if not _STORAGE_KEY.fullmatch(key):
            raise UnsafeFileError("invalid_storage_key", "Invalid storage key.")
        path = (self.root / key).resolve()
        if path.parent != self.root:  # defence in depth against traversal
            raise UnsafeFileError("invalid_storage_key", "Invalid storage key.")
        return path

    def save(self, source: BinaryIO, max_bytes: int, extension: str = ".pdf") -> StoredFile:
        """Copy an upload into storage, enforcing the size limit (and the signature, for PDFs; other
        formats are detected from content by the parser registry after saving).

        Blocking: call via `asyncio.to_thread`. Writes to a temporary name and renames
        atomically, so a partially written file is never visible under its final key.
        """
        if extension not in STORED_EXTENSIONS:
            raise UnsafeFileError("unsupported_file_type", "This file type is not supported.")
        key = f"{uuid.uuid4().hex}{extension}"
        final_path = self.path_for(key)
        temp_path = final_path.with_name(f"{key}.part")
        digest = hashlib.sha256()
        size = 0
        try:
            with temp_path.open("xb") as out:
                first = True
                while chunk := source.read(_COPY_BUFFER):
                    if first:
                        if extension == ".pdf" and not chunk.startswith(PDF_MAGIC):
                            raise UnsafeFileError("not_a_pdf", "The file is not a valid PDF document.")
                        first = False
                    size += len(chunk)
                    if size > max_bytes:
                        raise UnsafeFileError("file_too_large", f"The file exceeds the {max_bytes // (1024 * 1024)} MB limit.")
                    digest.update(chunk)
                    out.write(chunk)
                if first:
                    raise UnsafeFileError("empty_file", "The file is empty.")
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp_path, final_path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return StoredFile(key=key, size_bytes=size, sha256=digest.hexdigest())

    def preview_path(self, key: str) -> Path:
        """Where the PDF rendering of an Office file is cached (see `app.rag.office_preview`)."""
        source = self.path_for(key)
        return self.root / "previews" / f"{source.stem}.pdf"

    def delete(self, key: str) -> None:
        source = self.path_for(key)
        source.unlink(missing_ok=True)
        self.preview_path(key).unlink(missing_ok=True)
        preview_dir = self.preview_path(key).parent
        if preview_dir.is_dir():
            for preview in preview_dir.glob(f"{source.stem}.*.png"):
                preview.unlink(missing_ok=True)
