"""Create a one-page synthetic source file for the native local review acceptance."""

from __future__ import annotations

import os
from pathlib import Path

from reportlab.pdfgen import canvas


output_path = Path(os.environ["INFRASPINE_LOCAL_REVIEW_PDF"]).resolve()
if output_path.exists():
    raise SystemExit("Refusing to overwrite an existing local review PDF.")
output_path.parent.mkdir(parents=True, exist_ok=True)

document = canvas.Canvas(str(output_path), pagesize=(612, 792), pageCompression=0)
document.setTitle("Synthetic InfraSpine local review evidence")
document.setFont("Helvetica", 16)
document.drawString(60, 720, "Synthetic local review evidence")
document.setFont("Helvetica", 12)
document.drawString(60, 680, "Service target: ticket F12-4817 has a response target of 48 hours.")
document.drawString(60, 650, "Invoice Number: INV-204")
document.drawString(60, 625, "Invoice Date: 30/09/2026")
document.drawString(60, 600, "Vendor: North Star Supplies")
document.drawString(60, 575, "Subtotal: USD 20.00")
document.drawString(60, 550, "Tax: USD 0.00")
document.drawString(60, 525, "Grand Total: USD 20.00")
document.drawString(60, 495, "This page contains synthetic acceptance data only.")
document.showPage()
document.save()
