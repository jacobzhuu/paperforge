'use client';

import * as React from 'react';
import Link from 'next/link';
import { AlertTriangle, ArrowRight, CheckCircle2, Download, Rocket } from 'lucide-react';
import { Button, buttonVariants } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import { useJobFinished, useProject } from './project-context';
import {
  exportDownloadUrl,
  generateAll,
  getCitationAudit,
  getCostDetail,
  getNumLint,
  getVersionHistory,
  listExports,
  listJobs,
} from '@/lib/api';
import { describeError } from '@/lib/errors';
import { stageLabel } from '@/lib/labels';
import { nextAction } from '@/lib/useProjectProgress';
import { projectHref } from '@/lib/pipeline';
import type {
  CitationAudit,
  CostDetail,
  ExportArtifact,
  Job,
  NumLintReport,
  VersionHistory,
} from '@/lib/types';
import { formatDate } from '@/lib/utils';

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

/**
 * 项目概览。
 *
 * 此前是「下一步 / 稿件快照 / 近期任务 / 成本 / 版本」五张 Card 拼成的
 * SaaS 网格。docs/ui-design.md 原则 07（Typography over containers）要求
 * 分层靠排版而不是容器：一个语义段落不自动等于一张卡片。现在整页 0 张 Card，
 * 层级由「小写标题 + 留白 + 一条分隔线」承担。
 *
 * 唯一保留边框的是最上面的「下一步」——它是全页唯一的行动召唤，
 * 需要一个能被眼睛先抓到的锚点。
 */
export function ProjectOverview() {
  const { projectId, paperType, progress, busy, startJob } = useProject();
  const { toast } = useToast();

  const [audit, setAudit] = React.useState<CitationAudit | undefined>();
  const [lint, setLint] = React.useState<NumLintReport | undefined>();
  const [jobs, setJobs] = React.useState<Job[]>([]);
  const [cost, setCost] = React.useState<CostDetail | undefined>();
  const [versions, setVersions] = React.useState<VersionHistory | undefined>();
  const [exports, setExports] = React.useState<ExportArtifact[]>([]);

  const load = React.useCallback(() => {
    if (!projectId) return;
    Promise.all([
      getCitationAudit(projectId),
      listJobs(projectId),
      getCostDetail(projectId),
      getVersionHistory(projectId),
      listExports(projectId),
      paperType === 'original' ? getNumLint(projectId) : Promise.resolve({ data: undefined }),
    ])
      .then(([a, j, c, v, e, n]) => {
        setAudit(a.data);
        setJobs(j.data);
        setCost(c.data);
        setVersions(v.data);
        setExports(e.data);
        setLint(n.data as NumLintReport | undefined);
      })
      .catch(() => {
        /* 概览是只读汇总，取不到不阻断任何操作 */
      });
  }, [projectId, paperType]);

  React.useEffect(load, [load]);
  useJobFinished(load);

  const action = nextAction(progress, paperType);
  const latestPdf = exports.find((a) => a.format === 'pdf');

  const runAll = async () => {
    try {
      const started = await generateAll(projectId);
      startJob(started.data, '后端不可用：无法启动全管线');
    } catch (err) {
      toast({ title: '全管线未能启动', description: describeError(err), variant: 'error' });
    }
  };

  if (progress.loading) {
    return (
      <div className="space-y-8">
        <Skeleton className="h-24" />
        <Skeleton className="h-32" />
        <Skeleton className="h-24" />
      </div>
    );
  }

  return (
    <div className="space-y-10">
      {/* 下一步：全页唯一的行动召唤，也是唯一保留边框的块。 */}
      <div className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-primary/25 bg-accent/40 px-5 py-4">
        <div className="min-w-0 space-y-1">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            下一步
          </p>
          <p className="font-serif text-xl font-semibold tracking-tight">{action.label}</p>
          <p className="max-w-2xl text-sm text-muted-foreground">{action.reason}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="ghost" onClick={runAll} disabled={busy}>
            <Rocket className="h-4 w-4" /> 跑通全管线
          </Button>
          <Link
            href={projectHref(projectId, action.step === 'overview' ? '' : action.step)}
            className={buttonVariants()}
          >
            {action.label} <ArrowRight className="h-4 w-4" />
          </Link>
        </div>
      </div>

      {/* 稿件：draft-first——永远展示当前最好的产物。 */}
      <section className="space-y-3">
        <SectionTitle>稿件</SectionTitle>

        <p className="text-sm text-muted-foreground">
          <Figure value={progress.libraryCount} unit="篇文献" />
          <Sep />
          <Figure value={progress.sectionCount} unit="章节" />
          <Sep />
          <Figure value={progress.wordCount} unit="字" />
          <Sep />
          <Figure value={progress.exportCount} unit="个导出产物" />
        </p>

        <div className="space-y-1.5 pt-1">
          <TrustRow
            ok={(audit?.hallucinated_cite_keys.length ?? 0) === 0}
            okText={`0 幻觉引用，全文 ${audit?.used_cite_keys.length ?? 0} 个引用键均在白名单内`}
            badText={`${audit?.hallucinated_cite_keys.length ?? 0} 个越权引用`}
            muted={!audit}
            mutedText="引用审计在正文生成后产出"
          />
          {paperType === 'original' && (
            <TrustRow
              ok={lint?.consistent ?? true}
              okText={
                lint
                  ? `正文数字全部可溯源（已核 ${lint.checked_count} 处）`
                  : '数字一致性检查在正文生成后产出'
              }
              badText={`${lint?.unsourced_count ?? 0} 处数值在素材中找不到出处`}
              muted={!lint}
              mutedText="数字一致性检查在正文生成后产出"
              href={projectHref(projectId, 'write')}
            />
          )}
        </div>

        {latestPdf && (
          <div className="flex items-center gap-3 pt-1 text-sm">
            <a
              href={exportDownloadUrl(projectId, latestPdf.id)}
              download
              className="inline-flex items-center gap-1.5 font-medium underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Download className="h-3.5 w-3.5" /> 下载最新 PDF
            </a>
            <span className="text-xs text-muted-foreground">
              {formatDate(latestPdf.created_at ?? undefined)}
            </span>
          </div>
        )}
      </section>

      <div className="grid gap-10 md:grid-cols-2">
        <RecentJobs jobs={jobs} />
        <div className="space-y-10">
          <CostSummary cost={cost} />
          <VersionSummary versions={versions} />
        </div>
      </div>
    </div>
  );
}

/** 分组标题：小号、字距略开的 sans，靠留白与下方内容拉开层级，不加边框。 */
function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
      {children}
    </h2>
  );
}

/** 「46 篇文献」这类计数：数字用等宽 + 前景色，单位用 muted，读起来是一句话而不是四张指标卡。 */
function Figure({ value, unit }: { value: number; unit: string }) {
  return (
    <>
      <span className="font-medium tabular-nums text-foreground">{value.toLocaleString()}</span>{' '}
      {unit}
    </>
  );
}

function Sep() {
  return <span className="px-2 text-border">·</span>;
}

function TrustRow({
  ok,
  okText,
  badText,
  muted,
  mutedText,
  href,
}: {
  ok: boolean;
  okText: string;
  badText: string;
  muted?: boolean;
  mutedText?: string;
  href?: string;
}) {
  if (muted) {
    return <p className="text-sm text-muted-foreground">{mutedText}</p>;
  }
  return (
    <div className="flex items-start gap-2 text-sm">
      {ok ? (
        <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success-strong" />
      ) : (
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive-strong" />
      )}
      <span className={ok ? 'text-muted-foreground' : 'text-destructive-strong'}>
        {ok ? okText : badText}
        {!ok && href && (
          <Link href={href} className="ml-1 underline underline-offset-4">
            去处理 →
          </Link>
        )}
      </span>
    </div>
  );
}

/**
 * 近期任务。
 *
 * 状态此前是彩色 Badge，五行任务就是五个色块。改成一列固定宽度的状态词 +
 * 一个小圆点：降级与失败仍然靠颜色一眼可辨，但不再有色块在页面上抢视线。
 */
function RecentJobs({ jobs }: { jobs: Job[] }) {
  const recent = jobs.slice(0, 5);
  return (
    <section className="space-y-3">
      <SectionTitle>近期任务</SectionTitle>
      {recent.length === 0 ? (
        <p className="text-sm text-muted-foreground">还没有运行过任务。</p>
      ) : (
        <ul className="space-y-2">
          {recent.map((job) => {
            const warnings = (job.error?.warnings as unknown[] | undefined)?.length ?? 0;
            const state = jobState(job.status, warnings);
            return (
              <li key={job.id} className="flex items-baseline gap-3 text-sm">
                <span
                  aria-hidden
                  className={`h-1.5 w-1.5 shrink-0 translate-y-[-1px] rounded-full ${state.dot}`}
                />
                <span className={`w-16 shrink-0 text-xs ${state.tone}`}>{state.label}</span>
                <span className="min-w-0 flex-1 truncate">{stageLabel(job.stage)}</span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {formatDate(job.created_at ?? undefined)}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function jobState(
  status: string,
  warnings: number,
): { label: string; tone: string; dot: string } {
  if (status === 'succeeded') {
    return warnings > 0
      ? { label: '降级完成', tone: 'text-warning-strong', dot: 'bg-warning' }
      : { label: '完成', tone: 'text-muted-foreground', dot: 'bg-success' };
  }
  if (status === 'failed') {
    return { label: '失败', tone: 'text-destructive-strong', dot: 'bg-destructive' };
  }
  if (TERMINAL.has(status)) {
    return { label: status, tone: 'text-muted-foreground', dot: 'bg-border' };
  }
  return { label: '进行中', tone: 'text-foreground', dot: 'bg-primary' };
}

function CostSummary({ cost }: { cost: CostDetail | undefined }) {
  const failed = cost?.totals.failed_call_count ?? 0;
  return (
    <section className="space-y-3">
      <SectionTitle>成本</SectionTitle>
      <p className="text-sm text-muted-foreground">
        <Figure value={cost?.totals.call_count ?? 0} unit="次调用" />
        <Sep />
        <Figure value={cost?.totals.input_tokens ?? 0} unit="输入 tokens" />
        <Sep />
        <Figure value={cost?.totals.output_tokens ?? 0} unit="输出 tokens" />
        {(cost?.images?.call_count ?? 0) > 0 && (
          <>
            <Sep />
            <Figure value={cost?.images?.call_count ?? 0} unit="次图片生成" />
          </>
        )}
      </p>
      {failed > 0 && (
        <p className="text-sm text-warning-strong">
          <span className="font-medium tabular-nums">{failed}</span> 次调用失败
        </p>
      )}
      {(cost?.images?.failed_call_count ?? 0) > 0 && (
        <p className="text-sm text-warning-strong">
          <span className="font-medium tabular-nums">{cost?.images?.failed_call_count}</span>{' '}
          次图片生成失败
        </p>
      )}
      {(cost?.images?.by_size?.length ?? 0) > 0 && (
        <div className="space-y-1 text-xs text-muted-foreground">
          {cost?.images?.by_size?.map((row, index) => (
            <p key={`${row.provider}-${row.model ?? ''}-${row.width ?? 0}-${row.height ?? 0}-${index}`}>
              {row.provider}{row.model ? ` / ${row.model}` : ''}
              {' · '}{row.width && row.height ? `${row.width}×${row.height}` : '未产出尺寸'}
              {' · '}{row.call_count} 次
              {row.cost_estimate > 0 ? ` · 估算 $${row.cost_estimate.toFixed(4)}` : ''}
            </p>
          ))}
        </div>
      )}
    </section>
  );
}

function VersionSummary({ versions }: { versions: VersionHistory | undefined }) {
  const docs = versions?.documents ?? [];
  const outlines = versions?.outlines ?? [];
  if (docs.length === 0 && outlines.length === 0) {
    return (
      <section className="space-y-3">
        <SectionTitle>版本</SectionTitle>
        <p className="text-sm text-muted-foreground">还没有文稿或大纲版本。</p>
      </section>
    );
  }
  return (
    <section className="space-y-3">
      <SectionTitle>版本</SectionTitle>
      <p className="text-sm text-muted-foreground">
        {docs.length > 0 && (
          <>
            文稿 <span className="font-medium tabular-nums text-foreground">v{docs[0].version}</span>
            （共 {docs.length} 版）
          </>
        )}
        {docs.length > 0 && outlines.length > 0 && <Sep />}
        {outlines.length > 0 && (
          <>
            大纲{' '}
            <span className="font-medium tabular-nums text-foreground">v{outlines[0].version}</span>
            （共 {outlines.length} 版）
          </>
        )}
      </p>
    </section>
  );
}
