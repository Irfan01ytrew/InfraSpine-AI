from pathlib import Path

import pytest

from app.rag.parsers.base import ParseLimits
from app.rag.parsers.image_parser import PngParser


def test_local_ocr_extracts_citable_text_from_synthetic_image(tmp_path: Path):
    pytest.importorskip("rapidocr")
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    ImageFont = pytest.importorskip("PIL.ImageFont")

    image = Image.new("RGB", (1500, 520), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 76)
    except OSError:
        font = ImageFont.truetype("DejaVuSans.ttf", 76)
    draw.text((80, 80), "SYNTHETIC INVOICE INV-4821", fill="black", font=font)
    draw.text((80, 230), "TOTAL USD 1250.75", fill="black", font=font)
    path = tmp_path / "synthetic-invoice.png"
    image.save(path)

    sections = PngParser().parse(path, ParseLimits(10, 4000, 2_000_000, 1000))

    assert len(sections) == 1
    assert "1250.75" in sections[0].text
    assert sections[0].locator.to_json() == {
        "kind": "image",
        "position": 1,
        "label": "Image",
        "image": 1,
    }
