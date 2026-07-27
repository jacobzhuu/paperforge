import type {
  AddedVia,
  CitationStyle,
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
  scope: '生成研究范围',
  search: '五源检索与去重',
  curate: '分配引用 key',
  ingest: '获取并解析 OA 全文',
  snowball: '引文雪球扩展',
  cards: '抽取文献卡片',
  quality: '生成质量报告',
  outline: '生成大纲',
  write: '分节写作',
  polish: '连贯性润色',
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

/**
 * 文献库动作术语表。
 *
 * 「入库 / 导入 / 摄取」此前作为三个近义词散落在同一个页面上，措辞还各处不一
 * （「圈选入库」「移出文献库」「获取 OA 全文」「跑通全管线」）。它们其实是
 * **三件不同的事**，所以要各自有唯一且互不重叠的说法：
 *
 * - **入库**：把候选文献标为 selected，进而进入 R1 写作白名单。可逆。
 * - **导入**：从 DOI / BibTeX 新增文献（需经 R1 反查核验）。
 * - **获取 OA 全文**：抓取并解析开放获取全文（后端阶段名 ingest）。
 *   界面上一律不说「摄取」——那是管线示意里的书面语，不作为动作标签。
 *
 * 「移出文献库」专指**删除条目**（不可逆），与「取消入库」严格区分。
 */
export const LIBRARY_ACTION = {
  select: '圈选入库',
  deselect: '取消入库',
  selected: '已入库',
  candidate: '候选',
  excluded: '已排除',
  bulkSelect: '批量入库',
  bulkExclude: '批量排除',
  /** 真删除，不是取消入库。 */
  remove: '移出文献库',
  import: '导入 DOI / BibTeX',
  fulltext: '获取 OA 全文',
  snowball: '雪球扩展',
  cards: '生成卡片',
  search: '触发检索',
  runAll: '跑通全管线',
} as const;

export const ADDED_VIA_LABEL: Record<AddedVia, string> = {
  search: '检索',
  snowball: '雪球',
  doi_import: 'DOI 导入',
  bibtex_import: 'BibTeX 导入',
  llm_suggested_verified: 'LLM 建议(已核验)',
};

export const VENUE_TEMPLATES = [
  { id: 'article', label: '通用 article（无格式）' },
  { id: 'IEEEtran', label: 'IEEE（IEEEtran）' },
  { id: 'acmart', label: 'ACM（acmart）' },
  { id: 'elsarticle', label: 'Elsevier（elsarticle）' },
  { id: 'llncs', label: 'Springer（llncs）' },
  { id: 'cn_thesis', label: '中文学位论文/学报' },
];
