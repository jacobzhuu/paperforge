from __future__ import annotations

import json

from llm_runtime import LLMConfig
from pydantic_settings import BaseSettings, SettingsConfigDict

# 应用配置：pydantic-settings 取代 DeepSearch 的全局 Settings 对象（方案 §3.1）。


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://paperforge:paperforge@localhost:5432/paperforge"
    redis_url: str = "redis://localhost:6379/0"

    storage_backend: str = "filesystem"
    storage_fs_root: str = "./data/objects"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "paperforge"
    minio_secret_key: str = "paperforge-secret"
    minio_bucket: str = "paperforge"
    minio_secure: bool = False

    llm_default_provider: str = "noop"
    llm_openai_base_url: str = "https://api.openai.com/v1"
    llm_openai_api_key: str = ""
    llm_role_models: str = "{}"

    scholar_contact_email: str = ""
    scholar_user_agent: str = "PaperForge/0.1"
    semantic_scholar_api_key: str = ""

    texd_url: str = "http://localhost:8081"
    texd_timeout_seconds: int = 120

    api_host: str = "0.0.0.0"
    api_port: int = 8080
    cors_allow_origins: str = "http://localhost:3000"
    log_level: str = "INFO"

    def llm_config(self) -> LLMConfig:
        try:
            role_models = json.loads(self.llm_role_models) if self.llm_role_models else {}
        except json.JSONDecodeError:
            role_models = {}
        return LLMConfig(
            provider=self.llm_default_provider,
            base_url=self.llm_openai_base_url,
            api_key=self.llm_openai_api_key,
            role_models=role_models if isinstance(role_models, dict) else {},
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
