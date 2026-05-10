from __future__ import annotations

IMAGE_MODEL_NANOBANANA = "nanobanana"
# Backward compatibility for existing imports and historical config values.
IMAGE_MODEL_NEOBUNANA = IMAGE_MODEL_NANOBANANA
IMAGE_MODEL_GPT2 = "gpt2"
VIDEO_MODEL_XYQ = "小云雀"


def normalize_image_model(model: str | None) -> str:
    value = str(model or "").strip()
    lowered = value.lower()
    if lowered in {
        "nanobanana",
        "neobunana",
        "nano_banana_2",
        "google/gemini-3.1-flash-image-preview",
        "gemini-3.1-flash-image-preview",
    }:
        return IMAGE_MODEL_NANOBANANA
    if lowered in {
        "gpt2",
        "gpt_image_2",
        "openai/gpt-5.4-image-2",
        "gpt-5.4-image-2",
    }:
        return IMAGE_MODEL_GPT2
    return value


def resolve_openrouter_image_model(model: str | None, *, default_openrouter_image_model: str, default_nano_banana_model: str) -> str:
    normalized = normalize_image_model(model)
    if normalized == IMAGE_MODEL_NANOBANANA:
        return default_nano_banana_model
    if normalized == IMAGE_MODEL_GPT2:
        return default_openrouter_image_model
    return normalized


def image_model_options() -> list[str]:
    return [
        IMAGE_MODEL_NANOBANANA,
        IMAGE_MODEL_GPT2,
        "wanx2.1-t2i-turbo",
        "wanx-v1",
    ]


def normalize_video_model(model: str | None) -> str:
    value = str(model or "").strip()
    lowered = value.lower()
    if lowered in {"小云雀", "xyq_nest_video", "xyq", "xyq_nest", "xiaoyunque", "xiao_yunque"}:
        return VIDEO_MODEL_XYQ
    return value


def video_model_options() -> list[str]:
    return [
        "wan2.2-kf2v-flash",
        "wanx2.1-kf2v-plus",
        "wanx2.1-i2v-turbo",
        "seedance_2_0",
        VIDEO_MODEL_XYQ,
    ]


def video_provider_display(provider: str | None, model: str | None = None) -> str:
    normalized_model = normalize_video_model(model)
    if normalized_model == VIDEO_MODEL_XYQ:
        return VIDEO_MODEL_XYQ
    if str(provider or "").strip().lower() in {"xyq_nest", "xyq", "xiao_yunque", "xiaoyunque"}:
        return VIDEO_MODEL_XYQ
    return str(provider or "").strip() or "unknown"
