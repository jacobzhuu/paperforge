'use client';

import * as React from 'react';
import { getScope, getVisualSummary, listAssets, listExports, listSections } from './api';
import type { VisualSummary } from './types';
import type { PaperType } from './types';
import type { PipelineProgressMap, PipelineStepId } from './pipeline';

/**
 * 项目进度推断。
 *
 * **不使用 `project.status`**：`packages/db/db/repositories/projects.py::set_project_status`
 * 虽已实现却在全仓库没有任何调用方，所有项目永远停在 'draft'——那个字段目前是假的。
 * 这里改为按「实际存在的产物」推断进度，与 draft-first 的语义也更一致：
 * 用户关心的是「我现在有什么」，不是「流程被标成了哪一步」。
 */
export interface ProjectProgress {
  hasScope: boolean;
  /** scope 是 LLM 不可用时的确定性回退，检索前会被自动重生成。 */
  scopeIsFallback: boolean;
  assetCount: number;
  libraryCount: number;
  sectionCount: number;
  wordCount: number;
  exportCount: number;
  hasPdf: boolean;
  /**
   * 视觉状态计数。走专门的 summary 接口而不是拉完整视觉列表——导航上的一个
   * 状态点不该让浏览器把每张图的 spec 与 rendition 元数据都下载一遍。
   */
  visuals: VisualSummary;
  approvedVisualCount: number;
  loading: boolean;
}

const EMPTY_VISUALS: VisualSummary = {
  project_id: '',
  pending: 0,
  generating: 0,
  ready: 0,
  approved: 0,
  failed: 0,
  rejected: 0,
  stale: 0,
  latest_approved_at: null,
};

export const EMPTY_PROGRESS: ProjectProgress = {
  hasScope: false,
  scopeIsFallback: false,
  assetCount: 0,
  libraryCount: 0,
  sectionCount: 0,
  wordCount: 0,
  exportCount: 0,
  hasPdf: false,
  visuals: EMPTY_VISUALS,
  approvedVisualCount: 0,
  loading: true,
};

export function useProjectProgress(
  projectId: string,
  paperType: PaperType,
  libraryCount: number,
  reloadToken: number,
): ProjectProgress {
  const [progress, setProgress] = React.useState<ProjectProgress>(EMPTY_PROGRESS);

  React.useEffect(() => {
    if (!projectId) {
      setProgress({ ...EMPTY_PROGRESS, loading: false });
      return;
    }
    let alive = true;
    const wantAssets = paperType === 'original';
    /*
     * allSettled 而不是 all：这里是**导航状态点**的数据源。一个接口 500 不该让
     * 整条管线的进度点全部熄灭——那会让用户以为自己什么都还没做。
     * 单项失败就按「这一项没有产物」处理，其余照常显示。
     */
    Promise.allSettled([
      getScope(projectId),
      listSections(projectId),
      listExports(projectId),
      wantAssets ? listAssets(projectId) : Promise.resolve({ data: [] as unknown[] }),
      getVisualSummary(projectId),
    ])
      .then(([scope, sections, exports, assets, visuals]) => {
        if (!alive) return;
        const scopeData = scope.status === 'fulfilled' ? scope.value.data : undefined;
        const sectionRows = sections.status === 'fulfilled' ? sections.value.data : [];
        const exportRows = exports.status === 'fulfilled' ? exports.value.data : [];
        const assetRows =
          assets.status === 'fulfilled' ? (assets.value.data as unknown[]) : [];
        const visualSummary =
          visuals.status === 'fulfilled' ? visuals.value.data : EMPTY_VISUALS;
        const generator = String(scopeData?.generator ?? '');
        setProgress({
          hasScope: Boolean(scopeData && Object.keys(scopeData).length > 0),
          scopeIsFallback: generator.startsWith('deterministic'),
          assetCount: assetRows.length,
          libraryCount,
          sectionCount: sectionRows.length,
          wordCount: sectionRows.reduce((sum, s) => sum + (s.word_count ?? 0), 0),
          exportCount: exportRows.length,
          hasPdf: exportRows.some((a) => a.format === 'pdf'),
          visuals: visualSummary,
          approvedVisualCount: visualSummary.approved,
          loading: false,
        });
      })
      .catch(() => {
        // 进度推断失败不该阻断任何页面——退化为「什么都还没有」。
        if (alive) setProgress({ ...EMPTY_PROGRESS, libraryCount, loading: false });
      });
    return () => {
      alive = false;
    };
  }, [projectId, paperType, libraryCount, reloadToken]);

  return progress;
}

/** 每个管线步骤是否已有产物，用于导航状态点。 */
export function stepCompletion(progress: ProjectProgress): PipelineProgressMap {
  return {
    overview: true,
    scope: progress.hasScope,
    assets: progress.assetCount > 0,
    library: progress.libraryCount > 0,
    outline: progress.sectionCount > 0 || progress.libraryCount > 0,
    write: progress.sectionCount > 0,
    // 「有产物」= 至少插了一张图。有待处理建议不算完成——那正是需要用户去做的事。
    visuals: progress.approvedVisualCount > 0,
    export: progress.exportCount > 0,
  };
}

/** 概览页「下一步做什么」的推断。返回步骤 id 与一句话理由。 */
export function nextAction(
  progress: ProjectProgress,
  paperType: PaperType,
): { step: PipelineStepId; label: string; reason: string } {
  if (paperType === 'original' && progress.assetCount === 0) {
    return {
      step: 'assets',
      label: '上传素材',
      reason: '研究型论文的正文数字必须来自素材。没有素材也可以继续——实验结果处会写占位符，系统不会编造数值。',
    };
  }
  if (!progress.hasScope) {
    return {
      step: 'scope',
      label: '生成研究范围',
      reason: '研究范围决定检索用的关键词组，是检索质量的源头。',
    };
  }
  if (progress.libraryCount === 0) {
    return {
      step: 'library',
      label: '触发检索',
      reason: '还没有入库文献。检索会从五个学术源取回候选并做 R1 核验。',
    };
  }
  if (progress.sectionCount === 0) {
    return {
      step: 'outline',
      label: '生成大纲',
      reason: `已有 ${progress.libraryCount} 篇入库文献，可以按卡片聚类出章节树了。`,
    };
  }
  // 视觉是可选阶段：只有确实有待处理建议时才推荐它，绝不把它变成导出前的闸门。
  if (progress.visuals.ready > 0 || progress.visuals.pending > 0) {
    const actionable = progress.visuals.ready + progress.visuals.pending;
    return {
      step: 'visuals',
      label: '处理视觉建议',
      reason: `有 ${actionable} 条视觉建议待处理。可以逐条批准插入，也可以全部忽略直接导出——它不阻塞任何后续步骤。`,
    };
  }
  if (progress.exportCount === 0) {
    return {
      step: 'export',
      label: '生成导出产物',
      reason: `已有 ${progress.sectionCount} 节共 ${progress.wordCount.toLocaleString()} 字正文，可以编译成 PDF 了。`,
    };
  }
  return {
    step: 'write',
    label: '继续打磨正文',
    reason: '全流程已跑通。可以逐节修订，改完重新导出。',
  };
}
