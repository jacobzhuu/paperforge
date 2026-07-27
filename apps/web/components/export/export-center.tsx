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
  Loader2,
} from 'lucide-react';
import { Button, buttonVariants } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import { useToast } from '@/components/ui/toast';
import { LoadState } from '@/components/layout/load-state';
import { WorkbenchHeader } from '@/components/project/workbench-header';
import { WorkbenchFooterNav } from '@/components/project/workbench-footer-nav';
import { useJobEvent, useJobFinished, useProject } from '@/components/project/project-context';
import {
  ALL_EXPORT_FORMATS,
  exportDownloadUrl,
  exportPreviewUrl,
  listExports,
  listJobs,
  startExport,
} from '@/lib/api';
import type { ExportArtifact, ExportFormat, RequestableExportFormat } from '@/lib/types';
import { describeError } from '@/lib/errors';
import { projectHref } from '@/lib/pipeline';
import { cn, formatDate } from '@/lib/utils';

const FORMAT_LABEL: Record<ExportFormat, string> = {
  pdf: 'PDF',
  latex_zip: 'LaTeX 工程 (zip)',
  markdown: 'Markdown',
  markdown_bundle: 'Markdown 图片包 (zip)',
  bibtex: 'BibTeX',
  docx: 'Word (docx)',
  compile_log: '编译日志',
};

/** 同一次导出运行内产物的时间间隔上限（毫秒）。 */
const RUN_WINDOW_MS = 5 * 60 * 1000;

interface ExportRun {
  id: string;
  at: number;
  artifacts: ExportArtifact[];
}

/**
 * 按运行分组。
 *
 * 实测一个项目有 50 个产物，**全部** `document_version: 2`——版本列因此完全
 * 失去区分度，界面上是 10 行长得一模一样的「PDF · v2 · 07/25 02:03」。
 * 用户要找的是「我现在该投出去的那个 PDF」，不是一张 50 行的平表。
 */
function groupRuns(artifacts: ExportArtifact[]): ExportRun[] {
  const sorted = [...artifacts].sort(
    (a, b) => Date.parse(b.created_at ?? '') - Date.parse(a.created_at ?? ''),
  );
  const runs: ExportRun[] = [];
  for (const artifact of sorted) {
    const at = Date.parse(artifact.created_at ?? '') || 0;
    const last = runs[runs.length - 1];
    if (last && Math.abs(last.at - at) <= RUN_WINDOW_MS) {
      last.artifacts.push(artifact);
      continue;
    }
    runs.push({ id: artifact.id, at, artifacts: [artifact] });
  }
  return runs;
}

export function ExportCenter() {
  const { projectId, progress, busy, startJob } = useProject();
  const { toast } = useToast();

  const [artifacts, setArtifacts] = React.useState<ExportArtifact[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [result, setResult] = React.useState<Record<string, unknown> | null>(null);
  const [formats, setFormats] = React.useState<RequestableExportFormat[]>(ALL_EXPORT_FORMATS);

  const reload = React.useCallback(async () => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    // 诊断此前只来自 SSE，刷新一次页面就没了——而「书目走了兜底排版」这类降级
    // 恰恰是用户投稿前必须知道的事。编译任务的 checkpoint 里存着同一份 payload。
    const [rows, jobs] = await Promise.all([listExports(projectId), listJobs(projectId)]);
    setArtifacts(rows.data);
    // 一键全管线（kind=full）同样会产出 render 段，按「最近一个带 render 的任务」取。
    const render = jobs.data.find(
      (job) => job.status === 'succeeded' && job.checkpoint?.render,
    )?.checkpoint?.render;
    if (render && typeof render === 'object') setResult(render as Record<string, unknown>);
    setLoadError(null);
    setLoading(false);
  }, [projectId]);

  const runReload = React.useCallback(() => {
    setLoadError(null);
    reload().catch((err) => {
      setLoadError(describeError(err));
      setLoading(false);
    });
  }, [reload]);

  React.useEffect(() => {
    runReload();
  }, [runReload]);
  useJobFinished(runReload);
  useJobEvent((event) => {
    if (event.type === 'render.completed') setResult(event.payload);
  });

  const run = async () => {
    setResult(null);
    if (formats.length === 0) {
      toast({ title: '请至少选择一种格式', variant: 'error' });
      return;
    }
    try {
      const started = await startExport(projectId, formats);
      startJob(started.data, '后端不可用：无法触发导出');
    } catch (err) {
      toast({ title: '导出未能启动', description: describeError(err), variant: 'error' });
    }
  };

  const runs = React.useMemo(() => groupRuns(artifacts), [artifacts]);
  const latest = runs[0];
  const latestPdf = latest?.artifacts.find((a) => a.format === 'pdf');
  const compileOk = result?.compile_ok as boolean | undefined;

  return (
    <div className="space-y-4">
      <WorkbenchHeader
        title="导出中心"
        description="LaTeX 工程 / PDF / Markdown 图片包 / BibTeX / docx"
        actions={
          <Button onClick={run} disabled={!projectId || busy}>
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileDown className="h-4 w-4" />}
            生成导出产物
          </Button>
        }
      />

      <FormatSelector formats={formats} onChange={setFormats} />

      {result && (
        <CompileDiagnostics
          result={result}
          ok={compileOk}
          projectId={projectId}
          log={latest?.artifacts.find((a) => a.format === 'compile_log')}
        />
      )}

      <LoadState loading={loading} error={loadError} onRetry={runReload} skeletonClassName="h-64">
        {artifacts.length === 0 ? (
          <div className="rounded-xl border border-dashed py-16 text-center text-sm text-muted-foreground">
            {progress.sectionCount === 0 ? (
              <>
                还没有正文。先在
                <Link href={projectHref(projectId, 'write')} className="mx-1 underline">
                  写作工作台
                </Link>
                生成正文，再点「生成导出产物」。
              </>
            ) : (
              <>已有正文，还没有导出过。点右上角「生成导出产物」编译成 PDF。</>
            )}
          </div>
        ) : (
          <div className="space-y-6">
            <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr),20rem]">
              <div className="min-w-0 space-y-2">
                <h3 className="text-sm font-medium">当前产物预览</h3>
                {latestPdf ? (
                  <PdfPreviewPane projectId={projectId} artifact={latestPdf} />
                ) : (
                  <div className="rounded-xl border border-dashed py-16 text-center text-sm text-muted-foreground">
                    最近一次运行没有产出 PDF。LaTeX 工程与其余格式仍可在右侧下载。
                  </div>
                )}
              </div>

              <aside className="space-y-2 lg:sticky lg:top-4 lg:self-start">
                <h3 className="text-sm font-medium">
                  最近一次运行 · {formatDate(latest?.artifacts[0]?.created_at ?? undefined)}
                </h3>
                <Card>
                  <CardContent className="divide-y p-0">
                    {latest?.artifacts.map((artifact) => (
                      <ArtifactRow key={artifact.id} projectId={projectId} artifact={artifact} />
                    ))}
                  </CardContent>
                </Card>
                {!latest?.artifacts.some((a) => a.format === 'compile_log') && (
                  <p className="text-xs text-muted-foreground">
                    这次运行早于「编译日志登记为产物」的改动，因此没有日志条目；
                    重新导出一次即可拿到。
                  </p>
                )}
              </aside>
            </div>

            {runs.length > 1 && (
              <HistoryRuns projectId={projectId} runs={runs.slice(1)} />
            )}
          </div>
        )}
      </LoadState>

      <WorkbenchFooterNav current="export" />
    </div>
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
    <Card>
      <CardContent className="flex flex-wrap items-center gap-x-5 gap-y-2 py-3">
        <span className="text-xs font-medium text-muted-foreground">导出格式</span>
        {ALL_EXPORT_FORMATS.map((format) => (
          <label key={format} className="flex cursor-pointer items-center gap-2 text-sm">
            <Checkbox checked={formats.includes(format)} onCheckedChange={() => toggle(format)} />
            {FORMAT_LABEL[format]}
          </label>
        ))}
      </CardContent>
    </Card>
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
      <div className="overflow-hidden rounded-xl border">
        <iframe
          src={src}
          title="PDF 预览"
          className="h-[calc(100vh-22rem)] min-h-96 w-full bg-muted"
        />
      </div>
      {/* 不内嵌 PDF 阅读器的浏览器（部分移动端）拿不到上面的预览，给一条出路。 */}
      <p className="text-xs text-muted-foreground">
        预览为空？
        <a href={src} target="_blank" rel="noreferrer" className="mx-1 underline">
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
  const isLog = artifact.format === 'compile_log';
  return (
    <div className="flex items-center justify-between gap-3 px-3 py-2">
      <Badge variant={artifact.format === 'pdf' ? 'success' : isLog ? 'muted' : 'outline'}>
        {FORMAT_LABEL[artifact.format] ?? artifact.format}
      </Badge>
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

function HistoryRuns({ projectId, runs }: { projectId: string; runs: ExportRun[] }) {
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
                    {formatDate(run.artifacts[0]?.created_at ?? undefined)}
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
                    {run.artifacts.length} 个产物
                  </span>
                </button>
                {expanded && (
                  <div className="divide-y border-t bg-muted/20">
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
