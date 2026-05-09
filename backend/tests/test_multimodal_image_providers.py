import asyncio
import base64

import httpx

from app.core.config import settings
from app.providers.google_image import GoogleNanoBanana2Provider
from app.providers.openrouter_image import OpenRouterImageProvider


class FakeGoogleAsyncClient:
    def __init__(self, *, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None):
        parts = json["contents"][0]["parts"]
        assert parts[0]["text"] == "把这张图改成胶片海报风"
        assert parts[1]["inlineData"]["mimeType"] == "image/png"
        assert base64.b64decode(parts[1]["inlineData"]["data"]) == b"ref-image"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(b"out-image").decode("ascii"),
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
            request=httpx.Request("POST", url),
        )


class FakeOpenRouterAsyncClient:
    def __init__(self, *, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        content = json["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "把这张图改成赛博朋克风"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [
                                {
                                    "image_url": {
                                        "url": "data:image/png;base64," + base64.b64encode(b"out-image").decode("ascii")
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
            request=httpx.Request("POST", url),
        )


def test_google_provider_accepts_reference_images(monkeypatch, tmp_path):
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"ref-image")

    monkeypatch.setattr(settings, "google_api_key", "test-google-key")
    monkeypatch.setattr("app.providers.google_image.httpx.AsyncClient", FakeGoogleAsyncClient)

    result = asyncio.run(
        GoogleNanoBanana2Provider().generate_image(
            {
                "prompt": "把这张图改成胶片海报风",
                "reference_images": [f"file://{reference}"],
            }
        )
    )

    assert result.bytes_data == b"out-image"
    assert result.mime_type == "image/png"


def test_openrouter_provider_accepts_reference_images(monkeypatch, tmp_path):
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"ref-image")

    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr("app.providers.openrouter_image.httpx.AsyncClient", FakeOpenRouterAsyncClient)

    result = asyncio.run(
        OpenRouterImageProvider().generate_image(
            {
                "model": "nano_banana_2",
                "prompt": "把这张图改成赛博朋克风",
                "reference_images": [f"file://{reference}"],
            }
        )
    )

    assert result.bytes_data == b"out-image"
    assert result.mime_type == "image/png"


def test_openrouter_provider_maps_gpt_image_alias(monkeypatch, tmp_path):
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"ref-image")

    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr("app.providers.openrouter_image.httpx.AsyncClient", FakeOpenRouterAsyncClient)

    result = asyncio.run(
        OpenRouterImageProvider().generate_image(
            {
                "model": "gpt_image_2",
                "prompt": "把这张图改成赛博朋克风",
                "reference_images": [f"file://{reference}"],
            }
        )
    )

    assert result.bytes_data == b"out-image"
    assert result.mime_type == "image/png"
