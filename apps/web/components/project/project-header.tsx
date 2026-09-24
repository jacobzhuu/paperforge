'use client';

import * as React from 'react';
import Link from 'next/link';
import { BookOpen, ChevronLeft, FlaskConical, SlidersHorizontal } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Dialog } from '@/components/ui/dialog';
import { FastDraftToggle } from '@/components/ui/fast-draft-toggle';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import { getSubmissionReadiness, updateProject } from '@/lib/api';
import { describeError } from '@/lib/errors';
import { CITATION_STYLE_LABEL, LANGUAGE_LABEL, PAPER_TYPE_LABEL, VENUE_TEMPLATES, WRITING_MODE_LABEL } from '@/lib/labels';
import type { Job, Project, SubmissionReadiness } from '@/lib/types';
import { useAsyncModule } from '@/lib/useAsyncModule';
import { ProjectTitle } from './project-title';

/**
 * 项目头。
 *
 * 刻意**不显示 `project.status`**：那个字段永远是 'draft'
 * （`set_project_status` 无调用方），显示它等于对用户撒谎。真实进度由
 * 管线导航的状态点与概览页的产物计数承担。
 */
export function ProjectHeader({
  project,
  onRenamed,
  onProfileChanged,
  activeJob,
}: {
  project: Project | undefined;
  activeJob?: Job;
  onRenamed?: (title: string) => void;
  onProfileChanged?: () => void;
}) {
  const [settingsOpen, setSettingsOpen] = React.useState(false);
  const [savingProfile, setSavingProfile] = React.useState(false);
  const { toast } = useToast();
  const projectId = String(project?.id ?? '');
  const readinessModule = useAsyncModule<SubmissionReadiness | undefined>(
    (signal) =>
      projectId
        ? getSubmissionReadiness(projectId, signal).then((result) => result.data)
        : Promise.resolve(undefined),
    undefined,
    [projectId],
  );
  if (!project) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-7 w-2/3" />
      </div>
    );
  }

  const Icon = project.paper_type === 'review' ? BookOpen : FlaskConical;
  const template = VENUE_TEMPLATES.find((t) => t.id === project.venue_template);
  const detailedReadiness = readinessModule.data;
  const summaryReadiness = project.attention_summary?.readiness;
  const readiness = detailedReadiness
    ? {
        state: detailedReadiness.state,
        href:
          detailedReadiness.items.find((item) => item.state !== 'pass')?.fix_href ??
          `/projects/${project.id}/export`,
      }
    : readinessModule.error
      ? { state: 'unknown' as const, href: `/projects/${project.id}` }
      : summaryReadiness;
  const readinessLabel = readiness ? {
    pass: '投稿检查通过', warn: '投稿有提醒', fail: '投稿未就绪', unknown: '投稿状态未知', stale: '投稿检查已过期',
  }[readiness.state] : null;
  // fail 用 warning 而不是 destructive：投稿未就绪是稿件还差几步，不是系统故障。
  // 强弱靠 warning → outline → muted 的梯度和标签文案区分，不靠红色。
  const readinessVariant = readiness?.state === 'pass' ? 'success' : readiness?.state === 'fail' ? 'warning' : readiness?.state === 'warn' || readiness?.state === 'stale' ? 'outline' : 'muted';

  return (
    <div className="space-y-2">
      <Link
        href="/projects"
        className="inline-flex min-h-11 items-center gap-1 rounded text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ChevronLeft className="h-3.5 w-3.5" /> 全部项目
      </Link>
      <ProjectTitle
        projectId={String(project.id)}
        title={project.title}
        onRenamed={(title) => onRenamed?.(title)}
      />
      <div aria-label="论文属性" className="flex flex-wrap items-center gap-x-4 gap-y-2 pt-2 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5"><Icon className="h-3.5 w-3.5" />{project.intake && project.intake.status !== 'ready' ? '论文类型待判断' : PAPER_TYPE_LABEL[project.paper_type]}</span>
        <span>{LANGUAGE_LABEL[project.language]}</span>
        {project.citation_style && <span>{CITATION_STYLE_LABEL[project.citation_style]}</span>}
        {template && <span>{template.label}</span>}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 border-t border-border/60 pt-2 text-xs">
        <div aria-label="执行设置与状态" className="flex flex-wrap items-center gap-x-3 gap-y-1 text-muted-foreground">
          <span>{WRITING_MODE_LABEL[project.writing_mode]} · {project.execution_profile === 'fast_draft' ? '快速草稿' : '标准生成'}</span>
          <button type="button" onClick={() => setSettingsOpen(true)} className="inline-flex min-h-9 items-center gap-1 rounded px-1 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <SlidersHorizontal className="h-3.5 w-3.5" />执行设置
          </button>
          {activeJob && ['queued', 'running', 'paused'].includes(activeJob.status) && (
            <span className="text-foreground">{activeJob.status === 'queued' ? '排队中' : activeJob.status === 'paused' ? '已暂停' : '执行中'} · {activeJob.checkpoint?.execution_profile === 'fast_draft' ? '快速草稿' : activeJob.checkpoint?.execution_profile === 'standard' ? '标准生成' : '按任务配置'}</span>
          )}
        </div>
        {readiness && readinessLabel && (
          <Link href={readiness.href} aria-label={`投稿状态：${readinessLabel}`} className="inline-flex min-h-9 items-center rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <Badge variant={readinessVariant}>{readinessLabel}</Badge>
          </Link>
        )}
      </div>
      <Dialog open={settingsOpen} onClose={() => setSettingsOpen(false)} title="执行设置" description="修改生成策略用于后续新任务，已启动任务保留原有执行配置。">
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">当前协作方式：{WRITING_MODE_LABEL[project.writing_mode]}</p>
          <FastDraftToggle
            value={project.execution_profile ?? 'standard'}
            disabled={savingProfile}
            onChange={async (execution_profile) => {
              setSavingProfile(true);
              try {
                await updateProject(projectId, { execution_profile });
                onProfileChanged?.();
              } catch (error) {
                toast({ title: '快速草稿设置未保存', description: describeError(error), variant: 'error' });
              } finally {
                setSavingProfile(false);
              }
            }}
          />
          <p className="text-xs text-muted-foreground">更快生成初稿，引用仍需完整核验。</p>
        </div>
      </Dialog>
    </div>
  );
}
