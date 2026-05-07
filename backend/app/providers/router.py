from app.core.config import settings
from app.providers.base import ImageProvider, TextProvider, VideoProvider
from app.providers.dashscope import DashScopeImageProvider, DashScopeTextProvider, DashScopeVideoProvider
from app.providers.google_image import GoogleNanoBanana2Provider
from app.providers.mock import MockImageProvider, MockTextProvider, MockVideoProvider
from app.providers.openai_image import OpenAIImageProvider
from app.providers.openai_text import OpenAITextProvider
from app.providers.seedance import Seedance20VideoProvider


class ProviderRouter:
    def text(self, provider: str | None = None) -> TextProvider:
        selected = provider or settings.default_text_provider
        if selected == "dashscope" and settings.dashscope_api_key:
            return DashScopeTextProvider()
        if selected == "openai" and settings.openai_api_key:
            return OpenAITextProvider()
        return MockTextProvider()

    def image(self, provider: str | None = None) -> ImageProvider:
        selected = provider or settings.default_image_provider
        if selected == "dashscope" and settings.dashscope_api_key:
            return DashScopeImageProvider()
        if selected in {"openai", "gpt_image_2"} and settings.openai_api_key:
            return OpenAIImageProvider()
        if selected == "nano_banana_2" and settings.google_api_key:
            return GoogleNanoBanana2Provider()
        return MockImageProvider()

    def video(self, provider: str | None = None) -> VideoProvider:
        selected = provider or settings.default_video_provider
        if selected == "dashscope" and settings.dashscope_api_key:
            return DashScopeVideoProvider()
        if selected == "seedance_2_0" and settings.seedance_api_key and settings.seedance_base_url:
            return Seedance20VideoProvider()
        return MockVideoProvider()
