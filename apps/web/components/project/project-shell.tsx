'use client';

import * as React from 'react';
import { usePathname } from 'next/navigation';
import { DataSourceBanner } from '@/components/data-source-banner';
import { JobProgressCard, type RetryableStage } from '@/components/jobs/job-progress-card';
import { LoadState } from '@/components/layout/load-state';
import { useToast } from '@/components/ui/toast';
import {
  generateCards,
  generateOutline,
  generateQuality,
  generateSections,
  startExport,
  startIngest,
  startSearch,
  startSnowball,
} from '@/lib/api';
import { describeError } from '@/lib/errors';
import type { Job } from '@/lib/types';
import { ProjectHeader } from './project-header';
import { ProjectPipelineNav } from './project-pipeline-nav';
import { ProjectProvider, useProject } from './project-context';
import { stepForStage, stepFromPathname } from '@/lib/pipeline';
import { stepCompletion } from '@/lib/useProjectProgress';
import type { VisualSummary } from '@/lib/types';

/** 导航 tooltip：把四个状态一次说清，省得用户点进去才知道有没有事要做。 */
function describeVisualBadge(summary: VisualSummary): string {
  const parts: string[] = [];
  if (summary.pending > 0) parts.push(`${summary.pending} 条待生成`);
  if (summary.generating > 0) parts.push(`${summary.generating} 张生成中`);
  if (summary.ready > 0) parts.push(`${summary.ready} 张可批准`);
  if (summary.failed > 0) parts.push(`${summary.failed} 张生成失败`);
  if (summary.approved > 0) parts.push(`${summary.approved} 张已插入`);
  return parts.length > 0 ? parts.join('，') : '图表、示意图与 AI 插图';
}

/** 项目工作区外壳：项目头 + 管线导航 + 跨页任务条。所有工作台共用。 */
export function ProjectShell({
  projectId,
  children,
}: {
  projectId: string;
  children: React.ReactNode;
}) {
  return (
    <ProjectProvider projectId={projectId}>
      <ShellBody>{children}</ShellBody>
    </ProjectProvider>
  );
}

function ShellBody({ children }: { children: React.ReactNode }) {
  const {
    projectId,
    project,
    paperType,
    loading,
    error,
    source,
    note,
    reload,
    progress,
    tracked,
    jobMessage,
    startJob,
    skipPolish,
  } = useProject();
  const { toast } = useToast();
  const pathname = usePathname() ?? '';
  const current = stepFromPathname(pathname, projectId);
  const completion = stepCompletion(progress);

  /**
   * 单阶段重跑。
   *
   * 只覆盖有独立端点的阶段——curate / citecheck 没有单独的触发入口，
   * 那些阶段不给按钮，而不是给一个点了没反应的。
   */
  const retryStage = async (stage: RetryableStage) => {
    const starters: Record<RetryableStage, () => Promise<{ data: Job | undefined }>> = {
      search: () => startSearch(projectId, {}),
      ingest: () => startIngest(projectId),
      snowball: () => startSnowball(projectId, 'both'),
      cards: () => generateCards(projectId),
      quality: () => generateQuality(projectId),
      outline: () => generateOutline(projectId),
      write: () => generateSections(projectId, true),
      render: () => startExport(projectId),
    };
    try {
      const started = await starters[stage]();
      startJob(started.data, '后端不可用：无法重跑该阶段');
    } catch (err) {
      toast({ title: '重跑未能启动', description: describeError(err), variant: 'error' });
    }
  };
  const runningStep = stepForStage(tracked?.job.stage);

  /**
   * 视觉步骤的导航状态：待处理条数 + 失败告警。
   *
   * 数据来自专门的 summary 接口，不是完整视觉列表——导航上的一个数字不该
   * 让浏览器把每张图的 spec 和 rendition 元数据都下载一遍。
   */
  const visualSummary = progress.visuals;
  const actionableVisuals = visualSummary.pending + visualSummary.ready;
  const badges = {
    visuals: {
      count: actionableVisuals || undefined,
      alert: visualSummary.failed > 0,
      title: describeVisualBadge(visualSummary),
    },
  };

  // 写作台自行接管全宽布局；其余页面维持居中阅读宽度。
  const wide = current === 'write' || current === 'library';

  return (
    <div className={wide ? 'mx-auto w-full max-w-[1600px] px-4 sm:px-6 lg:px-8' : 'mx-auto w-full max-w-6xl px-4 sm:px-6 lg:px-8'}>
      <div className="space-y-4 py-6 lg:py-8">
        {/* 改名成功后重拉项目：题目会出现在项目切换器、导出文件名与 LaTeX 标题里。 */}
        <ProjectHeader project={project} onRenamed={reload} />
        <ProjectPipelineNav
          projectId={projectId}
          paperType={paperType}
          // 项目还没载入时按协作模式渲染：让步骤可见的代价，比让用户短暂
          // 失去导航要小。
          writingMode={project?.writing_mode ?? 'assisted'}
          current={current}
          completion={completion}
          running={runningStep}
          badges={badges}
        />

        <DataSourceBanner source={source} note={note} />

        {/* 任务条挂在 shell 上：切换工作台时进度、计时与已收集的降级警告都不丢。 */}
        {tracked && (
          <JobProgressCard
            tracked={tracked}
            onRetryStage={retryStage}
            onSkipPolish={() => void skipPolish(tracked.job.id)}
          />
        )}

        {jobMessage && (
          <div
            role="status"
            className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning-foreground"
          >
            {jobMessage}
          </div>
        )}

        <LoadState loading={loading && !project} error={error} onRetry={reload} skeletonClassName="h-64">
          {children}
        </LoadState>
      </div>
    </div>
  );
}
