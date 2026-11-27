"""The gate's golden corpus: synthetic Harborline Facilities documents, users and permissions.

Not a customer collection. The files cover every supported format (PDF, DOCX, XLSX, PPTX, TXT, CSV) and
English, Urdu and Roman Urdu, with near-miss documents that are on the topic but lack the fact, a withdrawn
draft that conflicts with the current policy, one private and one group-restricted document, and an IT memo
whose facts sit in different passages of one .docx (all of them share one citation number).

`seed` uploads them through the production `DocumentService` and processes them with the production worker,
so parsing, chunking, embedding and permissions are the real ones. The cases that use this corpus are in
`evals/golden.json`.
"""

from __future__ import annotations

import textwrap
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import UploadFile

from app.core.config import Settings
from app.core.security import hash_password
from app.db.models import ROLE_ADMIN, ROLE_USER, Document, User
from app.rag.components import RagComponents
from app.rag.documents import DocumentService
from app.services.access_control import AccessAdminService
from app.services.audit import AuditContext

PERSONAS = {
    "admin": "gate-admin@example.com",
    "owner": "gate-owner@example.com",
    "member": "gate-member@example.com",
    "outsider": "gate-outsider@example.com",
}
_PASSWORD = "gate-harborline-not-a-real-account"
_FILLER = "Shift logs are filed in the operations cabinet. "
_PRIVATE = {"warehouse-access.txt": "owner"}  # uploaded by that persona, readable by no one else
_RESTRICTED = {"rate-card.txt"}  # readable by the Harborline billing group (the member persona) only


class GoldenCorpusError(RuntimeError):
    pass


async def seed(sessions: async_sessionmaker[AsyncSession], settings: Settings, rag: RagComponents) -> int:
    """Create the personas and upload and process every file. Returns the number of documents."""
    files = corpus()
    ctx = AuditContext(None, "evaluation-gate", None, None)
    password_hash = hash_password(_PASSWORD)
    async with sessions() as db:
        for name, email in PERSONAS.items():
            db.add(User(email=email, role=ROLE_ADMIN if name == "admin" else ROLE_USER, password_hash=password_hash))
        await db.commit()
        admin = await _user(db, "admin")
        access = AccessAdminService(db, admin, ctx)
        group = await access.create_group("Harborline billing", "project", None)
        await access.add_member(group.id, PERSONAS["member"])
        group_id = group.id

    async def upload(persona: str, filename: str, visibility: str):
        async with sessions() as db:
            service = DocumentService(db, settings, rag.storage, rag.indexer, await _user(db, persona), ctx)
            doc = await service.upload(UploadFile(filename=filename, file=BytesIO(files[filename])), visibility)
            return doc.id

    for filename in files:
        if filename in _PRIVATE or filename in _RESTRICTED:
            continue
        await upload("admin", filename, "shared")
    for filename, persona in _PRIVATE.items():
        await upload(persona, filename, "private")
    for filename in _RESTRICTED:
        doc_id = await upload("admin", filename, "private")
        async with sessions() as db:
            service = DocumentService(db, settings, rag.storage, rag.indexer, await _user(db, "admin"), ctx)
            await service.set_access(doc_id, "restricted", [], [group_id])

    while await rag.worker.process_next():
        pass
    async with sessions() as db:
        rows = (await db.execute(select(Document.filename, Document.status, Document.error_code))).all()
    failed = [f"{row.filename} ({row.status} {row.error_code or ''})".strip() for row in rows if row.status != "ready"]
    if failed:
        raise GoldenCorpusError(f"Golden documents did not become ready: {', '.join(failed)}")
    return len(rows)


async def _user(db: AsyncSession, persona: str) -> User:
    return (await db.execute(select(User).where(User.email == PERSONAS[persona]))).scalar_one()


def corpus() -> dict[str, bytes]:
    texts = {
        "incident-runbook.txt": (
            "Lahore network runbook\n"
            "The Lahore core switch maintenance window is Sunday 02:00 to 04:00 PKT.\n"
            "The approved change ticket is CHG-4418.\n"
            "This runbook does not list a desk extension or a bank account.\n"
        ),
        "network-faq.txt": (
            "Network desk FAQ\n"
            "Core switches are maintained on a weekend window.\n"
            "Ask the network desk for the current ticket number.\n"
            "This FAQ does not name the Lahore window or a change ticket.\n"
        ),
        "leave-policy.txt": (
            "Annual leave policy\n"
            "Annual leave for permanent staff is 18 working days under the current policy.\n"
            "Carry-over is limited to 5 days.\n"
        ),
        "leave-draft.txt": (
            "Withdrawn leave draft\n"
            "This draft is withdrawn and is not the current policy.\n"
            "It proposed annual leave of 12 working days.\n"
            "Use the annual leave policy for the approved number.\n"
        ),
        "refund-policy.txt": (
            "Billing refund policy\n"
            "The billing refund window is thirty days for invoice disputes.\n"
            "Customers quote refund code REFUND-30.\n"
        ),
        "refund-notes.txt": (
            "Billing notes\n"
            "Staff discuss the billing refund window during invoice disputes.\n"
            "These notes do not state the number of days and do not include a refund code.\n"
        ),
        "casual-leave-roman.txt": "Casual leave note\nCasual leave sirf 8 din hai.\nCasual leave code CL-8 hai.\n",
        "refund-roman.txt": (
            "Roman Urdu billing note\n"
            "Billing refund window tees din hai invoice disputes ke liye.\n"
            "Roman refund code ROMAN-30 hai.\n"
        ),
        "warehouse-urdu.txt": "گودام کی رات کی شفٹ\nگودام کے دروازے کا کوڈ سلور میپل ہے۔\nیہ کوڈ صرف رات کی شفٹ کے لیے ہے۔\n",
        "site-manual.txt": (
            "Site manual\n"
            + _FILLER * 30
            + "\nThe rejected draft listed a standby generator fuel capacity of 180 litres. That draft is rejected.\n"
            + _FILLER * 30
            + "\nThe approved standby generator fuel capacity is 340 litres.\nThe annual inspection month is March.\n"
            + _FILLER * 30
        ),
        "safety-faq.txt": (
            "Generator FAQ\n"
            "Standby generators have a fuel capacity recorded in the site manual.\n"
            "This FAQ does not state the litres or the inspection month.\n"
        ),
        "rate-card.txt": (
            "Restricted billing rate card\n"
            "The restricted billing rate for priority support is PKR 4500 per hour.\n"
            "Rate card code RATE-4500.\n"
        ),
        "support-faq.txt": (
            "Priority support FAQ\n"
            "Priority support is billed hourly.\n"
            "Ask the billing group for the current rate card.\n"
            "This FAQ does not state an hourly amount or a rate code.\n"
        ),
        "warehouse-access.txt": (
            "Warehouse night shift access\n"
            "The warehouse door code is SILVERMAPLE for the night shift.\n"
            "Guards do not copy the code into general notes.\n"
        ),
        "warehouse-notes.txt": (
            "Warehouse night shift notes\n"
            "The warehouse night shift follows a door procedure and signs a paper log.\n"
            "This note does not contain the door code.\n"
        ),
    }
    files = {name: body.encode("utf-8") for name, body in texts.items()}
    files["patch-schedule.csv"] = (
        "Host,Owner,Patch window,Code\ndb01,platform team,Sunday 02:00,DB-01\nweb01,web team,Saturday 23:00,WEB-01\n"
    ).encode()
    files["handbook.pdf"] = _pdf(
        "Harborline Facilities handbook. This cover page names no operational figure.",
        "VPN tokens are rotated every 90 days by security operations. Rotation code SEC-90.",
        "The meal expense cap is PKR 1800 per person. Meal code MEAL-1800.",
        _FILLER * 12,
        _FILLER * 12,
    )
    files["vendor-contract.docx"] = _vendor_contract()
    files["it-memo.docx"] = _it_memo()
    files["asset-register.xlsx"] = _xlsx()
    files["quarterly-review.pptx"] = _pptx()
    return files


def _pdf(*pages: str) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
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
        lines = [wrapped for paragraph in text.split("\n") for wrapped in (textwrap.wrap(paragraph, 90) or [""])]
        ops = ["BT", "/F1 10 Tf", "12 TL", "50 790 Td", *(f"({line}) Tj T*" for line in lines), "ET"]
        stream = DecodedStreamObject()
        stream.set_data("\n".join(ops).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _vendor_contract() -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph("Harborline vendor contract. The notice period is 60 days. Contract code VC-2260.")
    document.add_paragraph("Payment terms are stated as net 45. Payment code PAY-45.")
    document.add_heading("Laptop refresh", level=1)
    table = document.add_table(rows=2, cols=2)
    for row, values in zip(table.rows, (("Model", "Refresh cycle"), ("Contoso Book 14", "four years")), strict=True):
        for cell, value in zip(row.cells, values, strict=True):
            cell.text = value
    out = BytesIO()
    document.save(out)
    return out.getvalue()


def _it_memo() -> bytes:
    """One memo, no headings: the facts land in separate passages that share the document's one citation."""
    import docx

    document = docx.Document()
    document.add_paragraph(
        "INTER OFFICE MEMO. From: IT Department. To: All Department Heads and Employees. "
        "Subject: Domain joining and network drive mapping."
    )
    document.add_paragraph(_FILLER * 10)
    document.add_paragraph(
        "All official laptops will be joined to the HARBOR domain by 30 November for central sign-in. "
        "Domain join code DOM-30."
    )
    document.add_paragraph(_FILLER * 10)
    document.add_paragraph(
        "Every department gets a mapped network drive for shared files, assigned by management approval. "
        "Network drive code NET-77."
    )
    document.add_paragraph(_FILLER * 10)
    document.add_paragraph("Save open work when IT staff ask, because the laptop restarts after joining the domain.")
    out = BytesIO()
    document.save(out)
    return out.getvalue()


def _xlsx() -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook()
    inventory = workbook.active
    inventory.title = "Inventory"
    inventory.append(["Asset", "Location", "Units", "Code"])
    inventory.append(["UPS battery", "Hall B", 40, "UPS-B40"])
    inventory.append(["Cooling unit", "Hall A", 12, "CHL-A12"])
    budget = workbook.create_sheet("Budget")
    budget.append(["Item", "Amount"])
    budget.append(["Chiller replacement", "PKR 125000"])
    out = BytesIO()
    workbook.save(out)
    return out.getvalue()


def _pptx() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Datacenter cooling"
    slide.placeholders[1].text = "Hall A chillers run at 60 percent capacity."
    table_slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    table_slide.shapes.title.text = "Support hours"
    table = table_slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(6), Inches(1)).table
    for (row, col), value in {(0, 0): "Region", (0, 1): "Hours", (1, 0): "EMEA", (1, 1): "07:00 to 19:00"}.items():
        table.cell(row, col).text = value
    out = BytesIO()
    presentation.save(out)
    return out.getvalue()
