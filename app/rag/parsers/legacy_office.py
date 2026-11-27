"""Legacy Microsoft Office 97-2003 binary formats (.doc, .xls, .ppt).

Extracts clean text and tables from binary OLE Compound File packages using
pure Python parsing (`olefile` for OLE container, `xlrd` for binary Excel,
and [MS-DOC] / [MS-PPT] record extractors).
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

import olefile

from app.rag.parsers.base import Locator, ParseError, ParseLimits, Section, TextBudget, require_text


def _check_encrypted(ole: olefile.OleFileIO) -> None:
    if ole.exists("EncryptionInfo") or ole.exists("EncryptedPackage"):
        raise ParseError("encrypted_file", "Password-protected Office files are not supported.")


# ==============================================================================
# Excel 97-2003 (.xls)
# ==============================================================================


class XlsParser:
    file_format = "xls"
    extensions = (".xls",)

    def inspect(self, path: Path, limits: ParseLimits) -> int | None:
        import xlrd

        try:
            wb = xlrd.open_workbook(str(path), on_demand=True)
            count = wb.nsheets
            wb.release_resources()
        except Exception:
            raise ParseError("invalid_file", "The Excel workbook could not be read. It may be damaged.") from None
        if count > limits.max_units:
            raise ParseError("too_many_sheets", f"The workbook has {count} sheets; the limit is {limits.max_units}.")
        return count

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        import xlrd

        try:
            wb = xlrd.open_workbook(str(path))
        except Exception:
            raise ParseError("invalid_file", "The Excel workbook could not be read. It may be damaged.") from None

        try:
            if wb.nsheets > limits.max_units:
                raise ParseError("too_many_sheets", f"The workbook has more than {limits.max_units} sheets.")

            budget = TextBudget(limits.max_chars * 3)
            sections: list[Section] = []
            cells_seen = 0

            for sheet_index, sheet_name in enumerate(wb.sheet_names(), start=1):
                sheet = wb.sheet_by_name(sheet_name)
                rows_text: list[str] = []
                for row_idx in range(sheet.nrows):
                    cells_seen += sheet.ncols
                    if cells_seen > limits.max_chars:
                        raise ParseError("text_too_large", "The workbook has more cells than the processing limit allows.")
                    row_vals = [str(sheet.cell_value(row_idx, col_idx)).strip() for col_idx in range(sheet.ncols)]
                    row_str = " | ".join(v for v in row_vals if v)
                    if row_str:
                        rows_text.append(f"Row {row_idx + 1}: {row_str}")

                if rows_text:
                    sheet_body = "\n".join(rows_text)
                    chunk_text = budget.take(f"Sheet '{sheet_name}':\n{sheet_body}")
                    sections.append(
                        Section(
                            chunk_text,
                            Locator("sheet", sheet_index, f"Sheet '{sheet_name}'", {"sheet": sheet_index}),
                        )
                    )
        finally:
            wb.release_resources()

        return require_text(sections)


# ==============================================================================
# Word 97-2003 (.doc)
# ==============================================================================


class DocParser:
    file_format = "doc"
    extensions = (".doc",)

    def inspect(self, path: Path, limits: ParseLimits) -> int | None:
        try:
            with olefile.OleFileIO(str(path)) as ole:
                _check_encrypted(ole)
                if not ole.exists("WordDocument"):
                    raise ParseError("invalid_file", "The file is not a valid Word 97-2003 document.")
        except ParseError:
            raise
        except Exception:
            raise ParseError("invalid_file", "The Word document could not be read. It may be damaged.") from None
        return 1

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        try:
            with olefile.OleFileIO(str(path)) as ole:
                _check_encrypted(ole)
                if not ole.exists("WordDocument"):
                    raise ParseError("invalid_file", "The file is not a valid Word 97-2003 document.")
                word_stream = ole.openstream("WordDocument").read()
                table_stream = None
                for tbl_name in ("1Table", "0Table"):
                    if ole.exists(tbl_name):
                        table_stream = ole.openstream(tbl_name).read()
                        break
        except ParseError:
            raise
        except Exception:
            raise ParseError("invalid_file", "The Word document could not be read. It may be damaged.") from None

        text_pieces = self._extract_text(word_stream, table_stream)
        raw_text = "\n\n".join(t.strip() for t in text_pieces if t.strip())
        if not raw_text:
            raise ParseError("no_text", "No readable text found in the Word document.")

        budget = TextBudget(limits.max_chars)
        full_text = budget.take(raw_text)

        # Break text into sections roughly by section_chars
        sections: list[Section] = []
        paragraphs = full_text.split("\n\n")
        current_block: list[str] = []
        current_len = 0
        section_idx = 1

        for para in paragraphs:
            para_len = len(para) + 2
            if current_block and (current_len + para_len > limits.section_chars):
                sect_text = "\n\n".join(current_block)
                sections.append(Section(sect_text, Locator("section", section_idx, f"Section {section_idx}", {"section": section_idx})))
                section_idx += 1
                current_block = [para]
                current_len = para_len
            else:
                current_block.append(para)
                current_len += para_len

        if current_block:
            sect_text = "\n\n".join(current_block)
            sections.append(Section(sect_text, Locator("section", section_idx, f"Section {section_idx}", {"section": section_idx})))

        return require_text(sections)

    @staticmethod
    def _extract_text(word_stream: bytes, table_stream: bytes | None) -> list[str]:
        if len(word_stream) >= 426 and table_stream:
            try:
                wIdent = struct.unpack_from("<H", word_stream, 0)[0]
                if wIdent == 0xA5EC:
                    fcClx, lcbClx = struct.unpack_from("<II", word_stream, 0x01A2)
                    if fcClx + lcbClx <= len(table_stream):
                        clx = table_stream[fcClx : fcClx + lcbClx]
                        pos = 0
                        while pos < len(clx):
                            clxt = clx[pos]
                            pos += 1
                            if clxt == 1:
                                cb = struct.unpack_from("<H", clx, pos)[0]
                                pos += 2 + cb
                            elif clxt == 2:
                                lcb = struct.unpack_from("<I", clx, pos)[0]
                                pos += 4
                                pcd_data = clx[pos : pos + lcb]
                                n = (lcb - 4) // (4 + 8)
                                cps = [struct.unpack_from("<I", pcd_data, i * 4)[0] for i in range(n + 1)]
                                pieces: list[str] = []
                                for i in range(n):
                                    pcd_offset = (n + 1) * 4 + i * 8
                                    fc = struct.unpack_from("<I", pcd_data, pcd_offset + 2)[0]
                                    is_compressed = bool(fc & 0x40000000)
                                    actual_fc = (fc & ~0x40000000) // 2 if is_compressed else (fc & ~0x40000000)
                                    char_len = cps[i + 1] - cps[i]
                                    if is_compressed:
                                        raw = word_stream[actual_fc : actual_fc + char_len]
                                        pieces.append(raw.decode("latin1", "replace"))
                                    else:
                                        raw = word_stream[actual_fc : actual_fc + char_len * 2]
                                        pieces.append(raw.decode("utf-16le", "replace"))
                                if pieces:
                                    return pieces
                                break
            except Exception:
                pass

        # Robust string extraction fallback from word_stream
        paragraphs: list[str] = []
        for m in re.finditer(rb"(?:[\x20-\x7e\r\n\t]\x00){4,}", word_stream):
            try:
                t = m.group(0).decode("utf-16le", "replace").strip()
                if len(t) >= 4:
                    paragraphs.append(t)
            except Exception:
                pass
        if not paragraphs:
            for m in re.finditer(rb"[\x20-\x7e\r\n\t]{6,}", word_stream):
                try:
                    t = m.group(0).decode("latin1", "replace").strip()
                    if len(t) >= 6:
                        paragraphs.append(t)
                except Exception:
                    pass
        return paragraphs


# ==============================================================================
# PowerPoint 97-2003 (.ppt)
# ==============================================================================


class PptParser:
    file_format = "ppt"
    extensions = (".ppt",)

    def inspect(self, path: Path, limits: ParseLimits) -> int | None:
        try:
            with olefile.OleFileIO(str(path)) as ole:
                _check_encrypted(ole)
                if not ole.exists("PowerPoint Document"):
                    raise ParseError("invalid_file", "The file is not a valid PowerPoint 97-2003 presentation.")
        except ParseError:
            raise
        except Exception:
            raise ParseError("invalid_file", "The presentation could not be read. It may be damaged.") from None
        return 1

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        try:
            with olefile.OleFileIO(str(path)) as ole:
                _check_encrypted(ole)
                if not ole.exists("PowerPoint Document"):
                    raise ParseError("invalid_file", "The file is not a valid PowerPoint 97-2003 presentation.")
                ppt_stream = ole.openstream("PowerPoint Document").read()
        except ParseError:
            raise
        except Exception:
            raise ParseError("invalid_file", "The presentation could not be read. It may be damaged.") from None

        records = self._extract_records(ppt_stream)
        if not records:
            raise ParseError("no_text", "No readable text found in the PowerPoint presentation.")

        budget = TextBudget(limits.max_chars)
        sections: list[Section] = []
        current_slide_text: list[str] = []
        slide_idx = 1

        for rec in records:
            current_slide_text.append(rec)
            if len("\n".join(current_slide_text)) >= limits.section_chars:
                text = budget.take("\n".join(current_slide_text))
                sections.append(Section(text, Locator("slide", slide_idx, f"Slide {slide_idx}", {"slide": slide_idx})))
                slide_idx += 1
                current_slide_text = []

        if current_slide_text:
            text = budget.take("\n".join(current_slide_text))
            sections.append(Section(text, Locator("slide", slide_idx, f"Slide {slide_idx}", {"slide": slide_idx})))

        return require_text(sections)

    @staticmethod
    def _extract_records(data: bytes) -> list[str]:
        pos = 0
        text_records: list[str] = []
        while pos + 8 <= len(data):
            rec_ver_inst, rec_type, rec_len = struct.unpack_from("<HHI", data, pos)
            pos += 8
            if pos + rec_len > len(data):
                break
            if rec_type == 4000:  # RT_TextCharsAtom (UTF-16LE)
                try:
                    txt = data[pos : pos + rec_len].decode("utf-16le", "replace").strip()
                    if txt:
                        text_records.append(txt)
                except Exception:
                    pass
            elif rec_type == 4008:  # RT_TextBytesAtom (ASCII/Latin1)
                try:
                    txt = data[pos : pos + rec_len].decode("latin1", "replace").strip()
                    if txt:
                        text_records.append(txt)
                except Exception:
                    pass
            pos += rec_len
        return text_records
