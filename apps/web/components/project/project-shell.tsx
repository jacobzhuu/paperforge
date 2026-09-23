'use client';

import * as React from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { DataSourceBanner } from '@/components/data-source-banner';
import { JobProgressCard } from '@/components/jobs/job-progress-card';
import { Button } from '@/components/ui/button';
import { Callout } from '@/components/ui/callout';
import { Play } from 'lucide-react';
import { LoadState } from '@/components/layout/load-state';
import { ModuleError } from '@/components/layout/module-error';
import { useToast } from '@/components/ui/toast';
import { describeError } from '@/lib/errors';
import { ProjectHeader } from './project-header';
import { ProjectPipelineNav } from './project-pipeline-nav';
import { ProjectProvider, useProject } from './project-context';
import { stageLabel } from '@/lib/labels';
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
    whitelistError,
    reloadWhitelist,
    progress,
    tracked,
    jobMessage,
    retryStage,
    skipPolish,
    cancelJob,
    pauseJob,
    pausedJob,
    resumePausedJob,
  } = useProject();
  const { toast } = useToast();
  const [dismissedPause, setDismissedPause] = React.useState<string | null>(null);
  const pathname = usePathname() ?? '';
  const current = stepFromPathname(pathname, projectId);
  const completion = stepCompletion(progress);

  const handleRetryStage = async (stage: Parameters<typeof retryStage>[0]) => {
    try {
      await retryStage(stage);
      toast({ title: '已开始重跑', description: `${stageLabel(stage)}正在重新执行。` });
    } catch (err) {
      toast({ title: '重跑未能启动', description: describeError(err), variant: 'error' });
    }
  };
  const runningStep = stepForStage(tracked?.job.stage);

  const resume = async () => {
    try {
      await resumePausedJob();
    } catch (err) {
      toast({ title: '继续未能启动', description: describeError(err), variant: 'error' });
    }
  };

  /**
   * 放弃续跑：只收起横幅。任务本身已经是终态（paused），不需要再调后端；
   * 它留在任务历史里，已生成的内容也一件不删——用户放弃的是「接着跑」，不是产物。
   */
  const discardPaused = () => setDismissedPause(pausedJob?.id ?? null);

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

        <ModuleError
          label="引用白名单"
          error={whitelistError}
          onRetry={reloadWhitelist}
        />

        {/* 任务条挂在 shell 上：切换工作台时进度、计时与已收集的降级警告都不丢。 */}
        {tracked && (
          <JobProgressCard
            tracked={tracked}
            onRetryStage={(stage) => void handleRetryStage(stage)}
            onSkipPolish={() => void skipPolish(tracked.job.id)}
            onPause={() => void pauseJob(tracked.job.id)}
            onCancel={() => void cancelJob(tracked.job.id)}
          />
        )}

        {/* 暂停的任务已经离开进度条（paused 是终态），不给一条常驻横幅的话，
            用户点完暂停就再也找不到「继续」的入口了。 */}
        {!tracked && pausedJob && pausedJob.id !== dismissedPause && (
          <Callout
            role="status"
            className="flex flex-wrap items-center justify-between gap-3"
          >
            <span>
              <span className="font-medium">任务已暂停</span>
              <span className="ml-1.5 text-muted-foreground">
                停在「{stageLabel(pausedJob.stage)}」；已生成的内容都在，继续会跳过已完成的阶段。
              </span>
            </span>
            <span className="flex shrink-0 items-center gap-2">
              {pausedJob.checkpoint?.repair_interrupt ? (
                <Link className="text-sm underline" href={`/projects/${projectId}/jobs/${pausedJob.id}`}>查看待补充材料</Link>
              ) : <Button size="xs" onClick={resume}>
                <Play className="h-3 w-3" />
                继续
              </Button>}
              <Button
                variant="ghost"
                size="xs"
                onClick={discardPaused}
              >
                放弃
              </Button>
            </span>
          </Callout>
        )}

        {jobMessage && (
          <Callout
            variant="warning"
            role="status"
          >
            {jobMessage}
          </Callout>
        )}

        <LoadState loading={loading && !project} error={error} onRetry={reload} skeletonClassName="h-64">
          {children}
        </LoadState>
      </div>
    </div>
  );
}
