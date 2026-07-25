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
