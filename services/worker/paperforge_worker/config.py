"""Worker 配置（pydantic-settings，取代 DeepSearch 的全局 Settings 对象）。"""

from __future__ import annotations

import json
from typing import Literal

from llm_runtime import LLMConfig, parse_model_prices
from pydantic_settings import BaseSettings, SettingsConfigDict
from scholar_gateway.providers import ProviderConfig
from visuals import ImageProviderConfig


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
    llm_role_thinking: str = '{"extractor":"disabled","reranker":"disabled"}'
    # 模型 → 单价（每百万 token）。留空则所有调用记为**未定价**，成本面板会
    # 明说金额不完整，而不是显示一个看起来很划算的 $0.00。
    #   {"deepseek-v4-pro": {"input": 0.27, "output": 1.10}}
    llm_model_prices: str = "{}"

    # Independent structured tasks can safely share the provider concurrently.
    # Keep the bounds below the default SQLAlchemy overflow capacity.
    card_concurrency: int = 6
    qmatrix_concurrency: int = 4

    # LLM 结构化实验抽取（P0-3 / Phase 2）。
    #   off    —— 完全不调用，行为与引入前逐字节相同（默认）。
    #   shadow —— 调用并把结果落成 experiment_v3_llm 的 ExperimentResult 行，
    #             但 EvidenceMeasurement 仍只由正则通道写入，于是
    #             comparability_key / SYNTH / 正文全都不变，只积累抽取质量数据。
    #   on     —— 对账后的维度进入 EvidenceMeasurement，comparability_key 才真正生效。
    experiment_extraction_mode: str = "off"
    experiment_extraction_max_works: int = 20
    experiment_extraction_max_chars: int = 60_000

    # 未绑定任务的项目如何解析任务集（P0-5 / 审计修正 C-6）。
    #   all_tasks    —— 历史行为：继承**全部**任务定义。由于没有任何路由创建
    #                   ProjectTaskProfile，实践中每个项目都拿到了 recsys + BGC 的
    #                   指标与数据集白名单。
    #   generic_only —— 解析到 generic.scholarly：空词表、空数据集，只留跨领域指标核心。
    # 切换会缩小未绑定项目的匹配面，因此先发 all_tasks，量过再翻。
    task_profile_fallback: str = "all_tasks"

    # 叙述性跨研究综合（P0-4 / Phase 4）。关闭时 SYNTH bundle 与引入前逐字节相同：
    # 不加 `synthesis` 键、不读表、不调用。开启后综合只是**增补**——
    # answer_status、证据归属、comparison_clusters 一个都不由它改写。
    synthesis_llm_enabled: bool = False
    synthesis_llm_max_questions: int = 8

    # 核心论断语义蕴含核验的分阶段发布开关。默认只允许可靠的正向判定解除词法
    # 假阴性；只有经过 shadow 人工抽检后才应启用 enforce 的负向降级。
    claim_entailment_mode: Literal["off", "shadow", "promote_only", "enforce"] = "promote_only"

    @property
    def experiment_extraction_enabled(self) -> bool:
        return self.experiment_extraction_mode.strip().lower() in {"shadow", "on"}

    @property
    def experiment_extraction_authoritative(self) -> bool:
        """维度是否可以进入 EvidenceMeasurement（即影响可比性与正文）。"""
        return self.experiment_extraction_mode.strip().lower() == "on"

    scholar_contact_email: str = ""
    scholar_user_agent: str = "PaperForge/0.1"
    openalex_api_key: str = ""
    scholar_timeout_seconds: float = 20.0
    scholar_cache_ttl_hours: float = 72.0

    # 检索预算（设计 §4.4.1 CURATE：全自动模式取 top-K）。
    search_limit_per_provider: int = 25
    search_auto_select_top_k: int = 30
    rerank_top_n: int = 40
    # 一键综述默认尝试前 40 篇 OA 全文；仍可按部署成本下调。
    fulltext_max_works: int = 40

    texd_url: str = "http://localhost:8081"
    texd_timeout_seconds: int = 120
    visuals_enabled: bool = True
    ai_images_enabled: bool = True
    visuald_url: str = "http://localhost:8082"
    visuald_timeout_seconds: int = 30
    #: 默认走 Yunwu 的 OpenAI-compatible Images API：它接受尺寸与质量参数，
    #: 而 Cloudflare FLUX 只接受提示词与步数。`IMAGE_*` 仍是其他 provider 的配置源。
    image_provider: str = "yunwu"
    image_base_url: str = "https://api.cloudflare.com/client/v4"
    image_api_key: str = ""
    image_model: str = "@cf/black-forest-labs/flux-1-schnell"
    image_account_id: str = ""
    image_timeout_seconds: float = 180.0
    image_max_retries: int = 2
    yunwu_api_base_url: str = ""
    yunwu_api_key: str = ""
    yunwu_image_model: str = ""
    yunwu_image_timeout_seconds: float | None = None
    log_level: str = "INFO"

    def llm_config(self) -> LLMConfig:
        try:
            role_models = json.loads(self.llm_role_models) if self.llm_role_models else {}
        except json.JSONDecodeError:
            role_models = {}
        try:
            role_thinking = json.loads(self.llm_role_thinking) if self.llm_role_thinking else {}
        except json.JSONDecodeError:
            role_thinking = {}
        try:
            model_prices = json.loads(self.llm_model_prices) if self.llm_model_prices else {}
        except json.JSONDecodeError:
            model_prices = {}
        return LLMConfig(
            provider=self.llm_default_provider,
            base_url=self.llm_openai_base_url,
            api_key=self.llm_openai_api_key,
            role_models=role_models if isinstance(role_models, dict) else {},
            role_thinking=role_thinking if isinstance(role_thinking, dict) else {},
            model_prices=parse_model_prices(model_prices),
        )

    def user_agent(self) -> str:
        agent = self.scholar_user_agent.strip() or "PaperForge/0.1"
        email = self.scholar_contact_email.strip()
        if email and "mailto" not in agent:
            return f"{agent} (mailto:{email})"
        return agent

    def image_provider_config(self, provider_override: str | None = None) -> ImageProviderConfig:
        """解析生效的图像配置；Yunwu 凭据不与其他 provider 混用。

        普通生图使用 ``IMAGE_PROVIDER``；全流程论文摘要图显式传 ``yunwu``，
        从而不会被一个旧的 Cloudflare 环境变量悄悄改走其他供应商。
        """
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

    def provider_config(self, provider_name: str) -> ProviderConfig:
        """按 provider 注入凭据与礼貌配置（凭据只在请求时使用，不进缓存键）。"""
        api_key = ""
        if provider_name == "openalex":
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
