'use client';

import * as React from 'react';
import { getScope, listAssets, listExports, listSections } from './api';
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
  loading: boolean;
}

export const EMPTY_PROGRESS: ProjectProgress = {
  hasScope: false,
  scopeIsFallback: false,
  assetCount: 0,
  libraryCount: 0,
  sectionCount: 0,
  wordCount: 0,
  exportCount: 0,
  hasPdf: false,
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
    Promise.all([
      getScope(projectId),
      listSections(projectId),
      listExports(projectId),
      wantAssets ? listAssets(projectId) : Promise.resolve({ data: [] as unknown[] }),
    ])
      .then(([scope, sections, exports, assets]) => {
        if (!alive) return;
        const scopeData = scope.data;
        const generator = String(scopeData?.generator ?? '');
        setProgress({
          hasScope: Boolean(scopeData && Object.keys(scopeData).length > 0),
          scopeIsFallback: generator.startsWith('deterministic'),
          assetCount: (assets.data as unknown[]).length,
          libraryCount,
          sectionCount: sections.data.length,
          wordCount: sections.data.reduce((sum, s) => sum + (s.word_count ?? 0), 0),
          exportCount: exports.data.length,
          hasPdf: exports.data.some((a) => a.format === 'pdf'),
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
