"""File-type detection from the file's content (signatures and container structure).

The filename extension only says what the uploader *claims*; the content decides. A file is accepted
only when both agree, so a renamed file can never reach a parser for another format. Plain text has
no signature, so TXT and CSV are told apart by the extension once the content is confirmed to be text.
"""

import re
import zipfile
from pathlib import Path

from app.rag.parsers.base import ParseError

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
WEBP_RIFF = b"RIFF"
CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE compound file: legacy Office, or encrypted OOXML

# Main-part content types of the supported Office Open XML packages. Macro-enabled variants (.docm,
# .xlsm, .pptm) are deliberately not listed: they are not accepted.
OOXML_MAIN_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml": "pptx",
}
_OVERRIDE = re.compile(rb"<Override\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(rb'(PartName|ContentType)\s*=\s*"([^"]*)"')

_TEXT_SAMPLE = 64 * 1024
MAX_ZIP_ENTRIES = 10_000
_DTD_MARKERS = tuple(
    marker.encode(encoding)
    for marker in ("<!doctype", "<!entity")
    for encoding in ("ascii", "utf-16-le", "utf-16-be")
)
_SCAN_BLOCK = 64 * 1024
_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac"})
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})


def unsupported(message: str = "This file type is not supported.") -> ParseError:
    return ParseError("unsupported_file_type", message)


def detect_format(path: Path, extension_format: str, max_unpacked_bytes: int) -> str:
    """The file's real format. Raises `ParseError` when it is unsupported, unsafe or contradicts its extension."""
    with path.open("rb") as handle:
        head = handle.read(_TEXT_SAMPLE)
    if not head:
        raise ParseError("empty_file", "The file is empty.")
    if head.startswith(PDF_MAGIC):
        detected = "pdf"
    elif head.startswith(PNG_MAGIC):
        detected = "png"
    elif head.startswith(JPEG_MAGIC):
        detected = "jpeg"
    elif len(head) >= 12 and head.startswith(WEBP_RIFF) and head[8:12] == b"WEBP":
        detected = "webp"
    elif head.startswith(ZIP_MAGIC):
        detected = ooxml_format(path, max_unpacked_bytes)
    elif head.startswith(CFB_MAGIC):
        import olefile

        try:
            with olefile.OleFileIO(str(path)) as ole:
                if ole.exists("EncryptionInfo") or ole.exists("EncryptedPackage"):
                    raise ParseError("encrypted_file", "Password-protected Office files are not supported.")
                if ole.exists("WordDocument"):
                    detected = "doc"
                elif ole.exists("Workbook") or ole.exists("Book"):
                    detected = "xls"
                elif ole.exists("PowerPoint Document"):
                    detected = "ppt"
                else:
                    detected = extension_format if extension_format in ("doc", "xls", "ppt") else "doc"
        except ParseError:
            raise
        except Exception:
            raise ParseError("invalid_file", "The Office file could not be read. It may be damaged.") from None
    elif extension_format in ("audio", "video") and _media_signature(path.suffix.lower(), head):
        detected = extension_format
    elif looks_like_text(head):
        detected = extension_format if extension_format in ("txt", "csv", "md", "json", "html") else "txt"
    else:
        raise unsupported("This file type is not supported.")
    if detected != extension_format:
        raise ParseError(
            "file_type_mismatch",
            f"The file's content ({detected.upper()}) does not match its .{extension_format} extension.",
        )
    return detected


def _media_signature(extension: str, head: bytes) -> bool:
    """Cheap container/frame checks before an optional decoder runs in the isolated worker."""
    if extension not in _AUDIO_EXTENSIONS | _VIDEO_EXTENSIONS:
        return False
    if extension == ".wav":
        return len(head) >= 12 and head[:4] in (b"RIFF", b"RF64") and head[8:12] == b"WAVE"
    if extension == ".flac":
        return head.startswith(b"fLaC")
    if extension in {".ogg", ".opus"}:
        return head.startswith(b"OggS")
    if extension == ".mp3":
        return head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if extension == ".aac":
        return len(head) >= 2 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0
    if extension in {".mp4", ".mov", ".m4v", ".m4a"}:
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if extension in {".mkv", ".webm"}:
        return head.startswith(b"\x1a\x45\xdf\xa3")
    if extension == ".avi":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"AVI "
    return False


def looks_like_text(sample: bytes) -> bool:
    """UTF-8/UTF-16 (with BOM) or single-byte text without binary control characters."""
    if sample.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return True
    if b"\x00" in sample:
        return False
    control = sum(1 for b in sample if b < 0x09 or 0x0E <= b < 0x20 or b == 0x7F)
    return control <= len(sample) // 1000


def open_package(path: Path, max_unpacked_bytes: int) -> zipfile.ZipFile:
    """Open an Office package after checking it cannot exhaust resources or smuggle XML entities."""
    try:
        package = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError):
        raise ParseError("invalid_file", "The file could not be read. It may be damaged.") from None
    try:
        entries = package.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise ParseError("file_too_complex", "The file contains too many parts to process.")
        if sum(e.file_size for e in entries) > max_unpacked_bytes:
            raise ParseError("file_too_large", "The file expands beyond the processing limit when unpacked.")
        if any(e.flag_bits & 0x1 for e in entries):
            raise ParseError("encrypted_file", "Password-protected files are not supported.")
        for entry in entries:
            name = entry.filename.lower()
            if name.endswith((".xml", ".rels")) or name == "[content_types].xml":
                _reject_dtd(package, entry)
    except ParseError:
        package.close()
        raise
    except (zipfile.BadZipFile, OSError, ValueError, EOFError, RuntimeError, NotImplementedError):
        package.close()
        raise ParseError("invalid_file", "The file could not be read. It may be damaged.") from None
    return package


def _reject_dtd(package: zipfile.ZipFile, entry: zipfile.ZipInfo) -> None:
    """Office XML never contains a DTD. Refusing any DOCTYPE/ENTITY rules out entity expansion and
    external-entity attacks regardless of how a library configures its XML parser."""
    tail = b""
    with package.open(entry) as stream:
        while block := stream.read(_SCAN_BLOCK):
            window = (tail + block).lower()
            if any(marker in window for marker in _DTD_MARKERS):
                raise ParseError("unsafe_file", "The file contains XML constructs that are not allowed.")
            tail = window[-32:]


def ooxml_format(path: Path, max_unpacked_bytes: int) -> str:
    with open_package(path, max_unpacked_bytes) as package:
        try:
            types = package.read("[Content_Types].xml")
        except KeyError:
            raise unsupported("ZIP archives are not supported; only Word, Excel and PowerPoint files.") from None
    main_types = set()
    for override in _OVERRIDE.findall(types):
        attrs = dict(_ATTR.findall(override))
        content_type = attrs.get(b"ContentType", b"").decode("ascii", "replace")
        if content_type in OOXML_MAIN_TYPES:
            main_types.add(OOXML_MAIN_TYPES[content_type])
        elif "macroenabled" in content_type.lower() and ".main" in content_type:
            raise unsupported("Macro-enabled Office files are not supported. Save the file without macros.")
    if len(main_types) != 1:
        raise unsupported("Only Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) Office files are supported.")
    return main_types.pop()
