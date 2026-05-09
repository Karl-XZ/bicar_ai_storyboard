from app.core.config import settings
from app.providers.openrouter_image import OpenRouterImageProvider
from app.providers.router import ProviderRouter
from app.services.workflow import WorkflowService


def test_image_provider_prefers_openrouter_for_compat_aliases(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "openrouter-test")
    monkeypatch.setattr(settings, "google_api_key", "google-test")
    monkeypatch.setattr(settings, "openai_api_key", "openai-test")

    router = ProviderRouter()

    assert isinstance(router.image("gpt_image_2"), OpenRouterImageProvider)
    assert isinstance(router.image("nano_banana_2"), OpenRouterImageProvider)


def test_workflow_infers_openrouter_for_supported_image_aliases(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "openrouter-test")
    monkeypatch.setattr(settings, "google_api_key", "google-test")
    monkeypatch.setattr(settings, "openai_api_key", "openai-test")

    workflow = WorkflowService(db=None)

    assert workflow._infer_provider(kind="image", provider="auto", model_id="gpt_image_2") == "openrouter"
    assert workflow._infer_provider(kind="image", provider="auto", model_id="nano_banana_2") == "openrouter"
