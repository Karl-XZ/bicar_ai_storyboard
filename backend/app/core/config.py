from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    app_name: str = "biche-storyboard-api"
    app_debug: bool = True

    database_url: str = "postgresql+psycopg://biche:biche@localhost:5432/biche_storyboard"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    storage_endpoint: str = "http://localhost:9000"
    storage_bucket: str = "storyboard-ai"
    storage_backend: str = "local"
    storage_local_root: str = "./local_storage"
    storage_access_key: str = "minioadmin"
    storage_secret_key: str = "minioadmin"
    storage_region: str = "us-east-1"
    storage_force_path_style: bool = True

    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_base_url: str = "https://open.feishu.cn"
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_default_chat_id: str = ""
    feishu_root_folder_token: str = "root"
    feishu_workspace_parent_url: str = "https://ocnwptzvwvt6.feishu.cn/drive/folder/TcAUfNw3nlk8eTdrPWxc0kK3nJe"
    feishu_workspace_folder_name: str = "AI分镜"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com"
    openai_text_model: str = "gpt-5.4"
    openai_image_model: str = "gpt-image-2"
    openai_deep_research_model: str = "o4-mini-deep-research"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_anthropic_base_url: str = "https://api.deepseek.com/anthropic"
    deepseek_text_model: str = "deepseek-v4-pro"
    deepseek_fast_text_model: str = "deepseek-v4-flash"

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_text_model: str = "google/gemini-3.1-pro-preview"
    openrouter_fast_text_model: str = "google/gemini-3.1-flash-lite-preview"
    openrouter_deep_research_model: str = "openai/o4-mini-deep-research"
    openrouter_image_model: str = "openai/gpt-5.4-image-2"
    openrouter_nano_banana_model: str = "google/gemini-3.1-flash-image-preview"

    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    dashscope_compatible_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_text_model: str = "qwen-plus"
    dashscope_image_model: str = "wanx2.1-t2i-turbo"
    dashscope_video_model: str = "wan2.2-kf2v-flash"
    dashscope_i2v_model: str = "wanx2.1-i2v-turbo"
    dashscope_t2v_model: str = "wan2.2-t2v-plus"
    dashscope_image_size: str = "1280*720"
    dashscope_video_resolution: str = "720P"
    dashscope_prompt_extend: bool = True

    default_text_provider: str = "deepseek"
    default_image_provider: str = "openrouter"
    default_video_provider: str = "xyq_nest"

    google_api_key: str = ""
    google_base_url: str = "https://generativelanguage.googleapis.com"
    google_deep_research_model: str = "deep-research-preview-04-2026"
    google_deep_research_poll_interval_seconds: int = Field(default=10, ge=1)
    google_deep_research_max_poll_attempts: int = Field(default=90, ge=1)
    nano_banana_model: str = "gemini-3.1-flash-image-preview"

    seedance_api_key: str = ""
    seedance_base_url: str = ""
    seedance_model_id: str = ""
    seedance_webhook_secret: str = ""

    xyq_access_key: str = ""
    xyq_base_url: str = "https://xyq.jianying.com"
    xyq_video_model: str = "小云雀"

    chatbot_memory_rounds: int = Field(default=20, ge=0)
    image_parallel_per_project: int = Field(default=10, ge=1)
    video_parallel_per_project: int = Field(default=2, ge=1)
    image_max_retries: int = Field(default=3, ge=0)
    video_polling_interval_seconds: int = Field(default=10, ge=1)
    video_max_polling_attempts: int = Field(default=180, ge=1)
    feishu_backfill_batch_size: int = Field(default=50, ge=1)
    workflow_inline_execution: bool = True
    workflow_keyframe_variants: int = Field(default=3, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
