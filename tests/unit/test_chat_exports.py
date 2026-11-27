import io
import uuid

import pytest
from fastapi import HTTPException

from app.services.chat_exports import create_chat_export, resolve_chat_export


@pytest.mark.parametrize("file_format", ["pptx", "docx", "xlsx", "pdf"])
def test_chat_exports_are_real_files_with_owner_bound_downloads(tmp_path, file_format):
    owner = uuid.uuid4()
    key = b"chat-export-test-key"
    content = "# CodeLab November content plan\n\n## Post 3\n- Show the delivery charge before payment. [S1]"

    token, filename = create_chat_export(content, file_format, tmp_path, owner, key)
    path = resolve_chat_export(token, file_format, tmp_path, owner, key)
    body = path.read_bytes()
    assert filename.endswith(f".{file_format}")

    if file_format == "pdf":
        assert body.startswith(b"%PDF-")
    else:
        assert body.startswith(b"PK\x03\x04")
    if file_format == "pptx":
        from pptx import Presentation

        presentation = Presentation(io.BytesIO(body))
        text = " ".join(shape.text for slide in presentation.slides for shape in slide.shapes if shape.has_text_frame)
        assert "CodeLab November content plan" in text and "Post 3" in text
    if file_format == "docx":
        from docx import Document

        document = Document(io.BytesIO(body))
        text = " ".join(paragraph.text for paragraph in document.paragraphs)
        assert "CodeLab November content plan" in text and "Post 3" in text
    if file_format == "xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(body), data_only=True)
        text = " ".join(str(cell.value or "") for row in workbook.active.iter_rows() for cell in row)
        assert "CodeLab November content plan" in text and "Post 3" in text

    with pytest.raises(HTTPException) as error:
        resolve_chat_export(token, file_format, tmp_path, uuid.uuid4(), key)
    assert error.value.status_code == 404
