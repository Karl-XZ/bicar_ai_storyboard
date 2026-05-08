import asyncio
import base64

import httpx

from app.core.config import settings
from app.providers.openai_image import OpenAIImageProvider


class FakeAsyncClient:
    def __init__(self, *, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        assert url.endswith("/v1/images/generations")
        assert json["model"] == "gpt-image-2"
        assert json["size"] == "1280x720"
        assert "response_format" not in json
        return httpx.Response(
            200,
            json={"data": [{"id": "img_123", "b64_json": base64.b64encode(b"png-bytes").decode("ascii")}]},
            request=httpx.Request("POST", url),
        )


def test_openai_image_provider_uses_current_api_shape(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")
    monkeypatch.setattr("app.providers.openai_image.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        OpenAIImageProvider().generate_image(
            {"model": "gpt-image-2", "prompt": "a yellow toy car", "size": "1280*720"}
        )
    )

    assert result.bytes_data == b"png-bytes"
    assert result.mime_type == "image/png"
    assert result.provider_asset_id == "img_123"
