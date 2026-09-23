"""Worker 配置（pydantic-settings，取代 DeepSearch 的全局 Settings 对象）。"""

from __future__ import annotations

import json
from typing import Literal

from llm_runtime import LLMConfig, parse_model_prices, parse_role_retry
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from scholar_gateway.providers import ProviderConfig
from visuals import ImageProviderConfig


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    worker_max_jobs: int = Field(default=4, ge=1, le=32)
    worker_queue_name: str = "arq:queue"
    mcp_web_enabled: bool = True
    exa_api_key: str = Field(default="", repr=False)
    mcp_run_call_limit: int = Field(default=10, ge=1, le=20)
    mcp_user_daily_calls: int = Field(default=100, ge=1)
    mcp_global_daily_calls: int = Field(default=1000, ge=1)
    mcp_call_timeout: float = Field(default=30, gt=0, le=60)
    mcp_run_timeout: float = Field(default=180, gt=0, le=300)

    evidence_retrieval_mode: Literal["legacy", "hybrid", "hybrid_rerank"] = "legacy"

    writer_polish_policy: Literal["legacy", "full_parallel", "selective_parallel"] = "legacy"
    writer_polish_concurrency: int = Field(default=2, ge=1, le=2)
    semantic_repair_engine: Literal["legacy", "langgraph"] = "legacy"

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
    llm_role_thinking: str = '{"extractor":"disabled","reranker":"disabled","verifier":"disabled"}'
    # 模型 → 单价（每百万 token）。留空则所有调用记为**未定价**，成本面板会
    # 明说金额不完整，而不是显示一个看起来很划算的 $0.00。
    #   {"glm-5.3-flash": {"input": 0.8, "output": 2.8}}
    llm_model_prices: str = "{}"
    # 角色 → 截断重试策略。留空则用 llm_runtime 的默认档位。
    #   {"writer": {"max_attempts": 0}, "verifier": {"multiplier": 2.0, "max_attempts": 1}}
    # 与上面几项不同，这一项**配错就抛**：被静默忽略的重试策略意味着某个角色继续
    # 按旧策略烧调用，而部署以为自己已经调过了。
    llm_role_retry: str = "{}"
    # 单次 HTTP 尝试的分段超时（connect/read/write），秒。**这才是长调用真正撞上的墙**：
    # 生产实测（2026-09-07，GLM-5.3-Flash）writer 请求 16000 预算时平均 52.5s，贴着
    # 默认的 60s 跑；偶尔越线的那次会被重试两轮，最终以 60×3+退避 ≈ 183.8s 失败。
    # 那个数字长期被误读成 `total_deadline_seconds`（180s）在生效——两条路径的
    # error_code 都是 `timeout`，只看错误码分不出来，要看耗时。
    # 代价要知情：调高它会让**真正卡死**的端点等更久（最坏 timeout × (max_retries+1)）。
    llm_timeout_seconds: float = 60.0
    # Jev/System One is an independent, typed decision path.  It is disabled by
    # default; rollout is controlled separately from the generative provider.
    typesafe_api_key: str = Field(default="", repr=False)
    typesafe_base_url: str = "https://api.typesafe.ai/v1/systemone"
    typesafe_model: str = "jev-1.13.0"
    typesafe_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    typesafe_concurrency: int = Field(default=2, ge=1, le=8)
    typesafe_failure_threshold: int = Field(default=5, ge=1, le=20)
    typesafe_cooldown_seconds: float = Field(default=60.0, gt=0, le=600)
    typesafe_soft_check_mode: Literal["off", "shadow", "on"] = "off"
    typesafe_confidence_threshold: float = Field(default=0.90, ge=0, le=1)
    typesafe_cache_enabled: bool = True
    typesafe_model_prices: str = '{"jev-1.13.0":{"input":0.042,"output":0}}'
    # 写作后的「重写未达标章节 + 全文重新评估」轮数。2 是设计值：一轮走一条修复路，
    # 两轮足以让「先补证据、再收回论断」这条最常见的组合走完。做成可配置是因为它是
    # 全流程最大的一块墙钟——生产实测（2026-09-06）66 分钟里 37 分钟在这里，29 次
    # 章节重写。赶时间的 draft 档可以降到 1：实测（2026-09-07）修好按节结算之后，
    # 单轮已能保留 9 节里的 5 节，代价是放弃上面那条要两轮才走得完的组合路径。
    # 0 表示写完就交，不做任何修复。
    quality_repair_rounds: int = 2

    # Independent structured tasks can safely share the provider concurrently.
    # Keep the bounds below the default SQLAlchemy overflow capacity.
    card_concurrency: int = 6
    qmatrix_concurrency: int = 4

    # ---- 并发化（2026-09-07）----------------------------------------------
    # 生产实测（job 1b6ba10a，65.7 分钟）：**51.4 分钟里只有一个 LLM 调用在飞**，
    # 所有调用延迟之和 65.6 分钟 ≈ 墙钟，也就是说管线几乎完全串行。下面这组是
    # 把各段独立工作并发起来的闸门，**不改变任何一次调用的输入**——提速全部来自调度。
    #
    # 真正的绑定约束是数据库连接池，不是 provider：`db/session.py` 是
    # pool_size=2 + max_overflow=12，每进程上限 14 条，而每个并发任务的落库与
    # 每次 `context.emit` 都要占一条。各阶段互不重叠，所以逐项限额不必相加，
    # 但全局闸门是保险带。provider 侧没有任何背压（`proxy_relay.py` 是裸 TCP 转发，
    # 无连接上限、无排队），所以客户端这顶帽子是唯一的帽子。
    # 从 6 起步；上调前先看 `llm_call_log.error_code` 有没有出现限流码。
    llm_max_concurrency: int = 6
    # 修复轮章节重写的波宽。章节之间有「前两节滚动摘要」的依赖，所以不是简单扇出，
    # 而是按 outline 序号分层（见 `document._repair_levels`），层内才并发。
    writer_repair_concurrency: int = 3
    # Opt in only after the paired A/B/C evaluation; resumes pin their original mode.
    writer_execution_mode: Literal["legacy", "dag_serial", "dag_parallel"] = "legacy"
    writer_concurrency: int = Field(default=2, ge=1, le=4)
    writer_frame_concurrency: int = Field(default=1, ge=1, le=2)
    agent_max_calls: int = Field(default=2000, ge=1)
    agent_max_reserved_tokens: int = Field(default=50_000_000, ge=1)
    agent_max_seconds: int = Field(default=7200, ge=1)
    evaluator_shadow_enabled: bool = True
    evaluator_shadow_document_limit: int = Field(default=10, ge=0, le=20)
    evaluator_shadow_repeats: int = Field(default=3, ge=2, le=3)
    section_review_concurrency: int = 4
    verifier_concurrency: int = 3
    synthesis_concurrency: int = 3
    rerank_concurrency: int = 2
    # OA 全文抓取：按文献并发，但**同一 host 仍然串行**。
    # `SafeHttpClient` 自己完全没有限速，而一次抓取计划高度集中在少数几个 host
    # （arXiv/PMC/DOAJ/出版商落地页），无界扇出会被限流甚至封禁——那会表现为
    # 全文变少、卡片变薄，也就是**质量退化**。没有正当理由不要调高 per_host。
    fulltext_download_concurrency: int = 6
    fulltext_per_host_concurrency: int = 1

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
    #
    # 2026-08-15 开启。此前它自落地起一直是 False：整个生产库 `question_synthesis`
    # **零行**，`synthesizer` 角色一次都没被调用过，于是每一节只能罗列文献——正文里
    # 「这些研究在什么条件下一致、缺口在哪」那一层根本没有人写。
    # 开启前在真实 bundle 上离线重放过（项目 6a6bbf18，6 个子问题）：6/6 出结果、
    # 0 截断、五道准入过滤 **0 拒绝**，产出 5 条 claim / 24 条 agreement /
    # 2 条 conditional / 9 条 gap，且 gap 全部是可核验的方法学缺口。
    # 代价是每个子问题约 75s（推理档位），一次全流程 ≈ +8 分钟。
    synthesis_llm_enabled: bool = True
    synthesis_llm_max_questions: int = 8

    # 核心论断语义蕴含核验的分阶段发布开关。默认 shadow：只记录判定，不改状态。
    #
    # 注意 shadow 并非「promote_only 的更安全版本」，两者的风险方向相反：
    # promote_only 不可能降级（结构上就没有这条路径），但它能把词法假阴性的
    # insufficient_support 提升为 supported，从而消掉 core_claim_fulltext_missing
    # 这一最常见的 blocker。shadow 放弃了这条解阻塞路径，**因此比 promote_only
    # 更容易把稿子判成 needs_revision**，代价是多跑质量修复轮次。
    #
    # 之所以仍然默认 shadow：提升的正确性尚未被标注样本验证过（当前影子样本覆盖率
    # 约 5.7%，且无人工标注）。覆盖率与人工抽检达标后应先升到 promote_only；
    # enforce 的负向降级必须最后启用。
    claim_entailment_mode: Literal["off", "shadow", "promote_only", "enforce"] = "shadow"

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
    #: Ceiling on the auto-selected writing corpus.  Raised from 30 so it sits
    #: above ``library_backfill_floor`` — a floor capped by the ceiling is not a
    #: floor.  ``fulltext_max_works`` (40) bounds the expensive stage anyway.
    search_auto_select_top_k: int = 40
    rerank_top_n: int = 40
    #: 综述写作语料的下限。锚点闸门是字面匹配，命中数天然偏低——实测一次真实
    #: 运行 764 篇候选只有 9 篇合取命中，写出来的稿子有近四成篇幅在描述由此
    #: 造成的「证据空白」。命中数不足时按 relevance_score 回填到这个下限，
    #: 名额本来就是空转的（top-K 30 只用掉 9）。
    library_backfill_floor: int = 36
    #: 回填的分数下限。同一次运行里 selected 的均值 0.50、被降级的
    #: candidate_uncertain 均值 0.45，而排名 40 以后开始混入跑题文献，
    #: 0.28 是实测的精度拐点。
    library_backfill_min_relevance: float = 0.28
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
        try:
            role_retry = json.loads(self.llm_role_retry) if self.llm_role_retry else {}
        except json.JSONDecodeError as error:
            raise ValueError(f"LLM_ROLE_RETRY is not valid JSON: {error}") from error
        return LLMConfig(
            provider=self.llm_default_provider,
            base_url=self.llm_openai_base_url,
            api_key=self.llm_openai_api_key,
            role_models=role_models if isinstance(role_models, dict) else {},
            role_thinking=role_thinking if isinstance(role_thinking, dict) else {},
            model_prices=parse_model_prices(model_prices),
            role_retry=parse_role_retry(role_retry),
            timeout_seconds=self.llm_timeout_seconds,
            max_concurrency=self.llm_max_concurrency,
        )

    def typesafe_decision_config(self) -> dict[str, object]:
        """Return Jev settings without exposing the credential in logs or payloads."""

        try:
            model_prices = (
                json.loads(self.typesafe_model_prices) if self.typesafe_model_prices else {}
            )
        except json.JSONDecodeError:
            model_prices = {}
        return {
            "api_key": self.typesafe_api_key,
            "base_url": self.typesafe_base_url,
            "model": self.typesafe_model,
            "timeout_seconds": self.typesafe_timeout_seconds,
            "max_concurrency": self.typesafe_concurrency,
            "failure_threshold": self.typesafe_failure_threshold,
            "cooldown_seconds": self.typesafe_cooldown_seconds,
            "soft_check_mode": self.typesafe_soft_check_mode,
            "confidence_threshold": self.typesafe_confidence_threshold,
            "cache_enabled": self.typesafe_cache_enabled,
            "model_prices": parse_model_prices(model_prices),
        }

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
