"""API 请求/响应模型（设计 §4.7）。前端 `apps/web/lib/types.ts` 与之一一对应。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=120)


class LoginRequest(BaseModel):
    # Kept as `email` for API compatibility, but development also accepts the
    # local username `admin` when AUTH_DEV_LOGIN_ENABLED is enabled.
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class TokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=256)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(TokenRequest):
    new_password: str = Field(min_length=8, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class AuthMessageResponse(BaseModel):
    message: str


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    email_verified: bool


class LoginResponse(BaseModel):
    user: UserResponse

PaperType = Literal["review", "original"]
WritingMode = Literal["auto", "assisted"]
Language = Literal["zh", "en"]
CitationStyle = Literal["author_year", "gbt7714", "ieee", "apa"]
EntryStatus = Literal["candidate", "selected", "excluded"]


class CreateProjectRequest(BaseModel):
    title: str
    paper_type: PaperType
    writing_mode: WritingMode = "auto"
    language: Language = "en"
    topic: str | None = None
    venue_template: str | None = None
    citation_style: CitationStyle = "author_year"
    contribution_points: list[str] = Field(default_factory=list)
    publication_title: str | None = None
    authors: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class UpdateProjectRequest(BaseModel):
    """
    项目元数据的局部更新。所有字段可选，**未出现的字段不改**
    （靠 `model_fields_set` 区分「没传」与「传了 null」——后者是合法的清空）。

    刻意不含 `paper_type`：论文类型决定管线形状、大纲结构与是否做数字一致性 lint，
    中途改只会得到自相矛盾的稿子。换类型 = 新建项目。
    """

    title: str | None = None
    topic: str | None = None
    venue_template: str | None = None
    language: Language | None = None
    citation_style: CitationStyle | None = None
    writing_mode: WritingMode | None = None
    contribution_points: list[str] | None = None
    publication_title: str | None = None
    authors: list[str] | None = None
    keywords: list[str] | None = None
    metadata_confirmed: bool | None = None


class ProjectResponse(BaseModel):
    id: str
    title: str
    paper_type: str
    writing_mode: str
    language: str
    status: str
    venue_template: str | None = None
    citation_style: str = "author_year"
    topic: str | None = None
    contribution_points: list[str] = Field(default_factory=list)
    publication_title: str | None = None
    authors: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    metadata_confirmed: bool = False
    library_count: int = 0
    section_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ScopeResponse(BaseModel):
    project_id: str
    scope: dict[str, Any]


class UpdateScopeRequest(BaseModel):
    scope: dict[str, Any]


class GenerateScopeRequest(BaseModel):
    topic: str | None = None


class SearchRequest(BaseModel):
    providers: list[str] | None = None
    regenerate_scope: bool = False


class JobResponse(BaseModel):
    id: str
    project_id: str
    kind: str
    status: str
    stage: str | None = None
    progress: float = 0.0
    checkpoint: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None


class ScholarlyWorkResponse(BaseModel):
    id: str
    canonical_title: str
    authors: list[str] = Field(default_factory=list)
    publication_year: int | None = None
    venue_name: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    oa_status: str | None = None
    is_retracted: bool = False
    abstract: str | None = None
    citation_count: int | None = None


class QuotablePointResponse(BaseModel):
    text: str
    page: int | None = None
    section: str | None = None
    paragraph: int | None = None


class LiteratureCardResponse(BaseModel):
    summary: str = ""
    contributions: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    results: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    quotable_points: list[str | QuotablePointResponse] = Field(default_factory=list)
    fulltext_used: bool = False
    extraction_model: str | None = None


class LibraryEntryResponse(BaseModel):
    id: str
    work: ScholarlyWorkResponse
    status: str
    relevance_score: float = 0.0
    rank_reason: str | None = None
    user_pinned: bool = False
    added_via: str
    bibtex_key: str | None = None
    verified_at: datetime | None = None
    card: LiteratureCardResponse | None = None


class SelectEntriesRequest(BaseModel):
    """圈选入库/排除：只在已核验候选之间切换状态，不引入新文献（R1）。"""

    work_ids: list[str] = Field(default_factory=list)
    status: EntryStatus = "selected"


class ImportReferencesRequest(BaseModel):
    dois: list[str] = Field(default_factory=list)
    bibtex: str | None = None


class SearchRunResponse(BaseModel):
    id: str
    provider: str
    query_text: str
    hit_count: int | None = None
    retrieved_count: int | None = None
    status: str | None = None
    executed_at: datetime | None = None
    error: str | None = None


class WhitelistResponse(BaseModel):
    """R1 写作白名单：前端引用 chip 的唯一取值来源。"""

    project_id: str
    cite_keys: list[str]


class CostResponse(BaseModel):
    project_id: str
    call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_estimate: float = 0.0
    # 失败调用数：draft-first 下失败会静默降级，成本面板必须能看见它。
    failed_call_count: int = 0


# ---- M2：大纲 / 章节 / 引用审计 / 预览 ----


class OutlineResponse(BaseModel):
    project_id: str
    outline_id: str | None = None
    version: int = 0
    status: str = "draft"
    tree: dict[str, Any] = Field(default_factory=dict)


class UpdateOutlineRequest(BaseModel):
    tree: dict[str, Any]
    status: Literal["draft", "confirmed"] = "draft"


class WriteRequest(BaseModel):
    coherence: bool = True


class SectionResponse(BaseModel):
    section_key: str
    title: str
    order_no: int = 0
    status: str = "generated"
    model: str | None = None
    cite_keys: list[str] = Field(default_factory=list)
    body_ir: dict[str, Any] = Field(default_factory=dict)
    citation_warnings: list[dict[str, Any]] = Field(default_factory=list)
    word_count: int = 0
    updated_at: datetime | None = None


class UpdateSectionRequest(BaseModel):
    body_ir: dict[str, Any]
    title: str | None = None
    #: 基础版本。与 ApproveVisualRequest 同一套乐观并发协议：视觉批准会直接
    #: 改写章节 IR，若此时保存一份基于旧版本的草稿，刚插入的 FigureBlock
    #: 会被静默删掉。可空以兼容旧客户端。
    expected_updated_at: datetime | None = None


class CitationAuditRow(BaseModel):
    cite_key: str
    section_id: str
    work_id: str
    context_snippet: str | None = None


class CitationAuditResponse(BaseModel):
    """R2 审计：hallucinated_cite_keys 必须恒为空。"""

    project_id: str
    whitelist_size: int = 0
    used_cite_keys: list[str] = Field(default_factory=list)
    hallucinated_cite_keys: list[str] = Field(default_factory=list)
    removed_citation_warnings: list[dict[str, Any]] = Field(default_factory=list)
    unused_cite_keys: list[str] = Field(default_factory=list)
    rows: list[CitationAuditRow] = Field(default_factory=list)


class MarkdownResponse(BaseModel):
    project_id: str
    markdown: str = ""
    word_count: int = 0
    document_version: int | None = None


# ---- M3：导出 ----


class ExportRequest(BaseModel):
    formats: list[Literal["pdf", "latex_zip", "markdown", "markdown_bundle", "bibtex", "docx"]] = (
        Field(
            default_factory=lambda: [
                "pdf",
                "latex_zip",
                "markdown",
                "markdown_bundle",
                "bibtex",
                "docx",
            ]
        )
    )
    quality_profile: Literal["draft", "submission"] = "draft"


class ExportArtifactResponse(BaseModel):
    id: str
    format: str
    document_version: int | None = None
    object_key: str | None = None
    content_hash: str | None = None
    created_at: datetime | None = None
    download_url: str | None = None
    quality_report_id: str | None = None
    quality_profile: Literal["draft", "submission"] = "draft"
    readiness_status: str = "unassessed"
    paper_snapshot_hash: str | None = None


# ---- M4：素材与数字 lint ----


class AssetResponse(BaseModel):
    id: str
    kind: str
    title: str | None = None
    description: str | None = None
    created_at: datetime | None = None
    parsed_type: str | None = None
    row_count: int | None = None
    column_count: int | None = None
    number_count: int = 0
    headers: list[str] = Field(default_factory=list)
    preview_rows: list[list[str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # asset_ref 供大纲/章节引用素材（渲染期确定性展开）。
    asset_ref: str | None = None


# ---- M8：视觉建议 / 生成 / 审核 ----


class CreateVisualRequest(BaseModel):
    spec: dict[str, Any]
    title: str | None = None
    caption: str = ""
    alt_text: str = ""
    target_section_key: str | None = None
    suggested_block_index: int | None = None


class UpdateVisualRequest(BaseModel):
    spec: dict[str, Any] | None = None
    title: str | None = None
    caption: str | None = None
    alt_text: str | None = None
    target_section_key: str | None = None
    suggested_block_index: int | None = None


class ApproveVisualRequest(BaseModel):
    section_key: str
    block_index: int
    #: 客户端读到该章节时的 `updated_at`。带上它才有乐观并发保护：
    #: 服务端发现章节已被别处改动就返回 409 `section_changed`，而不是
    #: 把插图写进一份已经过期的 IR 上。为兼容旧客户端保持可空。
    expected_section_updated_at: datetime | None = None


class DraftVisualRequest(BaseModel):
    """让模型把「一句话想法」补成一份完整的视觉规格。

    此前新建一张 AI 插图要用户手写图注、替代文本和构图描述三段文本，还得自己
    避开会触发内容审核的词——那是把提示词工程外包给了作者。这里只收一句意图，
    其余交给 planner 角色补全。
    """

    kind: Literal["diagram", "ai_image"] = "ai_image"
    intent: str = Field(default="", max_length=500)
    target_section_key: str | None = None


class DraftVisualResponse(BaseModel):
    title: str = ""
    caption: str = ""
    alt_text: str = ""
    spec: dict[str, Any] = Field(default_factory=dict)
    #: `llm:<model>` 或 `deterministic`——界面据此说明这份草稿是不是模型写的。
    generator: str = "deterministic"


class RegenerateVisualRequest(BaseModel):
    spec: dict[str, Any] | None = None
    caption: str | None = None
    alt_text: str | None = None


class VisualErrorResponse(BaseModel):
    """结构化失败信息。

    `code` 取自 `visuals.errors` 的统一词表；历史行里的旧值在读侧被映射过来，
    因此界面只需要认识一套词表。`message` 是可执行的中文提示，不是厂商英文串
    的转述。`request_id` 现在在失败路径上也有值（provider 抛错前读 cf-ray）。
    """

    code: str
    message: str
    retryable: bool = False
    request_id: str | None = None
    #: 脱敏后的技术细节，折叠展示，供排查用。
    detail: str | None = None


class VisualSummaryResponse(BaseModel):
    """项目视觉状态计数。

    导航状态点、项目概览与导出提醒都只需要这几个数字；让它们各自去拉一遍
    完整视觉列表（含 spec 与 renditions）纯属浪费。

    注：`stale`（建议基于旧版正文）依赖 `paper_snapshot_hash`，随规划器一起
    落地，这里作为可选字段返回。
    """

    project_id: str
    pending: int = 0
    generating: int = 0
    ready: int = 0
    approved: int = 0
    failed: int = 0
    rejected: int = 0
    stale: int = 0
    #: 最近一次批准插入的时间。导出中心用它和产物时间比，判断「这份 PDF 是不是
    #: 早于最近一次插图」——批准图片不会自动重新编译，用户得知道该重跑一次。
    latest_approved_at: datetime | None = None


class VisualResponse(BaseModel):
    id: str
    asset_ref: str
    kind: str
    generation_status: str
    review_status: str
    title: str | None = None
    caption: str = ""
    caption_hint: str | None = None
    alt_text: str = ""
    target_section_key: str | None = None
    suggested_block_index: int | None = None
    figure_label: str
    spec: dict[str, Any]
    provider: str | None = None
    model: str | None = None
    #: 平铺的 error_code / error_message 与嵌套的 `error` 并存一个版本，
    #: 前端切完再废弃平铺字段。
    error_code: str | None = None
    error_message: str | None = None
    error: VisualErrorResponse | None = None
    renditions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    input_hash: str
    content_hash: str | None = None
    version: int = 1
    supersedes_id: str | None = None
    #: 最近一次生成的实际输出尺寸。Cloudflare 不接受尺寸参数，用户要看到
    #: 的是**真实拿到了什么**，而不是他当初在下拉框里选了什么。
    output_width: int | None = None
    output_height: int | None = None
    #: AI 插图**实际会发送给图像服务商**的那一句。生成确认框展示它，
    #: 而不是让界面自己再拼一遍——否则用户确认的文本与真正发出去的会分叉。
    resolved_prompt: str | None = None
    #: 建议依据的正文快照。与当前正文不一致时界面提示「建议基于旧版正文」。
    paper_snapshot_hash: str | None = None
    suggestion_reason: str | None = None
    source_section_keys: list[str] = Field(default_factory=list)
    stale: bool = False
    created_at: datetime | None = None


class NumLintResponse(BaseModel):
    """数字一致性报告（设计 §4.4.2 红线）。consistent=false 必须人工处理。"""

    project_id: str
    consistent: bool = True
    asset_number_count: int = 0
    checked_count: int = 0
    sourced_count: int = 0
    unsourced_count: int = 0
    unsourced: list[dict[str, Any]] = Field(default_factory=list)


# ---- M5：雪球 / 全文 / 质量 / 润色 ----


class SnowballRequest(BaseModel):
    direction: Literal["both", "forward", "backward"] = "both"
    max_seeds: int = 8


class IngestRequest(BaseModel):
    max_works: int = 12


class GenerationOptionsRequest(BaseModel):
    quality_profile: Literal["draft", "submission"] = "draft"
    review_style: Literal["narrative", "systematic"] = "narrative"


class QualityResponse(BaseModel):
    """绑定正文快照的质量与投稿就绪报告。"""

    project_id: str
    section_count: int = 0
    word_count: int = 0
    cite_count: int = 0
    unique_cite_count: int = 0
    whitelist_size: int = 0
    citation_density: float = 0.0
    library_coverage: float = 0.0
    recent_ratio: float = 0.0
    fulltext_coverage: float = 0.0
    sections_without_citations: list[str] = Field(default_factory=list)
    soft_check: list[dict[str, Any]] = Field(default_factory=list)
    hints: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: str | None = None
    report_id: str | None = None
    document_version: int | None = None
    paper_snapshot_hash: str | None = None
    quality_profile: Literal["draft", "submission"] = "draft"
    review_style: Literal["narrative", "systematic"] = "narrative"
    readiness_status: str = "unassessed"
    stale: bool = False
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    core_claim_count: int = 0
    core_claim_fulltext_count: int = 0
    core_claim_fulltext_coverage: float = 0.0
    layout_checks: dict[str, Any] = Field(default_factory=dict)


class ClaimEvidenceResponse(BaseModel):
    id: str
    report_id: str
    section_key: str
    claim_text: str
    claim_kind: str
    is_core: bool = False
    cite_key: str | None = None
    source_kind: str
    source_page: int | None = None
    source_section: str | None = None
    source_paragraph: int | None = None
    evidence_excerpt: str | None = None
    evidence_hash: str | None = None
    support_status: str
    support_score: float | None = None
    manual_status: str = "unreviewed"


class ReviewClaimEvidenceRequest(BaseModel):
    manual_status: Literal["unreviewed", "confirmed", "rejected"]


class RefineRequest(BaseModel):
    """润色浮条：润色/扩写/缩写/改语气。绝不新增引用、绝不改动数字。"""

    action: Literal["polish", "expand", "shorten", "academic_tone"] = "polish"
    text: str
    instruction: str | None = None


class RefineResponse(BaseModel):
    action: str
    original: str
    refined: str
    changed: bool = False
    note: str | None = None


# ---- M6：设置 / 版本历史 / 成本 ----


class RoleModelResponse(BaseModel):
    role: str
    model: str
    default_model: str = ""
    description: str = ""


class ImageProviderCapabilitiesResponse(BaseModel):
    """图像提供商**真正**支持的能力。

    界面按这份声明渲染表单，不再假设所有提供商能力相同。Cloudflare
    FLUX.1-schnell 的请求体只有 prompt 与 steps——`supported_sizes` 为空时
    界面必须显示「尺寸由提供商决定」，而不是一个不会生效的比例下拉框。

    **只含能力，不含凭据**：没有 Token、Account ID 或 Base URL。
    """

    provider: str
    model: str
    supported_sizes: list[str] = Field(default_factory=list)
    supported_aspect_ratios: list[str] = Field(default_factory=list)
    quality_modes: list[str] = Field(default_factory=list)
    prompt_max_length: int = 4000
    supports_negative_prompt: bool = False
    supports_seed: bool = False
    fixed_output_size: str | None = None
    cost_estimate_available: bool = False
    note: str | None = None


class SettingsResponse(BaseModel):
    """运行时配置概览。密钥永不回传，只报告是否已配置。"""

    llm_provider: str
    llm_api_key_configured: bool = False
    llm_enabled: bool = False
    roles: list[RoleModelResponse] = Field(default_factory=list)
    scholar_contact_email_configured: bool = False
    semantic_scholar_key_configured: bool = False
    storage_backend: str = "filesystem"
    visuals_enabled: bool = True
    ai_images_enabled: bool = False
    image_provider: str = ""
    image_model: str = ""
    image_api_key_configured: bool = False
    image_provider_configured: bool = False
    image_capabilities: ImageProviderCapabilitiesResponse | None = None


class OutlineVersionResponse(BaseModel):
    id: str
    version: int
    status: str
    section_count: int = 0
    created_at: datetime | None = None


class DocumentVersionResponse(BaseModel):
    id: str
    version: int
    status: str
    section_count: int | None = None
    is_current: bool = False
    created_at: datetime | None = None


class VersionHistoryResponse(BaseModel):
    project_id: str
    outlines: list[OutlineVersionResponse] = Field(default_factory=list)
    documents: list[DocumentVersionResponse] = Field(default_factory=list)
