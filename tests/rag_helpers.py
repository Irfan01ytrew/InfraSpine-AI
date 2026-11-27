"""Synthetic PDFs and workflow helpers for RAG tests. All document content is invented."""

import io
import textwrap

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.main import app
from tests.conftest import ADMIN_EMAIL, VALID_KEY_1, login

PROVIDER = "/api/admin/providers/deepseek"


def _escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _write(writer: PdfWriter, *pages: str) -> None:
    """Add A4 pages with Helvetica 10pt text (wrapped), using only pypdf."""
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for text in pages:
        page = writer.add_blank_page(595, 842)
        if not text:
            continue
        lines = [w for paragraph in text.split("\n") for w in (textwrap.wrap(paragraph, 95) or [""])]
        ops = ["BT", "/F1 10 Tf", "12 TL", "50 790 Td", *(f"({_escape(line)}) Tj T*" for line in lines), "ET"]
        stream = DecodedStreamObject()
        stream.set_data("\n".join(ops).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )


def make_pdf(*pages: str) -> bytes:
    writer = PdfWriter()
    _write(writer, *pages)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_pdf_with_chart(intro: str, chart_page_text: str) -> bytes:
    """Two pages: text only, then text with an embedded raster bar chart (a real image XObject)."""
    import zlib

    from PIL import Image, ImageDraw, ImageFont
    from pypdf.generic import NumberObject, StreamObject

    chart = Image.new("RGB", (800, 500), "white")
    draw = ImageDraw.Draw(chart)
    font = ImageFont.load_default(size=22)
    draw.text((220, 15), "Quarterly revenue (PKR million)", fill="black", font=font)
    draw.line((80, 440, 760, 440), fill="black", width=3)
    draw.line((80, 60, 80, 440), fill="black", width=3)
    for i, (quarter, value) in enumerate((("Q1", 2.1), ("Q2", 3.4), ("Q3", 4.2), ("Q4", 3.8))):
        x = 130 + i * 160
        draw.rectangle((x, 440 - int(value * 80), x + 90, 440), fill=(40, 90, 200))
        draw.text((x + 25, 450), quarter, fill="black", font=font)
        draw.text((x + 20, 410 - int(value * 80)), str(value), fill="black", font=font)

    writer = PdfWriter()
    _write(writer, intro, "")
    image = StreamObject()
    image._data = zlib.compress(chart.tobytes())
    image.update({
        NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(chart.width), NameObject("/Height"): NumberObject(chart.height),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8),
        NameObject("/Filter"): NameObject("/FlateDecode"),
    })
    page = writer.pages[1]
    font_resources = writer.pages[0]["/Resources"]["/Font"]
    stream = DecodedStreamObject()
    stream.set_data(
        f"BT /F1 10 Tf 50 790 Td ({_escape(chart_page_text)}) Tj ET\nq 480 0 0 300 57 420 cm /Im1 Do Q".encode("latin-1")
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): font_resources,
        NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): writer._add_object(image)}),
    })
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_pdf_with_table(
    title: str,
    columns: list[str],
    rows: list[list[str]],
    rows_per_page: int | None = None,
    repeat_header: bool = True,
    trailing_pages: int = 0,
) -> bytes:
    """A landscape PDF with a ruled table (a real grid, so Docling's table model finds it) under a title.
    `rows_per_page` continues the table on the next pages (with the header repeated or not); `trailing_pages`
    adds pages holding only a footer, like a CRM export printed with spare pages."""
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    fonts = {
        name: writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject(base),
        }))
        for name, base in (("/F1", "/Helvetica"), ("/F2", "/Helvetica-Bold"))
    }
    width, height, left, row_height = 842, 595, 30, 18
    column_width = (width - 2 * left) / len(columns)
    per_page = rows_per_page or len(rows)
    chunks = [rows[i : i + per_page] for i in range(0, len(rows), per_page)] or [[]]

    def cell_ops(x: float, y: float, text: str, font: str) -> list[str]:
        return [f"{x:.1f} {y:.1f} {column_width:.1f} {row_height} re S",
                f"BT {font} 7 Tf {x + 3:.1f} {y + 6:.1f} Td ({_escape(text)}) Tj ET"]

    pages = []
    for number, chunk in enumerate(chunks):
        ops = ["0.5 w"]
        top = height - 40
        if number == 0:
            ops.append(f"BT /F2 16 Tf {left} {top} Td ({_escape(title)}) Tj ET")
            top -= 30
        lines = ([columns] if number == 0 or repeat_header else []) + chunk
        for r, values in enumerate(lines):
            y = top - (r + 1) * row_height
            font = "/F2" if r == 0 and (number == 0 or repeat_header) else "/F1"
            for c, value in enumerate(values):
                ops += cell_ops(left + c * column_width, y, value, font)
        pages.append(ops)
    for _ in range(trailing_pages):
        pages.append([f"BT /F1 8 Tf {width - 90} 20 Td (Page 1 of 1) Tj ET"])
    for ops in pages:
        page = writer.add_blank_page(width, height)
        stream = DecodedStreamObject()
        stream.set_data("\n".join(ops).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject(name): ref for name, ref in fonts.items()})}
        )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


LEAD_COLUMNS = ["ID", "Name", "Phone", "Source", "Project", "Stage", "Status", "Budget (PKR)", "City", "Assigned To"]


def synthetic_leads(count: int = 20, first_id: int = 137) -> list[list[str]]:
    """Invented CRM leads shaped like the export the WP1 defect was found on (newest first, some blank cells)."""
    sources, stages = ("Facebook", "Walk-in", "Website", "Referral"), ("New", "Contacted", "Site visit", "Negotiation")
    cities, agents = ("Lahore", "Faisalabad", "Arifwala"), ("Agent A", "Agent B")
    leads = []
    for n in range(count - 1, -1, -1):
        lead_id = first_id + n
        leads.append([
            str(lead_id), f"Test Lead {lead_id}", f"0300-0000{lead_id:03d}", sources[n % 4], "Project Alpha",
            stages[n % 4], "active" if n % 3 else "closed", f"{(n + 1) * 250_000:,}" if n % 5 else "",
            cities[n % 3], agents[n % 2],
        ])
    return leads


def make_encrypted_pdf(text: str = "Confidential synthetic text.") -> bytes:
    writer = PdfWriter()
    _write(writer, text)
    writer.encrypt(user_password="user-secret", owner_password="owner-secret", algorithm="AES-256")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def upload(client: TestClient, filename: str, data: bytes, visibility: str = "private", content_type="application/pdf"):
    return client.post(
        "/api/rag/documents", files={"file": (filename, data, content_type)}, data={"visibility": visibility}
    )


def ready(client: TestClient, filename: str, data: bytes, **kwargs) -> dict:
    """Upload a synthetic document and process it through the test worker."""
    response = upload(client, filename, data, **kwargs)
    assert response.status_code == 202, response.text
    process_all(client)
    result = document(client, response.json()["id"])
    assert result["status"] == "ready", result
    return result


def replace_file(client: TestClient, document_id: str, filename: str, data: bytes):
    return client.put(f"/api/rag/documents/{document_id}/file", files={"file": (filename, data, "application/pdf")})


def process_all(client: TestClient) -> int:
    """Run the ingestion worker in the app's event loop until the queue is empty."""
    processed = 0
    while client.portal.call(app.state.rag.worker.process_next):
        processed += 1
    return processed


def process_one(client: TestClient) -> bool:
    return client.portal.call(app.state.rag.worker.process_next)


def document(client: TestClient, document_id: str) -> dict:
    return client.get(f"/api/rag/documents/{document_id}").json()


def ask(client: TestClient, question: str, document_ids: list[str] | None = None):
    body = {"question": question}
    if document_ids is not None:
        body["document_ids"] = document_ids
    return client.post("/api/rag/chat", json=body)


def activate_provider(client: TestClient) -> None:
    """Configure + activate the (fake) DeepSeek provider as admin. Leaves the admin signed in."""
    login(client, ADMIN_EMAIL)
    version = client.get(PROVIDER).json()["version"]
    assert client.put(PROVIDER, json={"model": "deepseek-flash", "settings": {}, "expected_version": version}).status_code == 200
    version = client.get(PROVIDER).json()["version"]
    assert client.post(f"{PROVIDER}/credentials", json={"api_key": VALID_KEY_1, "expected_version": version}).status_code == 200
    version = client.get(PROVIDER).json()["version"]
    assert client.post(f"{PROVIDER}/activate", json={"expected_version": version}).status_code == 200


def allow_images(client: TestClient, authorized: bool = True):
    """As admin: authorize (or withdraw) sending document images to the (fake) DeepSeek provider."""
    version = client.get(PROVIDER).json()["version"]
    return client.put(f"{PROVIDER}/image-sharing", json={"authorized": authorized, "expected_version": version})


def make_chart_image(file_format: str = "PNG", size: tuple[int, int] = (800, 500)) -> bytes:
    """A synthetic bar chart as a PNG or JPEG file."""
    from PIL import Image, ImageDraw

    chart = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(chart)
    for i, value in enumerate((2.1, 3.4, 4.2, 3.8)):
        x = 60 + i * (size[0] - 100) // 4
        draw.rectangle((x, size[1] - 40 - int(value * 80), x + 60, size[1] - 40), fill=(40, 90, 200))
    buffer = io.BytesIO()
    chart.save(buffer, format=file_format)
    return buffer.getvalue()


def vector_ids() -> set[int]:
    return app.state.rag.vector_store.ids()
