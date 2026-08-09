"""API 请求/响应模型（设计 §4.7）。前端 `apps/web/lib/types.ts` 与之一一对应。"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=15, max_length=128)
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
    new_password: str = Field(min_length=15, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=15, max_length=128)


class AuthMessageResponse(BaseModel):
    message: str


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    email_verified: bool


class LoginResponse(BaseModel):
    user: UserResponse


class AuthorDetail(BaseModel):
    """一位作者在当前论文里的署名快照。"""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    affiliations: list[str] = Field(default_factory=list, max_length=8)
    email: EmailStr | None = None
    orcid: str | None = Field(default=None, max_length=19)
    corresponding: bool = False

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        result = " ".join(value.split())
        if not result:
            raise ValueError("author name must not be empty")
        return result

    @field_validator("affiliations")
    @classmethod
    def clean_affiliations(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = " ".join(value.split())[:240]
            key = item.casefold()
            if item and key not in seen:
                seen.add(key)
                cleaned.append(item)
        return cleaned

    @field_validator("orcid")
    @classmethod
    def validate_orcid(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        compact = re.sub(r"[^0-9Xx]", "", value)
        if len(compact) != 16:
            raise ValueError("ORCID must contain 16 digits")
        total = 0
        for char in compact[:15]:
            if not char.isdigit():
                raise ValueError("ORCID has an invalid format")
            total = (total + int(char)) * 2
        remainder = (12 - total % 11) % 11
        expected = "X" if remainder == 10 else str(remainder)
        if compact[-1].upper() != expected:
            raise ValueError("ORCID checksum is invalid")
        compact = compact[:15] + compact[-1].upper()
        return "-".join(compact[index : index + 4] for index in range(0, 16, 4))

    @model_validator(mode="after")
    def corresponding_author_has_email(self) -> AuthorDetail:
        if self.corresponding and self.email is None:
            raise ValueError("a corresponding author must have an email address")
        return self


class AcademicProfileRequest(BaseModel):
    profile: AuthorDetail | None = None


class AcademicProfileResponse(BaseModel):
    profile: AuthorDetail | None = None


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
    author_details: list[AuthorDetail] | None = None
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
    author_details: list[AuthorDetail] | None = None
    keywords: list[str] | None = None
    metadata_confirmed: bool | None = None


class ProjectAttentionSummary(BaseModel):
    manuscript: dict[str, Any]
    active_job: dict[str, Any] | None = None
    readiness: dict[str, Any]
    latest_pdf: dict[str, Any] | None = None


class SubmissionReadinessItem(BaseModel):
    key: str
    label: str
    state: Literal["pass", "warn", "fail", "unknown", "stale"]
    checked_at: datetime | None = None
    reason: str
    fix_href: str


class SubmissionReadinessResponse(BaseModel):
    project_id: str
    state: Literal["pass", "warn", "fail", "unknown", "stale"]
    checked_at: datetime | None = None
    items: list[SubmissionReadinessItem] = Field(default_factory=list)


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
    author_details: list[AuthorDetail] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    metadata_confirmed: bool = False
    library_count: int = 0
    section_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    #: 非空 = 在回收站里。常规列表永远取不到这样的项目，只有 ?deleted=true 会。
    deleted_at: datetime | None = None
    attention_summary: ProjectAttentionSummary | None = None


class ScopeResponse(BaseModel):
    project_id: str
    scope: dict[str, Any]


class UpdateScopeRequest(BaseModel):
    scope: dict[str, Any]


class TaskDefinitionResponse(BaseModel):
    """任务本体目录里的一条，供绑定选择器渲染。"""

    slug: str
    domain: str
    label: str
    metric_count: int = 0
    dataset_count: int = 0
    has_vocabulary: bool = False


class ProjectTaskProfileResponse(BaseModel):
    """项目当前**实际生效**的任务集，以及它是怎么来的。

    ``source`` 是这个响应的重点：三种来源在证据抽取时表现完全不同，而此前界面上
    根本看不出区别。

    * ``explicit``  —— 用户显式绑定，管线不会覆盖它；
    * ``inferred``  —— QDECOMP 从子问题里推断出来的（只在本体线索命中时才会发生）；
    * ``fallback``  —— 没有任何绑定，按 TASK_PROFILE_FALLBACK 回退。当前默认
      ``all_tasks`` 意味着这个项目继承了**每一个**领域的指标与数据集白名单。
    """

    project_id: str
    source: str
    bound: bool
    task_ids: list[str] = Field(default_factory=list)
    effective_tasks: list[TaskDefinitionResponse] = Field(default_factory=list)
    fallback_mode: str = "all_tasks"
    #: 回退生效时给用户看的一句话解释；显式绑定时为 None。
    fallback_note: str | None = None


class UpdateProjectTasksRequest(BaseModel):
    """空列表表示解除绑定，回到回退行为——这是"撤销"，不是"绑定到零个任务"。

    想表达"这是一篇通用学术论文"应当显式绑定 ``generic.scholarly``，那会写入一行，
    因而与"从未绑定"可区分，也不再受回退开关影响。
    """

    task_ids: list[str] = Field(default_factory=list)


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


class LibraryUtilizationResponse(BaseModel):
    """One selected work's progress from full text to an actual body citation."""

    fulltext_status: Literal["available", "parsing", "failed", "abstract_only", "unavailable"]
    fulltext_source: Literal["user_pdf", "oa", "none"] = "none"
    evidence_status: Literal["extracted", "pending", "none"] = "none"
    assignment_status: Literal["assigned", "pending", "unassigned"] = "unassigned"
    citation_status: Literal["cited", "not_cited"] = "not_cited"
    evidence_count: int = 0
    assignment_count: int = 0
    citation_count: int = 0
    usage_evaluated: bool = False
    unused_reason: str | None = None


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
    literature_role: Literal[
        "general", "core", "background", "method", "benchmark", "controversy"
    ] = "general"
    utilization: LibraryUtilizationResponse | None = None


class EligibilityDecisionResponse(BaseModel):
    """SCREEN 阶段对一篇文献的判定与理由（可复现的命中项）。"""

    work_id: str
    title: str
    decision: Literal["include", "exclude", "uncertain"]
    reason: str | None = None
    anchor_facet_hit: bool = False
    criterion_hits: dict[str, Any] = Field(default_factory=dict)
    decided_by: str = "deterministic"
    model: str | None = None


class SelectEntriesRequest(BaseModel):
    """圈选入库/排除：只在已核验候选之间切换状态，不引入新文献（R1）。"""

    work_ids: list[str] = Field(default_factory=list)
    status: EntryStatus = "selected"


class UpdateLibraryEntryRequest(BaseModel):
    literature_role: Literal["general", "core", "background", "method", "benchmark", "controversy"]


class ImportReferencesRequest(BaseModel):
    dois: list[str] = Field(default_factory=list)
    bibtex: str | None = None


class PdfExtractedMetadataResponse(BaseModel):
    doi: str | None = None
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    publication_year: int | None = None


class LiteraturePdfUploadResponse(BaseModel):
    id: str
    filename: str
    status: Literal[
        "matching",
        "needs_confirmation",
        "match_failed",
        "parsing",
        "extracting",
        "ready",
        "parse_failed",
        "rejected",
    ]
    extracted_metadata: PdfExtractedMetadataResponse | None = None
    matched_work: ScholarlyWorkResponse | None = None
    match_method: str | None = None
    match_confidence: float | None = None
    document_file_id: str | None = None
    error: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LiteraturePdfUploadStartedResponse(BaseModel):
    upload: LiteraturePdfUploadResponse
    job: JobResponse


class ConfirmLiteraturePdfRequest(BaseModel):
    literature_role: (
        Literal["general", "core", "background", "method", "benchmark", "controversy"] | None
    ) = None


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
    stale: bool = False
    stale_reason: str | None = None


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


ExportFormat = Literal[
    "pdf",
    "latex_zip",
    "markdown",
    "markdown_bundle",
    "bibtex",
    "docx",
]


def _default_export_formats() -> list[ExportFormat]:
    return ["pdf", "latex_zip", "markdown", "markdown_bundle", "bibtex", "docx"]


class ExportRequest(BaseModel):
    formats: list[ExportFormat] = Field(default_factory=_default_export_formats)
    quality_profile: Literal["draft", "scholarly", "submission"] = "scholarly"


class ExportArtifactResponse(BaseModel):
    id: str
    format: str
    document_version: int | None = None
    object_key: str | None = None
    content_hash: str | None = None
    created_at: datetime | None = None
    download_url: str | None = None
    quality_report_id: str | None = None
    quality_profile: Literal["draft", "scholarly", "submission"] = "scholarly"
    readiness_status: str = "unassessed"
    paper_snapshot_hash: str | None = None
    export_run_id: str | None = None


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


class AssetCapabilitiesResponse(BaseModel):
    max_bytes: int
    max_mib: int
    preferred_extensions: list[str] = Field(default_factory=list)
    accepts_unrecognized_as_method_note: bool = True


class MaterialPreflightIssue(BaseModel):
    code: str
    message: str


class MaterialPreflightResponse(BaseModel):
    ready: bool
    issues: list[MaterialPreflightIssue] = Field(default_factory=list)


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

    kind: Literal["auto", "chart", "diagram", "ai_image"] = "auto"
    intent: str = Field(default="", max_length=500)
    target_section_key: str | None = None
    source_asset_refs: list[str] = Field(default_factory=list, max_length=12)


class DraftVisualResponse(BaseModel):
    kind: Literal["chart", "diagram", "ai_image"]
    title: str = ""
    caption: str = ""
    alt_text: str = ""
    spec: dict[str, Any] = Field(default_factory=dict)
    target_section_key: str | None = None
    suggested_block_index: int | None = None
    reason: str = ""
    context_summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    #: `llm:<model>` 或 `deterministic`——界面据此说明这份草稿是不是模型写的。
    generator: str = "deterministic"


class RegenerateVisualRequest(BaseModel):
    spec: dict[str, Any] | None = None
    caption: str | None = None
    alt_text: str | None = None
    revision_instruction: str | None = Field(default=None, max_length=500)


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
    #: 最近一次成功生成 rendition 的准确时间，来自 visual_generation_attempt。
    #: 与建议的 created_at 分开，避免把“提出建议”误写成“图片已生成”。
    generated_at: datetime | None = None
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
    quality_profile: Literal["draft", "scholarly", "submission"] = "scholarly"
    review_style: Literal["narrative", "systematic"] = "narrative"


class FullPipelineOptionsRequest(GenerationOptionsRequest):
    """一键全管线的默认档位，和单跑质量端点不同。

    ``/quality``、``/quality/repair`` 是用户主动要一次严格评估，默认 scholarly 合理。
    全管线的承诺是「一次跑到 PDF」：scholarly 档会在正文写完后再追加最多两轮
    「重写未达标章节 + 全文重新评估」，而且没过质量门连导出都不做——把它当默认值，
    等于让「跑通全流程」默认可能不产出任何稿件。draft 档同样跑完整评估、发现项
    一条不少，只是不升级成阻断项；要不要为这些发现项花一轮重写，由用户跑完后决定。
    """

    quality_profile: Literal["draft", "scholarly", "submission"] = "draft"


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
    quality_profile: Literal["draft", "scholarly", "submission"] = "scholarly"
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
    depth_metrics: dict[str, Any] = Field(default_factory=dict)


class ResearchQuestionResponse(BaseModel):
    id: str
    parent_id: str | None = None
    text: str
    kind: Literal["core", "sub"]
    order_index: int
    comparison_dimensions: list[str] = Field(default_factory=list)
    expected_evidence_kinds: list[str] = Field(default_factory=list)
    answer_status: Literal["answered", "partial", "contested", "insufficient_evidence"]
    generator: str | None = None
    origin: Literal["auto", "user"] = "auto"
    locked: bool = False
    task_id: str | None = None
    search_query: str | None = None


class UpdateResearchQuestionRequest(BaseModel):
    text: str | None = None
    comparison_dimensions: list[str] | None = None
    expected_evidence_kinds: list[str] | None = None
    answer_status: Literal["answered", "partial", "contested", "insufficient_evidence"] | None = (
        None
    )
    task_id: str | None = None
    # 检索面：问题改了却不能改检索式的话，编辑对召回毫无作用。
    search_query: str | None = None
    # 解锁后这一行重新交给自动重生成管理。
    locked: bool | None = None


class EvidenceUnitResponse(BaseModel):
    id: str
    work_id: str
    cite_key: str | None = None
    title: str | None = None
    kind: str
    grade: str
    anchor_strength: str | None = None
    text: str
    page: int | None = None
    section_path: str | None = None
    paragraph_index: int | None = None
    object_ref: str | None = None
    measurements: list[dict[str, Any]] = Field(default_factory=list)


class QuestionEvidenceLinkResponse(BaseModel):
    id: str
    research_question_id: str
    evidence_unit_id: str
    stance: Literal["supports", "contradicts", "conditional", "not_comparable", "gap"]
    condition_note: str | None = None
    confidence: float | None = None
    manually_overridden: bool = False


class UpdateQuestionEvidenceLinkRequest(BaseModel):
    stance: Literal["supports", "contradicts", "conditional", "not_comparable", "gap"]
    condition_note: str | None = None


class EvidenceMatrixPerQuestionDiagnostics(BaseModel):
    question_id: str
    candidate_count: int = 0
    classified_count: int = 0
    eligible_abc_count: int = 0
    best_score: float | None = None
    routing_mode: str | None = None
    bridge_source: str | None = None
    no_link_reason: str | None = None
    rejected_by_lexical: int | None = None
    rejected_by_task: int | None = None
    fallback_classifier_used: bool = False
    # 被分类器整批否掉时的候选样本：用来区分「候选真不相关」和「提示过严」。
    rejected_sample: list[dict[str, Any]] = Field(default_factory=list)


class EvidenceMatrixDiagnostics(BaseModel):
    evidence_unit_count: int = 0
    link_count: int = 0
    question_count: int = 0
    unlinked_evidence_count: int = 0
    rejected_by_lexical: int | None = None
    rejected_by_task: int | None = None
    zero_candidate_questions: int | None = None
    bridge_sources: dict[str, int] | None = None
    per_question: list[EvidenceMatrixPerQuestionDiagnostics] | None = None


class EvidenceMatrixResponse(BaseModel):
    questions: list[ResearchQuestionResponse] = Field(default_factory=list)
    evidence: list[EvidenceUnitResponse] = Field(default_factory=list)
    links: list[QuestionEvidenceLinkResponse] = Field(default_factory=list)
    diagnostics: EvidenceMatrixDiagnostics | None = None


class SynthesisResponse(BaseModel):
    questions: list[ResearchQuestionResponse] = Field(default_factory=list)
    bundles: list[dict[str, Any]] | None = None
    comparison_cluster_count: int = 0


class ClaimEvidenceResponse(BaseModel):
    id: str
    report_id: str
    section_key: str
    claim_text: str
    claim_kind: str
    is_core: bool = False
    cite_key: str | None = None
    source_key: str | None = None
    user_asset_id: str | None = None
    source_kind: str
    source_page: int | None = None
    source_section: str | None = None
    source_paragraph: int | None = None
    evidence_excerpt: str | None = None
    evidence_hash: str | None = None
    evidence_unit_id: str | None = None
    comparability_ok: bool | None = None
    grade_ok: bool | None = None
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


class RewriteSectionRequest(BaseModel):
    instruction: str = Field(min_length=3, max_length=2000)
    expected_updated_at: datetime | None = None
    allowed_evidence_refs: list[str] = Field(default_factory=list, max_length=100)


class RewriteSectionCandidateResponse(BaseModel):
    section_key: str
    original_body_ir: dict[str, Any]
    candidate_body_ir: dict[str, Any]
    changed: bool
    checks: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None


class AcceptSectionRewriteRequest(BaseModel):
    candidate_body_ir: dict[str, Any]
    expected_updated_at: datetime | None = None


class AcceptSectionRewriteResponse(BaseModel):
    document_version: int
    section: SectionResponse


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
