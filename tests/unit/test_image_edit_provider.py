import asyncio
import base64
import io

import httpx2
from PIL import Image

from app.providers.openai_compatible import OpenAICompatibleClient
from app.services.llm import ClientParams


def test_gemini_image_edit_sends_the_source_image_and_returns_the_generated_image():
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "#20756b").save(output, "PNG")
    png = output.getvalue()
    encoded = base64.b64encode(png).decode("ascii")
    requests = []

    def handler(request):
        import json

        requests.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": encoded}}]}}]
        })

    async def exercise():
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        client = OpenAICompatibleClient(
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            ClientParams("test-key", "gemini-3.1-flash", {}, 10, 0),
            http_client=http,
        )
        try:
            return await client.edit_image(png, "Replace the background with a blue sky")
        finally:
            await client.close()

    image, mime_type, model, provider = asyncio.run(exercise())
    assert image == png and mime_type == "image/png"
    assert model == "gemini-3.1-flash-image" and provider == "gemini"
    payload = requests[0]
    assert payload["contents"][0]["parts"][0]["inlineData"] == {"mimeType": "image/png", "data": encoded}
    assert payload["contents"][0]["parts"][1]["text"] == "Replace the background with a blue sky"
    assert payload["generationConfig"]["responseModalities"] == ["IMAGE"]
