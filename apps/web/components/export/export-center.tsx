'use client';

import * as React from 'react';
import Link from 'next/link';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Download,
  FileDown,
  FileText,
  Image as ImageIcon,
  Loader2,
} from 'lucide-react';
import { Button, buttonVariants } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import { Select } from '@/components/ui/select';
import { useToast } from '@/components/ui/toast';
import { LoadState } from '@/components/layout/load-state';
import { ModuleError } from '@/components/layout/module-error';
import { EmptyState } from '@/components/ui/empty-state';
import { WorkbenchHeader } from '@/components/project/workbench-header';
import { WorkbenchFooterNav } from '@/components/project/workbench-footer-nav';
import { PublicationMetadata } from '@/components/project/publication-metadata';
import { useJobEvent, useJobFinished, useProject } from '@/components/project/project-context';
import {
  ALL_EXPORT_FORMATS,
  ApiError,
  exportDownloadUrl,
  exportPreviewUrl,
  exportRunDownloadUrl,
  listExports,
  listJobs,
  getQuality,
  retryExportRun,
  startExport,
} from '@/lib/api';
import type {
  ExportArtifact,
  ExportFormat,
  Job,
  JobStatus,
  QualityIssue,
  QualityProfile,
  QualityReport,
  RequestableExportFormat,
  VisualSummary,
} from '@/lib/types';
import { describeError } from '@/lib/errors';
import { timestampPredatesVisuals } from '@/lib/artifact-relations';
import { projectHref } from '@/lib/pipeline';
import { cn, formatDate } from '@/lib/utils';
import { useAsyncModule } from '@/lib/useAsyncModule';

const FORMAT_LABEL: Record<ExportFormat, string> = {
  pdf: 'PDF',
  latex_zip: 'LaTeX 工程 (zip)',
  markdown: 'Markdown',
  markdown_bundle: 'Markdown 图片包 (zip)',
  bibtex: 'BibTeX',
  docx: 'Word (docx)',
  compile_log: '编译日志',
  evidence_ledger: '证据台账（审计用）',
};

interface ExportRun {
  id: string;
  at: number;
  artifacts: ExportArtifact[];
  status?: JobStatus;
  requestedFormats?: RequestableExportFormat[];
}

/**
 * 按运行分组。
 *
 * 实测一个项目有 50 个产物，**全部** `document_version: 2`——版本列因此完全
 * 失去区分度，界面上是 10 行长得一模一样的「PDF · v2 · 07/25 02:03」。
 * 用户要找的是「我现在该投出去的那个 PDF」，不是一张 50 行的平表。
 */
function groupRuns(artifacts: ExportArtifact[], jobs: Job[]): ExportRun[] {
  const sorted = [...artifacts].sort(
    (a, b) => Date.parse(b.created_at ?? '') - Date.parse(a.created_at ?? ''),
  );
  const byRun = new Map<string, ExportRun>();
  for (const job of jobs.filter((candidate) => candidate.kind === 'compile')) {
    const resume = job.checkpoint?.resume as
      | { kwargs?: { formats?: RequestableExportFormat[] } }
      | undefined;
    byRun.set(job.id, {
      id: job.id,
      at: Date.parse(job.created_at ?? '') || 0,
      artifacts: [],
      status: job.status,
      requestedFormats: resume?.kwargs?.formats,
    });
  }
  for (const artifact of sorted) {
    const at = Date.parse(artifact.created_at ?? '') || 0;
    // 旧产物没有真实关系，明确放入“批次未知”的历史组；不再用五分钟窗口伪造批次。
    const id = artifact.export_run_id ?? 'historical-unknown';
    const existing = byRun.get(id);
    if (existing) {
      existing.artifacts.push(artifact);
      existing.at = Math.max(existing.at, at);
    }
    else byRun.set(id, { id, at, artifacts: [artifact] });
  }
  return [...byRun.values()].sort((a, b) => b.at - a.at);
}

export function ExportCenter() {
  const { projectId, project, progress, busy, startJob, reload: reloadProject } = useProject();
  const { toast } = useToast();

  const [result, setResult] = React.useState<Record<string, unknown> | null>(null);
  const [formats, setFormats] = React.useState<RequestableExportFormat[]>(ALL_EXPORT_FORMATS);
  const [qualityProfile, setQualityProfile] = React.useState<QualityProfile>('scholarly');
  const [gateBlockers, setGateBlockers] = React.useState<QualityIssue[]>([]);
  const artifactsModule = useAsyncModule<ExportArtifact[]>(
    (signal) => listExports(projectId, signal).then((response) => response.data), [], [projectId],
  );
  const jobsModule = useAsyncModule(
    (signal) => listJobs(projectId, signal).then((response) => response.data), [], [projectId],
  );
  const qualityModule = useAsyncModule<QualityReport | undefined>(
    (signal) => getQuality(projectId, qualityProfile, signal).then((response) => response.data),
    undefined,
    [projectId, qualityProfile],
  );
  const artifacts = artifactsModule.data;
  const quality = qualityModule.data;

  React.useEffect(() => {
    // 一键全管线（kind=full）同样会产出 render 段，按「最近一个带 render 的任务」取。
    const render = jobsModule.data.find(
      (job) => job.status === 'succeeded' && job.checkpoint?.render,
    )?.checkpoint?.render;
    if (render && typeof render === 'object') setResult(render as Record<string, unknown>);
  }, [jobsModule.data]);

  const reloadModules = React.useCallback(() => {
    artifactsModule.reload();
    jobsModule.reload();
    qualityModule.reload();
  }, [artifactsModule.reload, jobsModule.reload, qualityModule.reload]);
  useJobFinished(reloadModules);
  useJobEvent((event) => {
    if (event.type === 'render.completed') setResult(event.payload);
  });

  const run = async () => {
    setResult(null);
    setGateBlockers([]);
    if (formats.length === 0) {
      toast({ title: '请至少选择一种格式', variant: 'error' });
      return;
    }
    try {
      const started = await startExport(projectId, formats, qualityProfile);
      startJob(started.data, '后端不可用：无法触发导出');
      toast({
        title: '已开始生成导出产物',
        description: `${formats.length} 种格式正在编译，完成后会自动出现在本页。`,
      });
    } catch (err) {
      if (
        err instanceof ApiError &&
        ['quality_gate_failed', 'submission_quality_gate_failed'].includes(err.code)
      ) {
        const detail = err.detail as { blockers?: QualityIssue[] } | undefined;
        setGateBlockers(detail?.blockers ?? []);
      }
      toast({ title: '导出未能启动', description: describeError(err), variant: 'error' });
    }
  };

  const runs = React.useMemo(
    () => groupRuns(artifacts, jobsModule.data),
    [artifacts, jobsModule.data],
  );
  const latest = runs[0];
  const latestPdf = latest?.artifacts.find((a) => a.format === 'pdf');
  const compileOk = result?.compile_ok as boolean | undefined;

  const retryRun = React.useCallback(async (runId: string) => {
    try {
      const started = await retryExportRun(projectId, runId);
      startJob(started, '后端不可用：无法重试导出');
      toast({ title: '已重新开始这批导出' });
    } catch (error) {
      toast({ title: '批次重试失败', description: describeError(error), variant: 'error' });
    }
  }, [projectId, startJob, toast]);

  return (
    <div className="space-y-4">
      <WorkbenchHeader
        title="导出中心"
        description={formats.length > 0 ? `将生成：${formats.map((format) => FORMAT_LABEL[format]).join(' / ')}` : '请选择至少一种投稿文件格式'}
        actions={
          <Button onClick={run} disabled={!projectId || busy}>
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileDown className="h-4 w-4" />}
            生成投稿文件
          </Button>
        }
      />

      {project && (
        <PublicationMetadata
          project={project}
          onSaved={reloadProject}
          collapsedByDefault
        />
      )}

      <FormatSelector formats={formats} onChange={setFormats} />

      <SubmissionExportMode
        profile={qualityProfile}
        onChange={setQualityProfile}
        quality={quality}
        blockers={gateBlockers}
      />
      <ModuleError label="质量报告" error={qualityModule.error} onRetry={qualityModule.reload} />
      <ModuleError label="编译诊断" error={jobsModule.error} onRetry={jobsModule.reload} />
      <DepthMetricsPanel quality={quality} />

      <VisualExportSummary
        projectId={projectId}
        summary={progress.visuals}
        latestRunAt={runs[0]?.at}
      />

      {result && (
        <CompileDiagnostics
          result={result}
          ok={compileOk}
          projectId={projectId}
          log={latest?.artifacts.find((a) => a.format === 'compile_log')}
        />
      )}

      <LoadState
        loading={artifactsModule.loading && !artifactsModule.ready}
        error={artifactsModule.error}
        onRetry={artifactsModule.reload}
        skeletonClassName="h-64"
      >
        {runs.length === 0 ? (
          <EmptyState
            title={progress.sectionCount === 0 ? '还没有正文' : '还没有导出产物'}
            description={progress.sectionCount === 0 ? (
              <>
                先在
                <Link href={projectHref(projectId, 'write')} className="mx-1 underline">
                  写作工作台
                </Link>
                生成正文，再点「生成导出产物」。
              </>
            ) : (
              <>已有正文，还没有导出过。点右上角「生成导出产物」编译成 PDF。</>
            )}
          />
        ) : (
          <div className="space-y-6">
            <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr),20rem]">
              <div className="min-w-0 space-y-2">
                <h3 className="text-sm font-medium">当前产物预览</h3>
                {latestPdf ? (
                  <PdfPreviewPane projectId={projectId} artifact={latestPdf} />
                ) : (
                  <EmptyState
                    title="最近一次运行没有产出 PDF"
                    description="LaTeX 工程与其余格式仍可在右侧下载，也可以查看编译日志后重新导出。"
                    className="py-12"
                  />
                )}
              </div>

              <aside className="space-y-2 lg:sticky lg:top-4 lg:self-start">
                <h3 className="text-sm font-medium">
                  最近一次运行 · {formatDate(latest?.artifacts[0]?.created_at ?? new Date(latest?.at ?? 0).toISOString())}
                </h3>
                {latest && latest.id !== 'historical-unknown' && latest.artifacts.length > 0 && (
                  <a
                    href={exportRunDownloadUrl(projectId, latest.id)}
                    download
                    className={buttonVariants({ variant: 'outline', size: 'sm' })}
                  >
                    <Download className="h-3.5 w-3.5" /> 下载整批
                  </a>
                )}
                {latest?.status === 'failed' && (
                  <Button variant="outline" size="sm" onClick={() => retryRun(latest.id)}>
                    重试本批
                  </Button>
                )}
                <div className="divide-y border-y">
                    {latest?.artifacts.map((artifact) => (
                      <ArtifactRow key={artifact.id} projectId={projectId} artifact={artifact} />
                    ))}
                </div>
                {!latest?.artifacts.some((a) => a.format === 'compile_log') && (
                  <p className="text-xs text-muted-foreground">
                    这次运行早于「编译日志登记为产物」的改动，因此没有日志条目；
                    重新导出一次即可拿到。
                  </p>
                )}
              </aside>
            </div>

            {runs.length > 1 && (
              <HistoryRuns projectId={projectId} runs={runs.slice(1)} onRetry={retryRun} />
            )}
          </div>
        )}
      </LoadState>

      <WorkbenchFooterNav current="export" />
    </div>
  );
}

/**
 * 导出前的视觉完整性提示。
 *
 * 视觉**不是硬门槛**：用户可以忽略所有建议直接导出一份纯文本论文。这里只回答
 * 三个会影响判断的问题——正文里有几张图、有多少建议这次不会包含、上次导出是不是
 * 已经早于最近一次插图。批准新图不会自动重编译，也不会自动产生费用，因此
 * 「需要重新导出」必须显式说出来，而不是让用户拿到一份缺图的 PDF 才发现。
 */
function VisualExportSummary({
  projectId,
  summary,
  latestRunAt,
}: {
  projectId: string;
  summary: VisualSummary;
  /** 最近一次导出运行的时间戳（毫秒）。 */
  latestRunAt?: number;
}) {
  const unhandled = summary.pending + summary.ready;
  const outdated = timestampPredatesVisuals(latestRunAt, summary);

  if (summary.approved === 0 && unhandled === 0 && summary.failed === 0) return null;

  return (
    <section
      className="space-y-1.5 rounded-md border bg-muted/30 px-3 py-2 text-sm"
      aria-label="视觉完整性"
    >
      <p className="flex items-center gap-2 font-medium">
        <ImageIcon className="h-4 w-4 text-muted-foreground" /> 视觉
      </p>
      <ul className="space-y-0.5 text-xs text-muted-foreground">
        {summary.approved > 0 && (
          <li>
            已插入 {summary.approved} 张——PDF、DOCX、LaTeX ZIP 与 Markdown Bundle 都会包含它们。
          </li>
        )}
        {unhandled > 0 && (
          <li className="text-warning-foreground">
            {unhandled} 条建议尚未处理，本次导出<b>不会</b>包含。
          </li>
        )}
        {summary.failed > 0 && (
          <li className="text-destructive-strong">{summary.failed} 张生成失败。</li>
        )}
        {outdated && (
          <li className="text-warning-foreground">
            当前导出产物早于最近一次图片插入，需要重新导出才能把新图带进去。
          </li>
        )}
      </ul>
      {(unhandled > 0 || summary.failed > 0 || outdated) && (
        <Link
          href={projectHref(projectId, 'visuals')}
          className="inline-flex min-h-11 items-center text-xs underline underline-offset-2"
        >
          去视觉工作台处理 →
        </Link>
      )}
    </section>
  );
}

function DepthMetricsPanel({ quality }: { quality: QualityReport | undefined }) {
  const labels: Record<string, string> = {
    '1_fulltext_acquisition_rate': '全文获取',
    '2_methods_results_section_coverage': '方法/结果定位',
    '3_structured_object_coverage': '结构化对象',
    '4_core_claim_evidence_coverage': '核心证据覆盖',
    '5_numeric_locator_coverage': '数字定位',
    '6_numeric_source_consistency': '数值一致',
    '7_unsupported_strong_claim_rate': '无依据强结论 ↓',
    '8_invalid_comparison_rate': '错误比较 ↓',
    '9_cross_study_synthesis_paragraph_rate': '跨研究综合',
    '10_paper_enumeration_paragraph_rate': '按论文罗列 ↓',
    '11_question_answer_completeness': '问题回答完整',
  };
  const entries = Object.entries(quality?.depth_metrics ?? {}).filter(
    (entry): entry is [string, number] =>
      entry[0] in labels && typeof entry[1] === 'number',
  );
  if (entries.length === 0) return null;
  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-sm font-medium">综述深度指标</h3>
        <p className="text-xs text-muted-foreground">
          与当前正文快照绑定；改动正文后请重新质检。
        </p>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
        {entries.map(([key, value]) => (
          <div key={key} className="rounded-md border p-2">
            <div className="font-semibold tabular-nums">{Math.round(value * 100)}%</div>
            <div className="text-micro text-muted-foreground">{labels[key]}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function SubmissionExportMode({
  profile,
  onChange,
  quality,
  blockers,
}: {
  profile: QualityProfile;
  onChange: (profile: QualityProfile) => void;
  quality: QualityReport | undefined;
  blockers: QualityIssue[];
}) {
  const ready = quality?.readiness_status;
  return (
    <section className="space-y-3 border-y py-4">
        <div className="grid gap-3 sm:grid-cols-[12rem,1fr] sm:items-end">
          <label className="space-y-1 text-xs font-medium text-muted-foreground">
            产物用途
            <Select
              value={profile}
              onChange={(event) => onChange(event.target.value as QualityProfile)}
            >
              <option value="draft">内部草稿</option>
              <option value="scholarly">学术严谨稿（推荐）</option>
              <option value="submission">投稿候选稿</option>
            </Select>
          </label>
          <p className="text-xs text-muted-foreground">
            {profile === 'draft'
              ? '兼容模式：质量问题会提示，但仍可生成产物。'
              : profile === 'scholarly'
                ? '默认模式：证据等级、可比较性与数字定位必须通过，投稿元数据问题先提示。'
                : '严格模式：内容、审批与投稿元数据全部通过后才能导出。'}
          </p>
        </div>
        {profile !== 'draft' && (
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Badge
              variant={
                ready === 'submission_ready' || ready === 'preflight_ready'
                  ? 'success'
                  : 'warning'
              }
            >
              {ready === 'submission_ready'
                ? '最终 PDF 已通过'
                : ready === 'preflight_ready'
                  ? '内容预检已通过'
                  : '尚未通过投稿质量门'}
            </Badge>
            {quality?.stale && <Badge variant="warning">质量报告已过期</Badge>}
            <span className="text-muted-foreground">
              核心论断全文覆盖{' '}
              {Math.round((quality?.core_claim_fulltext_coverage ?? 0) * 100)}%
            </span>
          </div>
        )}
        {blockers.length > 0 && (
          <div className="rounded-md border border-warning/40 bg-warning/5 p-3 text-xs">
            <p className="mb-1 font-medium text-warning-strong">本次导出被质量门阻断：</p>
            {blockers.map((blocker) => (
              <p key={blocker.code}>· {blocker.message}</p>
            ))}
          </div>
        )}
    </section>
  );
}

function FormatSelector({
  formats,
  onChange,
}: {
  formats: RequestableExportFormat[];
  onChange: (formats: RequestableExportFormat[]) => void;
}) {
  const toggle = (format: RequestableExportFormat) =>
    onChange(
      formats.includes(format) ? formats.filter((f) => f !== format) : [...formats, format],
    );

  return (
    <section className="flex flex-wrap items-center gap-x-5 gap-y-2 border-y py-3">
        <span className="text-xs font-medium text-muted-foreground">导出格式</span>
        {ALL_EXPORT_FORMATS.map((format) => (
          <label key={format} className="flex cursor-pointer items-center gap-2 text-sm">
            <Checkbox checked={formats.includes(format)} onCheckedChange={() => toggle(format)} />
            {FORMAT_LABEL[format]}
          </label>
        ))}
    </section>
  );
}

function PdfPreviewPane({
  projectId,
  artifact,
}: {
  projectId: string;
  artifact: ExportArtifact;
}) {
  // 浏览器原生 PDF 渲染，不引入 pdf.js 之类的新依赖。
  // 必须用 inline 直链：attachment 会让这个 iframe 变成一次下载（页面一打开
  // 就凭空下载一个文件），而预览框留空。
  const src = exportPreviewUrl(projectId, artifact.id);
  return (
    <div className="space-y-2">
      <div className="hidden overflow-hidden rounded-lg border md:block">
        <iframe
          src={src}
          title="PDF 预览"
          className="h-[calc(100vh-22rem)] min-h-96 w-full bg-muted"
        />
      </div>
      <EmptyState
        title="PDF 已生成"
        description="移动端不内嵌 PDF 阅读器，请下载文件或在新标签中打开。"
        className="md:hidden"
        action={
          <div className="flex flex-wrap justify-center gap-2">
            <a
              href={exportDownloadUrl(projectId, artifact.id)}
              download
              className={buttonVariants()}
            >
              <Download /> 下载 PDF
            </a>
            <a
              href={src}
              target="_blank"
              rel="noreferrer"
              className={buttonVariants({ variant: 'outline' })}
            >
              在新标签打开
            </a>
          </div>
        }
      />
      {/* 不内嵌 PDF 阅读器的浏览器（部分移动端）拿不到上面的预览，给一条出路。 */}
      <p className="hidden text-meta text-muted-foreground md:block">
        预览为空？
        <a href={src} target="_blank" rel="noreferrer" className="mx-1 inline-flex min-h-11 items-center underline">
          在新标签打开 PDF
        </a>
        或从右侧下载。
      </p>
    </div>
  );
}

function ArtifactRow({
  projectId,
  artifact,
}: {
  projectId: string;
  artifact: ExportArtifact;
}) {
  const isLog = artifact.format === 'compile_log' || artifact.format === 'evidence_ledger';
  const isPdf = artifact.format === 'pdf';
  const ready = artifact.readiness_status === 'submission_ready';
  const needsRevision = artifact.readiness_status === 'needs_revision';
  return (
    <div className="flex items-center justify-between gap-3 px-3 py-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant={isPdf && ready ? 'success' : isLog ? 'muted' : 'outline'}>
          {FORMAT_LABEL[artifact.format] ?? artifact.format}
        </Badge>
        {artifact.quality_profile === 'submission' && (
          <Badge variant={ready ? 'success' : needsRevision ? 'warning' : 'muted'}>
            {ready ? '可提交' : needsRevision ? '需修订' : '未评估'}
          </Badge>
        )}
      </div>
      <a
        // 日志是纯文本，直接在新标签打开比下载更顺手——但那要求 inline 直链，
        // 否则 target=_blank 也只是「开一个空标签 + 下载一个 .log」。
        href={
          isLog ? exportPreviewUrl(projectId, artifact.id) : exportDownloadUrl(projectId, artifact.id)
        }
        {...(isLog ? { target: '_blank', rel: 'noreferrer' } : { download: true })}
        className={buttonVariants({ variant: 'ghost', size: 'sm' })}
      >
        {isLog ? <FileText className="h-3.5 w-3.5" /> : <Download className="h-3.5 w-3.5" />}
        {isLog ? '查看' : '下载'}
      </a>
    </div>
  );
}

function HistoryRuns({
  projectId,
  runs,
  onRetry,
}: {
  projectId: string;
  runs: ExportRun[];
  onRetry: (runId: string) => Promise<void>;
}) {
  const [open, setOpen] = React.useState<string | null>(null);

  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">历史运行（{runs.length}）</h3>
      <Card>
        <CardContent className="divide-y p-0">
          {runs.map((run) => {
            const expanded = open === run.id;
            const hasPdf = run.artifacts.some((a) => a.format === 'pdf');
            return (
              <div key={run.id}>
                <button
                  type="button"
                  onClick={() => setOpen(expanded ? null : run.id)}
                  aria-expanded={expanded}
                  className="flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                >
                  <ChevronDown
                    className={cn(
                      'h-4 w-4 shrink-0 text-muted-foreground transition-transform',
                      expanded && 'rotate-180',
                    )}
                  />
                  <span className="flex-1 text-sm">
                    {run.id === 'historical-unknown'
                      ? '历史导出（批次未知）'
                      : formatDate(run.artifacts[0]?.created_at ?? new Date(run.at).toISOString())}
                    <span className="ml-2 text-xs text-muted-foreground">
                      {new Date(run.at).toLocaleTimeString('zh-CN', {
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                    </span>
                  </span>
                  {hasPdf ? (
                    <Badge variant="success">含 PDF</Badge>
                  ) : (
                    <Badge variant="warning">无 PDF</Badge>
                  )}
                  <span className="text-xs text-muted-foreground">
                    {run.status === 'failed' ? '批次失败' : `${run.artifacts.length} 个文件`}
                  </span>
                </button>
                {expanded && (
                  <div className="divide-y border-t bg-muted/20">
                    {run.id !== 'historical-unknown' && (
                      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
                        {run.artifacts.length > 0 && (
                          <a
                            href={exportRunDownloadUrl(projectId, run.id)}
                            download
                            className={buttonVariants({ variant: 'outline', size: 'sm' })}
                          >
                            <Download className="h-3.5 w-3.5" /> 下载整批
                          </a>
                        )}
                        {run.status === 'failed' && (
                          <Button variant="outline" size="sm" onClick={() => onRetry(run.id)}>
                            重试本批
                          </Button>
                        )}
                        {run.requestedFormats && (
                          <span className="text-xs text-muted-foreground">
                            请求：{run.requestedFormats.map((format) => FORMAT_LABEL[format]).join(' / ')}
                          </span>
                        )}
                      </div>
                    )}
                    {run.artifacts.length === 0 && (
                      <p className="px-3 py-3 text-xs text-muted-foreground">
                        本批次没有留下可下载文件。
                      </p>
                    )}
                    {run.artifacts.map((artifact) => (
                      <ArtifactRow key={artifact.id} projectId={projectId} artifact={artifact} />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </CardContent>
      </Card>
    </div>
  );
}

/**
 * 导出告警的中文说明。
 *
 * 后端的 reason 是稳定的机器标识（`inline_bibliography_fallback`），直接摊在
 * 界面上等于没解释：用户看不出「这会不会影响我投出去的稿子」。
 */
const WARNING_TEXT: Record<string, string> = {
  inline_bibliography_fallback:
    '参考文献用了内置兜底排版（编译沙箱取不到该样式的 .bst）：编号与条目内容准确，' +
    '但格式细节不等于投稿方样式；下载 LaTeX 工程在本地重编即可得到标准排版。',
  citations_unresolved: '参考文献未能排出，PDF 内引用可能显示为 [?]；请查看编译日志。',
  template_not_implemented: '所选投稿模板尚未内置，已用通用 article 模板编译。',
  compile_failed: 'PDF 未编译成功，已交付修复后的 LaTeX 工程与编译日志。',
  pandoc_unavailable: '环境缺少 pandoc，本次未生成 Word (docx)。',
  cite_keys_stripped: '有引用键不在写作白名单内，已按引用真实性红线剔除。',
};

function describeWarning(warning: Record<string, unknown>): string {
  const reason = String(warning.reason ?? '');
  const text = WARNING_TEXT[reason];
  if (!text) return `${String(warning.stage ?? '导出')}：${reason}`;
  const requested = warning.requested ? `（${String(warning.requested)}）` : '';
  return `${text}${requested}`;
}

/**
 * 编译诊断。
 *
 * 编译失败但仍交付 LaTeX 工程，是 draft-first 的正常路径而不是错误——
 * 因此用琥珀色而不是红色。
 */
function CompileDiagnostics({
  result,
  ok,
  projectId,
  log,
}: {
  result: Record<string, unknown>;
  ok: boolean | undefined;
  projectId: string;
  log: ExportArtifact | undefined;
}) {
  const warnings = (result.warnings as Array<Record<string, unknown>>) ?? [];
  // 「编译成功」和「书目排出来了」是两件事：.bst 取不到时 BibTeX 失败会被
  // 降级成 warning，PDF 照样产出，只是全文引用都是 [?]。所以标题不能只看 ok。
  const clean = ok && warnings.length === 0;
  return (
    <Card className={clean ? undefined : 'border-warning/50 bg-warning/5'}>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm">
          {clean ? (
            <>
              <CheckCircle2 className="h-4 w-4 text-success-strong" /> PDF 编译成功
            </>
          ) : ok ? (
            <>
              <AlertTriangle className="h-4 w-4 text-warning-strong" />
              PDF 编译成功，但有降级项
            </>
          ) : (
            <>
              <AlertTriangle className="h-4 w-4 text-warning-strong" />
              PDF 未编译成功，LaTeX 工程与其余格式已交付
            </>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-xs">
        <p className="text-muted-foreground">
          模板 {String(result.template ?? '—')} · 修复轮次 {String(result.compile_rounds ?? 0)}
        </p>
        {warnings.map((warning, index) => (
          <p key={index} className="text-warning-strong">
            {describeWarning(warning)}
          </p>
        ))}
        {/* 编译失败时，日志是用户唯一能拿来判断「是模板、字体还是正文的问题」的东西。 */}
        {log && (
          <a
            href={exportPreviewUrl(projectId, log.id)}
            target="_blank"
            rel="noreferrer"
            className={buttonVariants({ variant: 'outline', size: 'sm' })}
          >
            <FileText className="h-3.5 w-3.5" /> 查看编译日志
          </a>
        )}
      </CardContent>
    </Card>
  );
}
