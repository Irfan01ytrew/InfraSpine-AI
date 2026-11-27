"""Create synthetic partner PDF/Word fixtures in a new local acceptance directory."""
import argparse
import io
from pathlib import Path

import docx
import openpyxl
from pptx import Presentation
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def pdf_bytes(*pages: str) -> bytes:
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for value in pages:
        page = writer.add_blank_page(595, 842)
        escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 50 790 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    target = parser.parse_args().directory.resolve()
    target.mkdir(parents=True, exist_ok=True)
    word = docx.Document()
    word.add_heading("Cedar support policy", level=1)
    word.add_paragraph("Cedar support responds within 17 business hours.")
    buffer = io.BytesIO()
    word.save(buffer)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Dispatch"
    sheet.append(["Service", "Window"])
    sheet.append(["Copperpine dispatch", "21:35 UTC"])
    sheet_bytes = io.BytesIO()
    workbook.save(sheet_bytes)
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Lagoon renewal"
    slide.placeholders[1].text = "Lagoon renewal quota is 42 units per quarter."
    slide_bytes = io.BytesIO()
    presentation.save(slide_bytes)
    files = {"quartz.pdf": pdf_bytes("Quartz field service overview.", "Quartz dispatch opens at 09:40 UTC."),
             "beta.pdf": pdf_bytes("Beta private reference is VEGA-7261."), "cedar.docx": buffer.getvalue(),
             "dispatch.xlsx": sheet_bytes.getvalue(), "renewal.pptx": slide_bytes.getvalue()}
    for name, data in files.items():
        with (target / name).open("xb") as out:
            out.write(data)
    print("F12_SYNTHETIC_DOCUMENTS_CREATED=5")


if __name__ == "__main__":
    main()
