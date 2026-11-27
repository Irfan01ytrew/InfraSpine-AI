"""Create a one-page synthetic invoice for the native financial-review browser acceptance."""

from __future__ import annotations

import os
from pathlib import Path

from reportlab.pdfgen import canvas


output_path = Path(os.environ["INFRASPINE_FINANCIAL_REVIEW_PDF"]).resolve()
if output_path.exists():
    raise SystemExit("Refusing to overwrite an existing synthetic invoice PDF.")
output_path.parent.mkdir(parents=True, exist_ok=True)

document = canvas.Canvas(str(output_path), pagesize=(612, 792), pageCompression=0)
document.setTitle("Synthetic financial form review")
document.setFont("Helvetica-Bold", 16)
document.drawString(60, 730, "TAX INVOICE")
document.setFont("Helvetica", 12)
for y, text in (
    (690, "Invoice Number: INV-9077"),
    (665, "Invoice Date: 27/10/2026"),
    (640, "Vendor: North Star Supplies"),
    (615, "Subtotal: USD 25.50"),
    (590, "Tax: USD 0.00"),
    (565, "Grand Total: USD 25.50"),
    (520, "Synthetic QA data only; no real customer or accounting data."),
):
    document.drawString(60, y, text)
document.showPage()
document.save()
