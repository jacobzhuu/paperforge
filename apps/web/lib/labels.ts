import type {
  AddedVia,
  CitationStyle,
  JobStatus,
  Language,
  PaperType,
  ProjectStatus,
  WritingMode,
} from './types';

export const PAPER_TYPE_LABEL: Record<PaperType, string> = {
  review: '综述论文',
  original: '研究型论文',
};

export const WRITING_MODE_LABEL: Record<WritingMode, string> = {
  auto: '全自动',
  assisted: '协作',
};

export const LANGUAGE_LABEL: Record<Language, string> = {
  zh: '中文',
  en: 'English',
};

export const CITATION_STYLE_LABEL: Record<CitationStyle, string> = {
  author_year: '作者-年份',
  gbt7714: 'GB/T 7714',
  ieee: 'IEEE',
  apa: 'APA',
};

export const STATUS_LABEL: Record<ProjectStatus, string> = {
  draft: '草稿',
  scoping: '范围拟定',
  searching: '检索中',
  curating: '文献筛选',
  writing: '写作中',
  rendering: '编译渲染',
  review: '人工评审',
  done: '已完成',
};

export const STATUS_VARIANT: Record<
  ProjectStatus,
  'default' | 'secondary' | 'success' | 'warning' | 'muted'
> = {
  draft: 'muted',
  scoping: 'secondary',
  searching: 'warning',
  curating: 'warning',
  writing: 'default',
  rendering: 'secondary',
  review: 'warning',
  done: 'success',
};

/**
 * 后端阶段名 → 中文标签。四个工作台共用，避免在全中文界面里露出 `render`、`write`。
 * 阶段集合见 worker.py::_STAGE_PROGRESS。
 */
export const STAGE_LABEL: Record<string, string> = {
  web_research: '补充网页资料',
  scope: '生成研究范围',
  qdecomp: '分解研究子问题',
  search: '多源检索与去重',
  screen: '筛选合格文献',
  curate: '分配引用 key',
  ingest: '获取并解析 OA 全文',
  snowball: '引文雪球扩展',
  cards: '抽取文献卡片',
  evidence: '抽取证据单元与数值',
  qmatrix: '构建问题—证据矩阵',
  synth: '判定一致、冲突与缺口',
  quality: '生成质量报告',
  repair_search: '补充检索缺口证据',
  repair_ingest: '获取补充文献全文',
  quality_repair: '重写未达标章节',
  quality_recheck: '复核学术严谨门',
  outline: '生成大纲',
  write: '分节写作',
  polish: '连贯性润色',
  'polish.started': '开始润色',
  'polish.completed': '润色完成',
  'polish.section': '章节润色结果',
  'polish.policy': '润色策略',
  'polish.skipped': '跳过剩余润色',
  citecheck: '引用越权校验',
  visual_plan: '生成视觉建议',
  visual_generate: '生成图表与插图',
  render: '编译 LaTeX / PDF',
  import: '反查核验导入文献',
  import_doi: '反查核验 DOI',
  import_bibtex: '反查核验 BibTeX 条目',
  done: '完成',
};

export function stageLabel(stage?: string | null): string {
  if (!stage) return '排队中';
  return STAGE_LABEL[stage] ?? stage;
}

/** 任务状态 → 中文。paused 是「这一轮停了、可从断点继续」，与 cancelled 不同。 */
export const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  queued: '排队中',
  running: '进行中',
  paused: '已暂停',
  succeeded: '已完成',
  failed: '已失败',
  cancelled: '已取消',
  needs_input: '需补充材料',
};

/**
 * 文献库动作术语表。
 *
 * 「入库 / 导入 / 摄取」此前作为三个近义词散落在同一个页面上，措辞还各处不一
 * （「圈选入库」「移出文献库」「获取 OA 全文」「跑通全管线」）。它们其实是
 * **三件不同的事**，所以要各自有唯一且互不重叠的说法：
 *
 * - **纳入写作**：把候选文献标为 selected，进而进入 R1 写作白名单。可逆。
 * - **导入**：从 DOI / BibTeX 新增文献（需经 R1 反查核验）。
 * - **获取 OA 全文**：抓取并解析开放获取全文（后端阶段名 ingest）。
 *   界面上一律不说「摄取」——那是管线示意里的书面语，不作为动作标签。
 *
 * 「移出文献库」专指**删除条目**（不可逆），与「取消纳入」严格区分。
 */
export const LIBRARY_ACTION = {
  select: '纳入写作',
  deselect: '取消纳入',
  selected: '已纳入',
  candidate: '候选',
  uncertain: '待复核',
  excluded: '已排除',
  bulkSelect: '批量纳入',
  bulkExclude: '批量排除',
  /** 真删除，不是取消入库。 */
  remove: '移出文献库',
  add: '添加文献',
  uploadPdf: '上传 PDF',
  importDoi: '输入 DOI',
  importBibtex: '粘贴 BibTeX',
  import: '导入 DOI / BibTeX',
  fulltext: '获取 OA 全文',
  snowball: '雪球扩展',
  cards: '生成卡片',
  search: '搜索添加',
  runAll: '跑通全管线',
} as const;

export const ADDED_VIA_LABEL: Record<AddedVia, string> = {
  mcp_web_verified: '网页发现（已核验）',
  search: '检索',
  snowball: '雪球',
  doi_import: 'DOI 导入',
  bibtex_import: 'BibTeX 导入',
  pdf_upload: 'PDF 上传',
  llm_suggested_verified: 'LLM 建议(已核验)',
};

export const EVIDENCE_GRADE_LABEL: Record<string, string> = {
  A_located_structured: 'A · 结构化定位',
  B_located_prose: 'B · 正文定位',
  C_fulltext_unlocated: 'C · 全文未定位',
  D_abstract_only: 'D · 仅摘要',
};

export const EVIDENCE_STANCE_LABEL: Record<string, string> = {
  supports: '支持',
  contradicts: '反驳',
  conditional: '条件成立',
  not_comparable: '不可比较',
  gap: '证据缺口',
};

export const ANCHOR_STRENGTH_LABEL: Record<string, string> = {
  structured_cell: '表格/公式单元',
  object_mention: '图表提及',
  prose_only: '仅正文叙述',
};

export const ANSWER_STATUS_LABEL: Record<string, string> = {
  answered: '已回答',
  partial: '部分回答',
  contested: '存在争议',
  insufficient_evidence: '证据不足',
};

export const ANSWER_STATUS_VARIANT: Record<
  string,
  'default' | 'secondary' | 'success' | 'warning' | 'muted' | 'destructive'
> = {
  answered: 'success',
  partial: 'warning',
  contested: 'destructive',
  insufficient_evidence: 'muted',
};

export function evidenceGradeLabel(grade: string): string {
  return EVIDENCE_GRADE_LABEL[grade] ?? grade;
}

export function evidenceStanceLabel(stance: string): string {
  return EVIDENCE_STANCE_LABEL[stance] ?? stance;
}

export function anchorStrengthLabel(strength: string | null | undefined): string {
  if (!strength) return '未标注';
  return ANCHOR_STRENGTH_LABEL[strength] ?? strength;
}

export function answerStatusLabel(status: string): string {
  return ANSWER_STATUS_LABEL[status] ?? status;
}

export const VENUE_TEMPLATES = [
  { id: 'article', label: '通用 article（无格式）' },
  { id: 'IEEEtran', label: 'IEEE（IEEEtran）' },
  { id: 'acmart', label: 'ACM（acmart）' },
  { id: 'elsarticle', label: 'Elsevier（elsarticle）' },
  { id: 'llncs', label: 'Springer（llncs）' },
  { id: 'cn_thesis', label: '中文学位论文/学报' },
];
