#!/usr/bin/env python3
"""Create a one-page source for isolated F17 policy-impact browser acceptance."""
from __future__ import annotations

import os
import io
import textwrap
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def make_policy_pdf(text: str) -> bytes:
    """Build a one-page synthetic PDF without importing the pytest app fixtures."""
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    page = writer.add_blank_page(595, 842)
    lines = [line for paragraph in text.split("\n") for line in (textwrap.wrap(paragraph, 95) or [""])]
    escaped = (line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines)
    operations = ["BT", "/F1 10 Tf", "12 TL", "50 790 Td", *(f"({line}) Tj T*" for line in escaped), "ET"]
    stream = DecodedStreamObject()
    stream.set_data("\n".join(operations).encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
    })
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def main() -> None:
    output = Path(os.environ["INFRASPINE_F17_POLICY_PDF"]).resolve()
    if not output.name.startswith("f17-policy-source-") or output.suffix.lower() != ".pdf":
        raise SystemExit("F17 output must use the guarded synthetic-policy filename.")
    if output.exists():
        raise SystemExit("Refusing to overwrite an existing synthetic F17 source PDF.")
    output.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "F17 Synthetic Access Change Policy\n"
        "Access changes require review within 14 days.\n"
        "This document is a synthetic source for isolated local acceptance only."
    )
    with output.open("xb") as stream:
        stream.write(make_policy_pdf(body))
    print("F17_SYNTHETIC_POLICY_SOURCE_CREATED=PASS")


if __name__ == "__main__":
    main()
