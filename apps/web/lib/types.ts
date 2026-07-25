// PaperForge 域模型（前端投影，对应 docs/design.md §4.3 / §4.7）。

export type PaperType = 'review' | 'original';
export type WritingMode = 'auto' | 'assisted';
export type Language = 'zh' | 'en';
export type CitationStyle = 'author_year' | 'gbt7714' | 'ieee' | 'apa';

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
  quotable_points: string[];
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

export type JobKind = 'search' | 'ingest' | 'cards' | 'outline' | 'write' | 'compile' | 'full';
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

/** SSE 具名事件类型（后端 worker 发出的阶段事件）。 */
export const JOB_EVENT_TYPES = [
  'scope.started',
  'scope.completed',
  'scope.failed',
  'search.started',
  'search.deduped',
  'search.completed',
  'search.failed',
  'curate.started',
  'curate.completed',
  'curate.failed',
  'cards.started',
  'cards.progress',
  'cards.completed',
  'cards.failed',
  'import.started',
  'import.verified',
  'import.rejected',
  'import.completed',
  'import.failed',
  'job.finished',
] as const;

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

/** PaperIR 行内 run（设计 §4.5：cite 是原子节点，不是正文里的字符串）。 */
export type IRRun =
  | { t: 'text'; v: string }
  | { t: 'cite'; keys: string[] }
  | { t: 'math_inline'; v: string };

export interface IRParagraph {
  type: 'paragraph';
  runs: IRRun[];
}

export interface IRCitationWarning {
  path: string;
  rejected_keys: string[];
  message: string;
}

export interface SectionIR {
  key: string;
  level: number;
  title: string;
  blocks: IRParagraph[];
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

export type ExportFormat = 'pdf' | 'latex_zip' | 'markdown' | 'bibtex' | 'docx';

export interface ExportArtifact {
  id: string;
  format: ExportFormat;
  document_version?: number | null;
  object_key?: string | null;
  content_hash?: string | null;
  created_at?: string | null;
  download_url?: string | null;
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

export interface RuntimeSettings {
  llm_provider: string;
  llm_base_url: string;
  /** 密钥永不回传，只报告是否已配置。 */
  llm_api_key_configured: boolean;
  llm_enabled: boolean;
  roles: RoleModel[];
  scholar_contact_email?: string | null;
  semantic_scholar_key_configured: boolean;
  storage_backend: string;
  texd_url: string;
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
