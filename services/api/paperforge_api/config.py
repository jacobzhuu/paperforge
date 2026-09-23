from __future__ import annotations

import json
from typing import Literal

from llm_runtime import LLMConfig
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from visuals import ImageProviderConfig

# 应用配置：pydantic-settings 取代 DeepSearch 的全局 Settings 对象（方案 §3.1）。


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    mcp_web_enabled: bool = True

    evidence_retrieval_mode: Literal["legacy", "hybrid", "hybrid_rerank"] = "legacy"

    writer_polish_policy: Literal["legacy", "full_parallel", "selective_parallel"] = "legacy"
    writer_polish_concurrency: int = Field(default=2, ge=1, le=2)
    semantic_repair_engine: Literal["legacy", "langgraph"] = "legacy"

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
    llm_role_thinking: str = '{"extractor":"disabled","reranker":"disabled"}'
    # LLM_MODEL_PRICES 里那些数字的货币。**只**决定成本面板与 `paperforge-admin cost`
    # 打印哪个符号，不做任何换算——照抄哪张价目表，就配哪个币种。默认 USD 保持既有
    # 部署原样；一个用 `$` 显示出来的人民币金额，面板自己是发现不了的。
    llm_price_currency: str = "USD"
    # 与 WorkerSettings 同名同义：单次 HTTP 尝试的分段超时（秒）。API 侧只有生图提示词
    # 分析走 LLM，但两边配同一个值，免得同一次调用在两个服务里有两种超时。
    llm_timeout_seconds: float = 60.0

    scholar_contact_email: str = ""
    scholar_user_agent: str = "PaperForge/0.1"

    texd_url: str = "http://localhost:8081"
    texd_timeout_seconds: int = 120
    visuals_enabled: bool = True
    ai_images_enabled: bool = True
    # 与 WorkerSettings 同名同义：未绑定任务的项目如何解析任务集。API 只读它，
    # 用来告诉界面「当前生效的任务集是从哪来的」；真正的抽取行为由 worker 决定。
    # 两边必须配同一个值，否则界面显示的生效任务集与实际抽取用的不一致。
    task_profile_fallback: str = "all_tasks"
    visuald_url: str = "http://localhost:8082"
    visuald_timeout_seconds: int = 30
    #: 与 worker 的默认值保持一致（`paperforge_worker.config.WorkerSettings`）：
    #: 两端不一致会让能力查询与真正生图用上不同的 provider。
    image_provider: str = "yunwu"
    image_base_url: str = "https://api.cloudflare.com/client/v4"
    image_api_key: str = ""
    image_model: str = "@cf/black-forest-labs/flux-1-schnell"
    image_account_id: str = ""
    image_timeout_seconds: float = 180.0
    image_max_retries: int = 2
    # Yunwu 使用独立命名，避免与文本 LLM 或既有 Cloudflare 凭据混用。
    yunwu_api_base_url: str = ""
    yunwu_api_key: str = ""
    yunwu_image_model: str = ""
    yunwu_image_timeout_seconds: float | None = None

    request_limits_enabled: bool = False
    job_dispatch_enabled: bool = True
    job_pending_limit: int = Field(default=100, ge=1, le=1000)
    job_user_pending_limit: int = Field(default=3, ge=1, le=100)
    job_dispatch_slots: int = Field(default=4, ge=1, le=32)
    job_short_slots: int = Field(default=0, ge=0, le=16)
    auth_registration_allowlist: str = ""
    auth_registration_restricted: bool = False

    api_host: str = "0.0.0.0"
    api_port: int = 8080
    cors_allow_origins: str = "http://localhost:3000"
    log_level: str = "INFO"

    # Authentication. Production should use HTTPS + SMTP; localhost keeps an explicit
    # developer mode so the one-command environment remains usable.
    auth_cookie_secure: bool = False
    auth_session_days: int = 30
    auth_idle_days: int = 7
    auth_rate_limit_enabled: bool = True
    # Local-only escape hatch for development before real email delivery is enabled.
    # create_app() refuses to start with this enabled outside a localhost HTTP setup.
    auth_dev_login_enabled: bool = True
    # disabled allows password login without email verification or delivery.
    auth_email_mode: str = "file"  # file (local development) | smtp | disabled
    auth_email_outbox_dir: str = "./data/auth-outbox"
    public_app_url: str = "http://localhost:3000"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True

    @property
    def auth_cookie_name(self) -> str:
        return "__Host-paperforge_session" if self.auth_cookie_secure else "paperforge_session"

    def llm_config(self) -> LLMConfig:
        try:
            role_models = json.loads(self.llm_role_models) if self.llm_role_models else {}
        except json.JSONDecodeError:
            role_models = {}
        try:
            role_thinking = json.loads(self.llm_role_thinking) if self.llm_role_thinking else {}
        except json.JSONDecodeError:
            role_thinking = {}
        return LLMConfig(
            provider=self.llm_default_provider,
            base_url=self.llm_openai_base_url,
            api_key=self.llm_openai_api_key,
            role_models=role_models if isinstance(role_models, dict) else {},
            role_thinking=role_thinking if isinstance(role_thinking, dict) else {},
            timeout_seconds=self.llm_timeout_seconds,
        )

    def image_provider_config(self, provider_override: str | None = None) -> ImageProviderConfig:
        provider = (provider_override or self.image_provider).strip().lower()
        if provider == "yunwu":
            return ImageProviderConfig(
                provider=provider,
                api_key=self.yunwu_api_key or self.image_api_key,
                model=self.yunwu_image_model or "gpt-image-1",
                base_url=self.yunwu_api_base_url or "https://yunwu.ai/v1",
                timeout_seconds=(
                    self.yunwu_image_timeout_seconds
                    if self.yunwu_image_timeout_seconds is not None
                    else self.image_timeout_seconds
                ),
                max_retries=self.image_max_retries,
            )
        return ImageProviderConfig(
            provider=provider,
            api_key=self.image_api_key,
            model=self.image_model,
            base_url=self.image_base_url,
            account_id=self.image_account_id,
            timeout_seconds=self.image_timeout_seconds,
            max_retries=self.image_max_retries,
        )


#: 已知币种的显示符号。未列入的币种直接打币种代码——猜一个符号比打 "SEK" 更容易
#: 让人看错金额。
_CURRENCY_SYMBOLS = {"USD": "$", "CNY": "¥"}


def currency_symbol(code: str) -> str:
    """货币代码 → 金额前缀。未知币种回落到代码本身（带一个空格便于阅读）。"""
    key = (code or "").strip().upper()
    return _CURRENCY_SYMBOLS.get(key) or (f"{key} " if key else "$")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
