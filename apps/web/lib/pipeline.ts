import type { PaperType } from './types';

/**
 * 管线步骤定义。
 *
 * 取代旧的 `lib/project-nav.ts::PIPELINE_STEPS`——那份是**两种论文共用的一条定序**，
 * 且把「素材中心」排在「写作」之后。研究型论文的素材是写作的**输入**
 * （docs/design.md §4.4.2：INPUT → SEARCH+ → WRITE → NUMLINT），排在写作之后
 * 等于让作者写完正文才被邀请上传那些本该为正文数字接地的数据。
 *
 * 这里改为按 paper_type 返回不同序列，并让综述论文根本不出现素材步骤。
 */
export type PipelineStepId =
  | 'overview'
  | 'scope'
  | 'assets'
  | 'library'
  | 'outline'
  | 'write'
  | 'export';

export interface PipelineStep {
  id: PipelineStepId;
  /** 相对项目根的子路径，'' 表示项目概览本身。 */
  segment: string;
  label: string;
  /** 一句话说明这一步在做什么，用于导航 tooltip 与概览页。 */
  hint: string;
}

/** 各步骤是否已有产物，用于导航状态点。 */
export type PipelineProgressMap = Record<PipelineStepId, boolean>;

const STEP: Record<PipelineStepId, PipelineStep> = {
  overview: { id: 'overview', segment: '', label: '概览', hint: '项目全貌与下一步建议' },
  scope: { id: 'scope', segment: 'scope', label: '研究范围', hint: '关键词组与研究问题，驱动检索' },
  assets: { id: 'assets', segment: 'assets', label: '素材中心', hint: '结果表格/图/笔记——正文数字的唯一出处' },
  library: { id: 'library', segment: 'library', label: '文献工作台', hint: '检索、分诊、入库核验' },
  outline: { id: 'outline', segment: 'outline', label: '大纲编辑器', hint: '章节结构与文献分配' },
  write: { id: 'write', segment: 'write', label: '写作工作台', hint: '正文起草与修订' },
  export: { id: 'export', segment: 'export', label: '导出中心', hint: 'LaTeX / PDF / docx 产物' },
};

const REVIEW_FLOW: PipelineStepId[] = ['overview', 'scope', 'library', 'outline', 'write', 'export'];

// 研究型论文：素材先行（设计 §4.4.2 INPUT 是第一步）。
const ORIGINAL_FLOW: PipelineStepId[] = [
  'overview',
  'assets',
  'scope',
  'library',
  'outline',
  'write',
  'export',
];

export function pipelineSteps(paperType: PaperType | undefined): PipelineStep[] {
  const flow = paperType === 'original' ? ORIGINAL_FLOW : REVIEW_FLOW;
  return flow.map((id) => STEP[id]);
}

/** 工作台步骤（不含概览），用于「上一步 / 下一步」。 */
export function workbenchSteps(paperType: PaperType | undefined): PipelineStep[] {
  return pipelineSteps(paperType).filter((s) => s.id !== 'overview');
}

export function projectHref(projectId: string, segment: string): string {
  return segment ? `/projects/${projectId}/${segment}` : `/projects/${projectId}`;
}

export function pipelineNeighbors(
  current: PipelineStepId,
  paperType: PaperType | undefined,
): { prev?: PipelineStep; next?: PipelineStep } {
  const steps = workbenchSteps(paperType);
  const index = steps.findIndex((s) => s.id === current);
  if (index < 0) return {};
  return {
    prev: index > 0 ? steps[index - 1] : undefined,
    next: index < steps.length - 1 ? steps[index + 1] : undefined,
  };
}

/**
 * worker 阶段名 → 管线步骤。用于在导航上标出「现在哪一步在跑」。
 * 阶段集合见 services/worker/paperforge_worker/worker.py::_STAGE_PROGRESS。
 */
const STAGE_TO_STEP: Record<string, PipelineStepId> = {
  scope: 'scope',
  search: 'library',
  curate: 'library',
  ingest: 'library',
  snowball: 'library',
  cards: 'library',
  import: 'library',
  import_doi: 'library',
  import_bibtex: 'library',
  outline: 'outline',
  write: 'write',
  citecheck: 'write',
  quality: 'write',
  render: 'export',
};

export function stepForStage(stage: string | null | undefined): PipelineStepId | null {
  if (!stage) return null;
  return STAGE_TO_STEP[stage] ?? null;
}

/** 从路径末段推断当前步骤，供布局高亮导航。 */
export function stepFromPathname(pathname: string, projectId: string): PipelineStepId {
  const base = `/projects/${projectId}`;
  if (!pathname.startsWith(base)) return 'overview';
  const rest = pathname.slice(base.length).replace(/^\//, '').split('/')[0] ?? '';
  const match = (Object.keys(STEP) as PipelineStepId[]).find((id) => STEP[id].segment === rest);
  return match ?? 'overview';
}
