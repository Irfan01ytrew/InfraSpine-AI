import uuid

import pytest
from fastapi import HTTPException
from PIL import Image

from app.services.chat_images import create_chat_image, latest_chat_image_token, read_chat_image


def _png() -> bytes:
    import io

    data = io.BytesIO()
    Image.new("RGB", (2, 2), "#20756b").save(data, "PNG")
    return data.getvalue()


def test_previous_chat_image_can_be_loaded_only_by_its_owner(tmp_path):
    owner = uuid.uuid4()
    other_user = uuid.uuid4()
    key = b"unit-test-signing-key"
    token = create_chat_image(_png(), tmp_path, owner, key)
    answer = f"![Generated image](/api/chat-images/{token})\n\n[Download Image](/api/chat-images/{token}?download=true)"

    assert latest_chat_image_token(answer) == token
    assert read_chat_image(token, tmp_path, owner, key).startswith(b"\x89PNG")
    with pytest.raises(HTTPException) as error:
        read_chat_image(token, tmp_path, other_user, key)
    assert error.value.status_code == 404


def test_chat_image_token_is_not_extracted_from_malformed_or_missing_content():
    assert latest_chat_image_token("No image in this reply") is None
    assert latest_chat_image_token("/api/chat-images/not-a-signed-token") is None
