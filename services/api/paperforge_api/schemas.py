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


class LiteratureCardResponse(BaseModel):
    summary: str = ""
    contributions: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    results: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    quotable_points: list[str] = Field(default_factory=list)
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


class ExportArtifactResponse(BaseModel):
    id: str
    format: str
    document_version: int | None = None
    object_key: str | None = None
    content_hash: str | None = None
    created_at: datetime | None = None
    download_url: str | None = None


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


class RegenerateVisualRequest(BaseModel):
    spec: dict[str, Any] | None = None
    caption: str | None = None
    alt_text: str | None = None


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
    error_code: str | None = None
    error_message: str | None = None
    renditions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    input_hash: str
    content_hash: str | None = None
    version: int = 1
    supersedes_id: str | None = None
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


class QualityResponse(BaseModel):
    """质量评分报告：只呈现，不设门槛（设计 §3.4）。"""

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
