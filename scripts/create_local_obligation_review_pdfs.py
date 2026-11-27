"""Create a synthetic contract pair for current-source obligation comparison acceptance."""

from __future__ import annotations

import os
from pathlib import Path

from reportlab.pdfgen import canvas


def make_pdf(path: Path, delivery_days: int) -> None:
    if path.exists():
        raise SystemExit("Refusing to overwrite an existing synthetic contract PDF.")
    path.parent.mkdir(parents=True, exist_ok=True)
    document = canvas.Canvas(str(path), pagesize=(612, 792), pageCompression=0)
    document.setTitle("Synthetic obligation comparison review")
    document.setFont("Helvetica-Bold", 16)
    document.drawString(60, 730, "SYNTHETIC SUPPLY TERMS")
    document.setFont("Helvetica", 12)
    document.drawString(60, 690, f"Supplier shall deliver replacement parts within {delivery_days} days after written request.")
    document.drawString(60, 650, "Supplier shall keep goods insured while they are in transit.")
    document.drawString(60, 590, "Synthetic QA only; not a real contract or legal advice.")
    document.showPage()
    document.save()


first = Path(os.environ["INFRASPINE_OBLIGATION_REVIEW_V1_PDF"]).resolve()
second = Path(os.environ["INFRASPINE_OBLIGATION_REVIEW_V2_PDF"]).resolve()
make_pdf(first, 30)
make_pdf(second, 10)
