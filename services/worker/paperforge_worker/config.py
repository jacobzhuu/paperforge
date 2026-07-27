"""Worker 配置（pydantic-settings，取代 DeepSearch 的全局 Settings 对象）。"""

from __future__ import annotations

import json

from llm_runtime import LLMConfig
from pydantic_settings import BaseSettings, SettingsConfigDict
from scholar_gateway.providers import ProviderConfig


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://paperforge:paperforge@localhost:15432/paperforge"
    redis_url: str = "redis://localhost:16379/0"

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
    openalex_api_key: str = ""
    scholar_timeout_seconds: float = 20.0
    scholar_cache_ttl_hours: float = 72.0

    # 检索预算（设计 §4.4.1 CURATE：全自动模式取 top-K）。
    search_limit_per_provider: int = 25
    search_auto_select_top_k: int = 30
    rerank_top_n: int = 40

    texd_url: str = "http://localhost:8081"
    texd_timeout_seconds: int = 120
    visuals_enabled: bool = True
    ai_images_enabled: bool = False
    visuald_url: str = "http://localhost:8082"
    visuald_timeout_seconds: int = 30
    image_provider: str = "cloudflare"
    image_base_url: str = "https://api.cloudflare.com/client/v4"
    image_api_key: str = ""
    image_model: str = "@cf/black-forest-labs/flux-1-schnell"
    image_account_id: str = ""
    image_timeout_seconds: float = 180.0
    image_max_retries: int = 2
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

    def user_agent(self) -> str:
        agent = self.scholar_user_agent.strip() or "PaperForge/0.1"
        email = self.scholar_contact_email.strip()
        if email and "mailto" not in agent:
            return f"{agent} (mailto:{email})"
        return agent

    def provider_config(self, provider_name: str) -> ProviderConfig:
        """按 provider 注入凭据与礼貌配置（凭据只在请求时使用，不进缓存键）。"""
        api_key = ""
        if provider_name == "semantic_scholar":
            api_key = self.semantic_scholar_api_key
        elif provider_name == "openalex":
            api_key = self.openalex_api_key
        return ProviderConfig(
            api_key=api_key or None,
            user_agent=self.user_agent(),
            contact_email=self.scholar_contact_email or None,
            timeout_seconds=self.scholar_timeout_seconds,
            rate_limit_delay=0.5,
            max_retries=2,
            cache_ttl_hours=self.scholar_cache_ttl_hours,
        )


_settings: WorkerSettings | None = None


def get_settings() -> WorkerSettings:
    global _settings
    if _settings is None:
        _settings = WorkerSettings()
    return _settings
