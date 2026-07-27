// PaperForge 域模型（前端投影，对应 docs/design.md §4.3 / §4.7）。

export type PaperType = 'review' | 'original';
export type WritingMode = 'auto' | 'assisted';
export type Language = 'zh' | 'en';
export type CitationStyle = 'author_year' | 'gbt7714' | 'ieee' | 'apa';
export type QualityProfile = 'draft' | 'submission';
export type ReviewStyle = 'narrative' | 'systematic';

export type ProjectStatus =
  | 'draft'
  | 'scoping'
  | 'searching'
  | 'curating'
  | 'writing'
  | 'rendering'
  | 'review'
  | 'done';

export interface Project {
  id: string;
  title: string;
  paper_type: PaperType;
  writing_mode: WritingMode;
  language: Language;
  status: ProjectStatus;
  venue_template?: string | null;
  citation_style?: CitationStyle;
  topic?: string | null;
  contribution_points?: string[];
  publication_title?: string | null;
  authors?: string[];
  keywords?: string[];
  metadata_confirmed?: boolean;
  library_count?: number;
  section_count?: number;
  created_at?: string;
  updated_at?: string;
}

export interface CreateProjectRequest {
  title: string;
  paper_type: PaperType;
  writing_mode: WritingMode;
  language: Language;
  topic?: string;
  venue_template?: string;
  citation_style: CitationStyle;
  contribution_points?: string[];
  publication_title?: string;
  authors?: string[];
  keywords?: string[];
}

/**
 * 项目元数据的局部更新（PATCH /projects/{id}）。
 *
 * 语义与后端 `UpdateProjectRequest` 一致：**字段缺席 = 不改**，显式 `null` = 清空。
 * 因此调用方只传真正要改的键，不要为了凑齐类型把当前值全带上——那会把并发的
 * 其他修改覆盖掉。
 *
 * 没有 `paper_type`：论文类型决定管线形状、大纲结构与是否做数字一致性 lint，
 * 换类型等于新建项目（详见后端同名 schema 的注释）。
 */
export interface UpdateProjectRequest {
  title?: string;
  topic?: string | null;
  venue_template?: string | null;
  language?: Language;
  citation_style?: CitationStyle;
  writing_mode?: WritingMode;
  contribution_points?: string[];
  publication_title?: string | null;
  authors?: string[];
  keywords?: string[];
  metadata_confirmed?: boolean;
}

export type LibraryEntryStatus = 'candidate' | 'selected' | 'excluded';
export type AddedVia =
  | 'search'
  | 'snowball'
  | 'doi_import'
  | 'bibtex_import'
  | 'llm_suggested_verified';

export interface ScholarlyWork {
  id: string;
  canonical_title: string;
  authors: string[];
  publication_year?: number | null;
  venue_name?: string | null;
  doi?: string | null;
  arxiv_id?: string | null;
  oa_status?: 'gold' | 'green' | 'hybrid' | 'closed' | null;
  is_retracted?: boolean;
  abstract?: string | null;
  citation_count?: number | null;
}

export interface LibraryEntry {
  id: string;
  work: ScholarlyWork;
  status: LibraryEntryStatus;
  relevance_score: number;
  rank_reason?: string;
  user_pinned?: boolean;
  added_via: AddedVia;
  bibtex_key?: string | null;
  verified_at?: string | null;
  card?: LiteratureCard | null;
}

export interface LiteratureCard {
  summary: string;
  contributions: string[];
  methods: string[];
  results: string[];
  limitations: string[];
  quotable_points: Array<
    | string
    | {
        text: string;
        page?: number | null;
        section?: string | null;
        paragraph?: number | null;
      }
  >;
  fulltext_used: boolean;
  extraction_model?: string;
}

export interface SearchRun {
  id: string;
  provider: string;
  query_text: string;
  hit_count: number;
  retrieved_count: number;
  // partial：部分源失败但仍有结果（draft-first 降级）。
  status: 'succeeded' | 'partial' | 'failed' | 'running';
  executed_at?: string;
  error?: string | null;
}

export type DataSource = 'live' | 'mock';

export interface ApiResult<T> {
  data: T;
  source: DataSource;
  note?: string;
}

// ---- SCOPE（设计 §4.4.1：可编辑、可随时重生成） ----

export interface ScopeKeywordGroup {
  name: string;
  keywords: string[];
}

export interface ScopePayload {
  topic?: string;
  language?: Language;
  research_question?: string;
  scope_summary?: string;
  keyword_groups?: ScopeKeywordGroup[];
  subtopics?: string[];
  time_range?: { start_year?: number; end_year?: number };
  inclusion_notes?: string[];
  exclusion_notes?: string[];
  generator?: string;
  generated_at?: string;
}

// ---- 任务与进度（设计 §4.3 generation_job / job_event） ----

export type JobKind =
  | 'search'
  | 'ingest'
  | 'cards'
  | 'outline'
  | 'write'
  | 'compile'
  | 'visual'
  | 'full';
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export interface Job {
  id: string;
  project_id: string;
  kind: JobKind;
  status: JobStatus;
  stage?: string | null;
  progress: number;
  checkpoint?: Record<string, unknown> | null;
  error?: Record<string, unknown> | null;
  created_at?: string;
  finished_at?: string | null;
}

export interface JobEvent {
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  stage?: string | null;
  progress?: number | null;
  status?: JobStatus | null;
}

/**
 * worker 的阶段名（services/worker/paperforge_worker/worker.py::_STAGE_PROGRESS）。
 * 仅用于把后端阶段翻成中文标签，**不再**用作 SSE 事件白名单——
 * 事件全部走无名 message 通道，前端不需要预先知道事件名。
 */
export const JOB_STAGES = [
  'scope',
  'search',
  'curate',
  'ingest',
  'snowball',
  'cards',
  'quality',
  'outline',
  'write',
  'polish',
  'citecheck',
  'visual_plan',
  'visual_generate',
  'render',
  'import',
  'import_doi',
  'import_bibtex',
  'done',
] as const;

export type JobStage = (typeof JOB_STAGES)[number];

export interface ProjectCost {
  project_id: string;
  call_count: number;
  input_tokens: number;
  output_tokens: number;
  cost_estimate: number;
  /** 失败调用数：draft-first 下失败会静默降级，面板必须能看见它。 */
  failed_call_count?: number;
}

// ---- 大纲与章节（设计 §4.3 outline / paper_section） ----

export interface OutlineSection {
  key: string;
  level?: number;
  title: string;
  summary?: string;
  argument_points?: string[];
  cite_keys?: string[];
  kind?: 'body' | 'frame';
  grounding?: 'library' | 'user_asset';
  orphans_reclaimed?: number;
}

export interface OutlineTree {
  topic?: string;
  research_question?: string;
  language?: Language;
  paper_type?: PaperType;
  sections: OutlineSection[];
}

export interface OutlinePayload {
  project_id: string;
  outline_id?: string | null;
  version: number;
  status: 'draft' | 'confirmed';
  tree: OutlineTree;
}

/**
 * 行内强调。**结构化标记**而非 LLM 写的自由 LaTeX：渲染器据此确定性展开
 * `\textbf{}` / `\emph{}`，正文照常转义（设计 §4.5）。
 */
export type IRTextMark = 'bold' | 'italic';

/** PaperIR 行内 run（设计 §4.5：cite 是原子节点，不是正文里的字符串）。 */
export type IRRun =
  | { t: 'text'; v: string; marks?: IRTextMark[] }
  | { t: 'cite'; keys: string[] }
  | { t: 'math_inline'; v: string }
  | { t: 'xref'; target: string; kind: 'figure' };

export interface IRParagraph {
  type: 'paragraph';
  runs: IRRun[];
}

export interface IRListItem {
  runs: IRRun[];
}

/** 无序 / 有序列表；渲染为 itemize / enumerate。 */
export interface IRList {
  type: 'list';
  ordered: boolean;
  items: IRListItem[];
}

/**
 * 块级元素，逐字段对齐 packages/paper_ir/paper_ir/schema.py 的 `Block` 联合。
 *
 * 前端此前只声明了 `IRParagraph`，编辑器因此在保存时把公式/图/表/算法/todo
 * 全部抹成空段落。写作器目前只产出 paragraph，但 IR 与 LaTeX 渲染器都已支持
 * 全部六种——序列化层必须能无损往返，否则后端一开始产出结构化块就会静默丢数据。
 */
export interface IREquation {
  type: 'equation';
  latex: string;
  label?: string | null;
}

export interface IRFigure {
  type: 'figure';
  asset_ref: string;
  caption?: string;
  alt_text?: string;
  label?: string | null;
  width?: 'column' | 'full';
}

export interface IRTableSource {
  kind: 'user_asset' | 'inline';
  ref?: string | null;
  data?: {
    headers?: unknown[];
    rows?: unknown[][];
  } | null;
}

export interface IRTable {
  type: 'table';
  source: IRTableSource;
  caption?: string;
  label?: string | null;
}

export interface IRAlgorithm {
  type: 'algorithm';
  latex: string;
  label?: string | null;
}

/** 实验结果占位（纯生成模式）：不编造数据，正文明确标注待补充。 */
export interface IRTodo {
  type: 'todo';
  text: string;
}

export type IRBlock =
  | IRParagraph
  | IRList
  | IREquation
  | IRFigure
  | IRTable
  | IRAlgorithm
  | IRTodo;

/** 非段落块的类型名，编辑器按此渲染只读块视图。 */
export const IR_STRUCTURED_BLOCK_TYPES = [
  'equation',
  'figure',
  'table',
  'algorithm',
  'todo',
] as const;

export type IRStructuredBlockType = (typeof IR_STRUCTURED_BLOCK_TYPES)[number];

export interface IRCitationWarning {
  path: string;
  rejected_keys: string[];
  message: string;
}

export interface SectionIR {
  key: string;
  level: number;
  title: string;
  blocks: IRBlock[];
  citation_warnings: IRCitationWarning[];
}

export interface PaperSection {
  section_key: string;
  title: string;
  order_no: number;
  status: 'generated' | 'edited' | 'approved';
  model?: string | null;
  cite_keys: string[];
  body_ir: SectionIR | Record<string, never>;
  citation_warnings: IRCitationWarning[];
  word_count: number;
  updated_at?: string | null;
}

export interface CitationAuditRow {
  cite_key: string;
  section_id: string;
  work_id: string;
  context_snippet?: string | null;
}

export interface CitationAudit {
  project_id: string;
  whitelist_size: number;
  used_cite_keys: string[];
  /** R2 契约：必须恒为空数组。 */
  hallucinated_cite_keys: string[];
  removed_citation_warnings: Array<IRCitationWarning & { section_key?: string }>;
  unused_cite_keys: string[];
  rows: CitationAuditRow[];
}

export interface MarkdownPreview {
  project_id: string;
  markdown: string;
  word_count: number;
  document_version?: number | null;
}

// ---- 导出产物（设计 §4.3 export_artifact） ----

/**
 * `compile_log` 是编译的**副产物**而非用户请求的格式：它不出现在
 * `ExportRequest.formats` 里，但会作为产物登记以便下载——PDF 编译失败时
 * 它是用户唯一能拿到的诊断材料。
 */
export type ExportFormat =
  | 'pdf'
  | 'latex_zip'
  | 'markdown'
  | 'markdown_bundle'
  | 'bibtex'
  | 'docx'
  | 'compile_log';

/** 用户可主动勾选的导出格式（不含 compile_log）。 */
export type RequestableExportFormat = Exclude<ExportFormat, 'compile_log'>;

export interface ExportArtifact {
  id: string;
  format: ExportFormat;
  document_version?: number | null;
  object_key?: string | null;
  content_hash?: string | null;
  created_at?: string | null;
  download_url?: string | null;
  quality_report_id?: string | null;
  quality_profile?: QualityProfile;
  readiness_status?: string;
  paper_snapshot_hash?: string | null;
}

// ---- 用户素材与数字 lint（设计 §4.3 user_asset / §4.4.2 NUMLINT） ----

export type AssetKind = 'dataset' | 'result_table' | 'figure' | 'method_note' | 'code' | 'bib';

export interface UserAsset {
  id: string;
  kind: AssetKind;
  title?: string | null;
  description?: string | null;
  created_at?: string | null;
  parsed_type?: string | null;
  row_count?: number | null;
  column_count?: number | null;
  number_count: number;
  headers: string[];
  preview_rows: string[][];
  warnings: string[];
  /** 大纲/章节引用素材用的 ref（渲染期确定性展开为 booktabs / includegraphics）。 */
  asset_ref?: string | null;
}

export type VisualKind = 'chart' | 'diagram' | 'ai_image';
export type VisualGenerationStatus = 'proposed' | 'queued' | 'running' | 'ready' | 'failed';
export type VisualReviewStatus = 'pending' | 'approved' | 'rejected';

export interface VisualRendition {
  object_key: string;
  sha256: string;
  media_type: string;
  width?: number | null;
  height?: number | null;
  url: string;
}

/**
 * 统一错误词表（`packages/visuals/visuals/errors.py`）。
 *
 * 后端在读侧把历史值（`auth`、`moderation`、甚至裸的 `ValueError`）映射到这里，
 * 因此界面只需要认识这一套。
 */
export type VisualErrorCode =
  | 'provider_not_configured'
  | 'authentication_failed'
  | 'content_rejected'
  | 'invalid_request'
  | 'rate_limited'
  | 'provider_unavailable'
  | 'network_timeout'
  | 'invalid_image'
  | 'normalization_failed'
  | 'visuald_unavailable'
  | 'source_unresolved'
  | 'internal_error';

export interface VisualError {
  code: VisualErrorCode;
  /** 可执行的中文提示，直接展示。 */
  message: string;
  /** 决定给「重试」还是给「先改提示词」。 */
  retryable: boolean;
  request_id?: string | null;
  /** 脱敏的技术细节，折叠展示。 */
  detail?: string | null;
}

export interface VisualAsset {
  id: string;
  asset_ref: string;
  kind: VisualKind;
  generation_status: VisualGenerationStatus;
  review_status: VisualReviewStatus;
  title?: string | null;
  caption: string;
  caption_hint?: string | null;
  alt_text: string;
  target_section_key?: string | null;
  suggested_block_index?: number | null;
  figure_label: string;
  spec: Record<string, unknown>;
  provider?: string | null;
  model?: string | null;
  /** @deprecated 用 `error`；平铺字段只为过渡期兼容保留。 */
  error_code?: string | null;
  /** @deprecated 用 `error`。 */
  error_message?: string | null;
  error?: VisualError | null;
  renditions: Partial<Record<'svg' | 'pdf' | 'png', VisualRendition>>;
  input_hash: string;
  content_hash?: string | null;
  version: number;
  supersedes_id?: string | null;
  /** 实际输出尺寸——用户要看的是真拿到了什么，不是当初选了什么。 */
  output_width?: number | null;
  output_height?: number | null;
  /** AI 插图实际会发给图像服务商的那一句，确认框展示它。 */
  resolved_prompt?: string | null;
  paper_snapshot_hash?: string | null;
  suggestion_reason?: string | null;
  source_section_keys?: string[];
  /** 建议基于旧版正文。只提示，不自动删除。 */
  stale?: boolean;
  created_at?: string | null;
}

/** 模型补全出来的视觉草稿；用户确认后才变成真正的资产。 */
export interface VisualDraft {
  title: string;
  caption: string;
  alt_text: string;
  spec: Record<string, unknown>;
  /** `llm:<model>` 或 `deterministic`。 */
  generator: string;
}

export interface VisualSummary {
  project_id: string;
  pending: number;
  generating: number;
  ready: number;
  approved: number;
  failed: number;
  rejected: number;
  stale: number;
  /** 最近一次批准插入的时间；导出中心据此判断产物是否已过期。 */
  latest_approved_at?: string | null;
}

export interface CreateVisualRequest {
  spec: Record<string, unknown>;
  title?: string | null;
  caption?: string;
  alt_text?: string;
  target_section_key?: string | null;
  suggested_block_index?: number | null;
}

export interface NumLintFinding {
  value: string;
  section_key: string;
  context: string;
  status: string;
  source_asset?: string | null;
}

export interface NumLintReport {
  project_id: string;
  /** false 表示正文里有素材中找不到出处的数值——红线告警。 */
  consistent: boolean;
  asset_number_count: number;
  checked_count: number;
  sourced_count: number;
  unsourced_count: number;
  unsourced: NumLintFinding[];
}

// ---- 质量报告与润色（M5） ----

export interface QualityHint {
  kind: string;
  section_key?: string;
  message: string;
}

export interface SoftCheckFinding {
  cite_key: string;
  section_key: string;
  score: number;
  reason: string;
  context: string;
  weak: boolean;
}

export interface QualityReport {
  project_id: string;
  section_count: number;
  word_count: number;
  cite_count: number;
  unique_cite_count: number;
  whitelist_size: number;
  citation_density: number;
  library_coverage: number;
  recent_ratio: number;
  fulltext_coverage: number;
  sections_without_citations: string[];
  soft_check: SoftCheckFinding[];
  hints: QualityHint[];
  generated_at?: string | null;
  report_id?: string | null;
  document_version?: number | null;
  paper_snapshot_hash?: string | null;
  quality_profile: QualityProfile;
  review_style: ReviewStyle;
  readiness_status:
    | 'unassessed'
    | 'draft'
    | 'needs_revision'
    | 'preflight_ready'
    | 'submission_ready';
  stale: boolean;
  blockers: QualityIssue[];
  warnings: QualityIssue[];
  scores: Record<string, number>;
  core_claim_count: number;
  core_claim_fulltext_count: number;
  core_claim_fulltext_coverage: number;
  layout_checks: Record<string, unknown>;
}

export interface QualityIssue {
  code: string;
  message: string;
  section_keys?: string[];
  count?: number;
  [key: string]: unknown;
}

export interface ClaimEvidence {
  id: string;
  report_id: string;
  section_key: string;
  claim_text: string;
  claim_kind: string;
  is_core: boolean;
  cite_key?: string | null;
  source_kind: string;
  source_page?: number | null;
  source_section?: string | null;
  source_paragraph?: number | null;
  evidence_excerpt?: string | null;
  evidence_hash?: string | null;
  support_status: string;
  support_score?: number | null;
  manual_status: 'unreviewed' | 'confirmed' | 'rejected';
}

export type RefineAction = 'polish' | 'expand' | 'shorten' | 'academic_tone';

export interface RefineResult {
  action: string;
  original: string;
  refined: string;
  changed: boolean;
  note?: string | null;
}

// ---- 设置 / 成本 / 版本历史（M6） ----

export interface RoleModel {
  role: string;
  model: string;
  default_model: string;
  description: string;
}

/**
 * 图像提供商**真正**支持的能力。
 *
 * 界面按它渲染表单：`supported_sizes` 为空时必须显示「尺寸由提供商决定」，
 * 而不是给一个不会生效的比例下拉框（Cloudflare FLUX 只接受 prompt 与 steps）。
 */
export interface ImageProviderCapabilities {
  provider: string;
  model: string;
  supported_sizes: string[];
  supported_aspect_ratios: string[];
  quality_modes: string[];
  prompt_max_length: number;
  supports_negative_prompt: boolean;
  supports_seed: boolean;
  fixed_output_size?: string | null;
  cost_estimate_available: boolean;
  note?: string | null;
}

export interface RuntimeSettings {
  llm_provider: string;
  /** 密钥永不回传，只报告是否已配置。 */
  llm_api_key_configured: boolean;
  llm_enabled: boolean;
  roles: RoleModel[];
  scholar_contact_email_configured: boolean;
  semantic_scholar_key_configured: boolean;
  storage_backend: string;
  visuals_enabled: boolean;
  ai_images_enabled: boolean;
  image_provider: string;
  image_model: string;
  image_api_key_configured: boolean;
  image_provider_configured: boolean;
  image_capabilities?: ImageProviderCapabilities | null;
}

export interface AuthUser {
  id: string;
  email: string;
  display_name?: string | null;
  email_verified: boolean;
}

export interface CostByRole {
  role: string;
  model: string;
  call_count: number;
  failed_call_count?: number;
  input_tokens: number;
  output_tokens: number;
  cost_estimate: number;
  avg_latency_ms: number;
}

export interface CostDetail {
  project_id: string;
  totals: ProjectCost;
  by_role: CostByRole[];
  images?: {
    call_count: number;
    failed_call_count: number;
    cost_estimate: number;
    by_size?: Array<{
      provider: string;
      model?: string | null;
      width?: number | null;
      height?: number | null;
      call_count: number;
      failed_call_count: number;
      cost_estimate: number;
    }>;
  };
}

export interface DocumentVersion {
  id: string;
  version: number;
  status: string;
  section_count?: number | null;
  is_current: boolean;
  created_at?: string | null;
}

export interface OutlineVersion {
  id: string;
  version: number;
  status: string;
  section_count: number;
  created_at?: string | null;
}

export interface VersionHistory {
  project_id: string;
  outlines: OutlineVersion[];
  documents: DocumentVersion[];
}
