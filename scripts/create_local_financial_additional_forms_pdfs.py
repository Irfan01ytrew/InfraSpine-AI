"""Create synthetic purchase-order and generic-form PDFs for focused review acceptance."""

from __future__ import annotations

import os
from pathlib import Path

from reportlab.pdfgen import canvas


def make_pdf(path: Path, title: str, lines: tuple[tuple[int, str], ...]) -> None:
    if path.exists():
        raise SystemExit("Refusing to overwrite an existing synthetic financial PDF.")
    path.parent.mkdir(parents=True, exist_ok=True)
    document = canvas.Canvas(str(path), pagesize=(612, 792), pageCompression=0)
    document.setTitle(title)
    document.setFont("Helvetica-Bold", 16)
    document.drawString(60, 730, title)
    document.setFont("Helvetica", 12)
    for y, text in lines:
        document.drawString(60, y, text)
    document.drawString(60, 520, "Synthetic QA data only; no customer or accounting data.")
    document.showPage()
    document.save()


purchase_order = Path(os.environ["INFRASPINE_FINANCIAL_PO_PDF"]).resolve()
generic_form = Path(os.environ["INFRASPINE_FINANCIAL_GENERIC_FORM_PDF"]).resolve()
make_pdf(purchase_order, "SYNTHETIC PURCHASE ORDER", (
    (690, "Purchase Order Number: PO-4421"),
    (665, "Purchase Order Date: 27/10/2026"),
    (640, "Vendor: North Star Supplies"),
    (615, "Currency: USD"),
    (590, "Subtotal: 100.00"),
    (565, "Tax: 5.00"),
    (540, "Order Total: 106.00"),
))
make_pdf(generic_form, "SYNTHETIC EQUIPMENT REQUEST FORM", (
    (690, "Request ID: REQ-2238"),
    (665, "Requested By: QA Employee"),
    (640, "Department: Laboratory QA"),
    (615, "Equipment: Safety gloves"),
    (590, "Quantity: 12"),
    (565, "Justification: Replace worn gloves"),
))
