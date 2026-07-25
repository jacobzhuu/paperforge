'use client';

import * as React from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { AlertTriangle, CheckCircle2, Download, FileDown, Loader2 } from 'lucide-react';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { DataSourceBanner } from '@/components/data-source-banner';
import {
  exportDownloadUrl,
  listExports,
  startExport,
  subscribeJobEvents,
} from '@/lib/api';
import type { DataSource, ExportArtifact, ExportFormat, Job } from '@/lib/types';
import { formatDate } from '@/lib/utils';

const FORMAT_LABEL: Record<ExportFormat, string> = {
  pdf: 'PDF',
  latex_zip: 'LaTeX 工程 (zip)',
  markdown: 'Markdown',
  bibtex: 'BibTeX',
  docx: 'Word (docx)',
};

export function ExportCenter() {
  const params = useSearchParams();
  const projectId = params.get('project') ?? '';

  const [artifacts, setArtifacts] = React.useState<ExportArtifact[]>([]);
  const [source, setSource] = React.useState<DataSource>('live');
  const [note, setNote] = React.useState<string | undefined>();
  const [loading, setLoading] = React.useState(true);
  const [job, setJob] = React.useState<Job | null>(null);
  const [jobStage, setJobStage] = React.useState<string | null>(null);
  const [result, setResult] = React.useState<Record<string, unknown> | null>(null);
  const [message, setMessage] = React.useState<string | null>(null);

  const reload = React.useCallback(async () => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    const rows = await listExports(projectId);
    setArtifacts(rows.data);
    setSource(rows.source);
    setNote(rows.note);
    setLoading(false);
  }, [projectId]);

  React.useEffect(() => {
    void reload();
  }, [reload]);

  const run = async () => {
    const started = await startExport(projectId);
    if (!started.data) {
      setMessage('后端不可用：无法触发导出');
      return;
    }
    setMessage(null);
    setResult(null);
    setJob(started.data);
    setJobStage('排队中');
    subscribeJobEvents(projectId, started.data.id, {
      onEvent: (event) => {
        setJobStage(event.stage ?? event.type);
        setJob((prev) => (prev ? { ...prev, progress: event.progress ?? prev.progress } : prev));
        if (event.type === 'render.completed') setResult(event.payload);
      },
      onClose: () => {
        setJob(null);
        setJobStage(null);
        void reload();
      },
    });
  };

  const compileOk = result?.compile_ok as boolean | undefined;
  const compileWarnings = (result?.warnings as Array<Record<string, unknown>>) ?? [];

  return (
    <div className="space-y-6">
      <PageHeader
        title="导出中心"
        description="LaTeX 工程 / PDF / Markdown / BibTeX 一键导出"
        actions={
          <Button onClick={run} disabled={!projectId || !!job}>
            {job ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileDown className="h-4 w-4" />}
            生成导出产物
          </Button>
        }
      />

      <DataSourceBanner source={source} note={note} />

      {job && (
        <Card>
          <CardContent className="flex items-center gap-3 py-3">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
            <div className="flex-1">
              <div className="flex items-center justify-between text-xs">
                <span className="font-medium">{jobStage ?? '编译中'}</span>
                <span className="text-muted-foreground">
                  {Math.round((job.progress ?? 0) * 100)}%
                </span>
              </div>
              <Progress value={Math.round((job.progress ?? 0) * 100)} className="mt-1.5" />
            </div>
          </CardContent>
        </Card>
      )}

      {message && (
        <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
          {message}
        </div>
      )}

      {result && (
        <Card>
          <CardContent className="flex items-start gap-3 py-4">
            {compileOk ? (
              <CheckCircle2 className="mt-0.5 h-5 w-5 text-success" />
            ) : (
              <AlertTriangle className="mt-0.5 h-5 w-5 text-warning" />
            )}
            <div className="space-y-1 text-sm">
              <p className="font-medium">
                {compileOk ? 'PDF 编译成功' : 'PDF 未编译成功，已交付 LaTeX 工程与编译日志'}
              </p>
              <p className="text-xs text-muted-foreground">
                模板 {String(result.template ?? '—')} · 修复轮次 {String(result.compile_rounds ?? 0)}
              </p>
              {compileWarnings.map((warning, index) => (
                <p key={index} className="text-xs text-warning">
                  {String(warning.stage)}：{String(warning.reason)}
                </p>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {!projectId ? (
        <EmptyHint />
      ) : loading ? (
        <div className="h-40 animate-pulse rounded-xl border bg-muted/40" />
      ) : artifacts.length === 0 ? (
        <div className="rounded-xl border border-dashed py-16 text-center text-sm text-muted-foreground">
          还没有导出产物。先在
          <Link href={`/write?project=${projectId}`} className="mx-1 underline">
            写作工作台
          </Link>
          生成正文，再点「生成导出产物」。
        </div>
      ) : (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">产物列表</CardTitle>
          </CardHeader>
          <CardContent className="px-0 pb-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>格式</TableHead>
                  <TableHead className="w-24">文稿版本</TableHead>
                  <TableHead className="w-44">生成时间</TableHead>
                  <TableHead className="w-28">下载</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {artifacts.map((artifact) => (
                  <TableRow key={artifact.id}>
                    <TableCell>
                      <Badge variant={artifact.format === 'pdf' ? 'success' : 'outline'}>
                        {FORMAT_LABEL[artifact.format] ?? artifact.format}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs tabular-nums text-muted-foreground">
                      v{artifact.document_version ?? 1}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {formatDate(artifact.created_at ?? undefined)}
                    </TableCell>
                    <TableCell>
                      <a
                        href={exportDownloadUrl(projectId, artifact.id)}
                        className="inline-flex items-center gap-1 text-xs underline"
                      >
                        <Download className="h-3.5 w-3.5" /> 下载
                      </a>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function EmptyHint() {
  return (
    <div className="rounded-xl border border-dashed py-16 text-center text-sm text-muted-foreground">
      请先从
      <Link href="/projects" className="mx-1 underline">
        项目列表
      </Link>
      进入某个项目。
    </div>
  );
}
