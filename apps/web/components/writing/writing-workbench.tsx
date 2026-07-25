'use client';

import * as React from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { CheckCircle2, FileText, Loader2, PenLine, Save, ShieldCheck } from 'lucide-react';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { DataSourceBanner } from '@/components/data-source-banner';
import { SectionEditor } from '@/components/writing/section-editor';
import {
  generateSections,
  getCitationAudit,
  getMarkdownPreview,
  getQuality,
  getWhitelist,
  listSections,
  subscribeJobEvents,
  updateSection,
} from '@/lib/api';
import type {
  CitationAudit,
  DataSource,
  Job,
  MarkdownPreview,
  PaperSection,
  QualityReport,
  SectionIR,
} from '@/lib/types';
import { cn } from '@/lib/utils';

type View = 'editor' | 'preview' | 'audit' | 'quality';

export function WritingWorkbench() {
  const params = useSearchParams();
  const projectId = params.get('project') ?? '';

  const [sections, setSections] = React.useState<PaperSection[]>([]);
  const [activeKey, setActiveKey] = React.useState<string | null>(null);
  const [whitelist, setWhitelist] = React.useState<string[]>([]);
  const [audit, setAudit] = React.useState<CitationAudit | undefined>();
  const [preview, setPreview] = React.useState<MarkdownPreview | undefined>();
  const [quality, setQuality] = React.useState<QualityReport | undefined>();
  const [view, setView] = React.useState<View>('editor');
  const [source, setSource] = React.useState<DataSource>('live');
  const [note, setNote] = React.useState<string | undefined>();
  const [loading, setLoading] = React.useState(true);
  const [draft, setDraft] = React.useState<SectionIR | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [job, setJob] = React.useState<Job | null>(null);
  const [jobStage, setJobStage] = React.useState<string | null>(null);
  const [message, setMessage] = React.useState<string | null>(null);

  const reload = React.useCallback(async () => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    const [rows, wl, auditResult, previewResult, qualityResult] = await Promise.all([
      listSections(projectId),
      getWhitelist(projectId),
      getCitationAudit(projectId),
      getMarkdownPreview(projectId),
      getQuality(projectId),
    ]);
    setSections(rows.data);
    setWhitelist(wl.data);
    setAudit(auditResult.data);
    setPreview(previewResult.data);
    setQuality(qualityResult.data);
    setSource(rows.source);
    setNote(rows.note);
    setActiveKey((current) => current ?? rows.data[0]?.section_key ?? null);
    setLoading(false);
  }, [projectId]);

  React.useEffect(() => {
    void reload();
  }, [reload]);

  const active = sections.find((s) => s.section_key === activeKey);

  React.useEffect(() => {
    if (!active) {
      setDraft(null);
      return;
    }
    const body = active.body_ir as SectionIR;
    setDraft(
      body && 'blocks' in body
        ? body
        : {
            key: active.section_key,
            level: 1,
            title: active.title,
            blocks: [],
            citation_warnings: [],
          },
    );
  }, [active]);

  const save = async () => {
    if (!draft || !active || !projectId) return;
    setSaving(true);
    const result = await updateSection(projectId, active.section_key, draft, draft.title);
    setSaving(false);
    if (result.data) {
      // 服务端会再过一次白名单（R2 兜底），以返回值为准。
      setSections((prev) =>
        prev.map((s) => (s.section_key === result.data!.section_key ? result.data! : s)),
      );
      void reload();
    } else {
      setMessage('后端不可用：本次编辑未保存');
    }
  };

  const startWriting = async () => {
    const started = await generateSections(projectId, true);
    if (!started.data) {
      setMessage('后端不可用：无法开始写作');
      return;
    }
    setJob(started.data);
    setJobStage('排队中');
    subscribeJobEvents(projectId, started.data.id, {
      onEvent: (event) => {
        setJobStage(
          event.type === 'write.section'
            ? `写作：${String((event.payload as { title?: string }).title ?? '')}`
            : (event.stage ?? event.type),
        );
        setJob((prev) => (prev ? { ...prev, progress: event.progress ?? prev.progress } : prev));
      },
      onClose: () => {
        setJob(null);
        setJobStage(null);
        void reload();
      },
    });
  };

  const totalWords = sections.reduce((sum, s) => sum + s.word_count, 0);
  const hallucinated = audit?.hallucinated_cite_keys ?? [];

  return (
    <div className="space-y-6">
      <PageHeader
        title="写作工作台"
        description={`共 ${sections.length} 节 · ${totalWords.toLocaleString()} 字`}
        actions={
          <>
            <Button variant="outline" onClick={save} disabled={!draft || saving || !!job}>
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              保存本节
            </Button>
            <Button onClick={startWriting} disabled={!projectId || !!job}>
              {job ? <Loader2 className="h-4 w-4 animate-spin" /> : <PenLine className="h-4 w-4" />}
              重新生成全文
            </Button>
          </>
        }
      />

      <DataSourceBanner source={source} note={note} />

      {job && (
        <Card>
          <CardContent className="flex items-center gap-3 py-3">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
            <div className="flex-1">
              <div className="flex items-center justify-between text-xs">
                <span className="font-medium">{jobStage ?? '进行中'}</span>
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

      {!projectId ? (
        <EmptyHint />
      ) : loading ? (
        <div className="h-64 animate-pulse rounded-xl border bg-muted/40" />
      ) : sections.length === 0 ? (
        <div className="rounded-xl border border-dashed py-16 text-center text-sm text-muted-foreground">
          还没有正文。先在
          <Link href={`/outline?project=${projectId}`} className="mx-1 underline">
            大纲编辑器
          </Link>
          生成大纲，再点「重新生成全文」。
        </div>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[14rem,1fr,18rem]">
          <nav className="space-y-1">
            {sections.map((section) => (
              <button
                key={section.section_key}
                onClick={() => setActiveKey(section.section_key)}
                className={cn(
                  'flex w-full items-center justify-between rounded-md px-2.5 py-2 text-left text-sm',
                  section.section_key === activeKey
                    ? 'bg-secondary font-medium'
                    : 'hover:bg-muted/60',
                )}
              >
                <span className="line-clamp-1">{section.title}</span>
                <span className="ml-2 shrink-0 text-xs tabular-nums text-muted-foreground">
                  {section.word_count}
                </span>
              </button>
            ))}
          </nav>

          <div className="space-y-4">
            <Tabs value={view} onValueChange={(v) => setView(v as View)}>
              <TabsList>
                <TabsTrigger value="editor">编辑</TabsTrigger>
                <TabsTrigger value="preview">Markdown 预览</TabsTrigger>
                <TabsTrigger value="audit">引用审计</TabsTrigger>
                <TabsTrigger value="quality">质量报告</TabsTrigger>
              </TabsList>
            </Tabs>

            {view === 'editor' && draft && (
              <SectionEditor
                section={draft}
                whitelist={whitelist}
                onChange={setDraft}
                projectId={projectId}
                softChecks={quality?.soft_check ?? []}
              />
            )}
            {view === 'preview' && (
              <Card>
                <CardContent className="max-h-[70vh] overflow-y-auto py-4">
                  <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed">
                    {preview?.markdown || '（暂无预览）'}
                  </pre>
                </CardContent>
              </Card>
            )}
            {view === 'audit' && <AuditPanel audit={audit} />}
            {view === 'quality' && <QualityPanel report={quality} />}
          </div>

          <aside className="space-y-4">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center gap-1.5 text-sm">
                  <ShieldCheck className="h-4 w-4" /> 引用审计
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-xs">
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">幻觉引用</span>
                  <Badge variant={hallucinated.length === 0 ? 'success' : 'destructive'}>
                    {hallucinated.length === 0 ? '0 条' : `${hallucinated.length} 条`}
                  </Badge>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">已用 / 白名单</span>
                  <span className="tabular-nums">
                    {audit?.used_cite_keys.length ?? 0} / {audit?.whitelist_size ?? 0}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">被移除的引用</span>
                  <span className="tabular-nums">
                    {audit?.removed_citation_warnings.length ?? 0}
                  </span>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center gap-1.5 text-sm">
                  <FileText className="h-4 w-4" /> 本节引用
                </CardTitle>
              </CardHeader>
              <CardContent className="flex flex-wrap gap-1">
                {(active?.cite_keys ?? []).map((key) => (
                  <Badge key={key} variant="outline" className="font-mono text-[11px]">
                    {key}
                  </Badge>
                ))}
                {(active?.cite_keys ?? []).length === 0 && (
                  <span className="text-xs text-muted-foreground">本节暂无引用</span>
                )}
              </CardContent>
            </Card>
          </aside>
        </div>
      )}
    </div>
  );
}

function AuditPanel({ audit }: { audit: CitationAudit | undefined }) {
  if (!audit) {
    return <p className="text-sm text-muted-foreground">暂无审计数据。</p>;
  }
  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="flex items-center gap-3 py-4">
          {audit.hallucinated_cite_keys.length === 0 ? (
            <>
              <CheckCircle2 className="h-5 w-5 text-success" />
              <div className="text-sm">
                <p className="font-medium">0 幻觉引用</p>
                <p className="text-xs text-muted-foreground">
                  全文 {audit.used_cite_keys.length} 个引用键全部在写作白名单内（R2）。
                </p>
              </div>
            </>
          ) : (
            <div className="text-sm text-destructive">
              <p className="font-medium">发现 {audit.hallucinated_cite_keys.length} 个越权引用</p>
              <p className="font-mono text-xs">{audit.hallucinated_cite_keys.join(', ')}</p>
            </div>
          )}
        </CardContent>
      </Card>

      {audit.removed_citation_warnings.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">已移除的引用</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-xs">
            {audit.removed_citation_warnings.map((warning, index) => (
              <p key={index}>
                {warning.section_key ? `[${warning.section_key}] ` : ''}
                {warning.message}
                <span className="ml-1 font-mono">{warning.rejected_keys.join(', ')}</span>
              </p>
            ))}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">未被引用的入库文献（{audit.unused_cite_keys.length}）</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-1">
          {audit.unused_cite_keys.map((key) => (
            <Badge key={key} variant="muted" className="font-mono text-[11px]">
              {key}
            </Badge>
          ))}
          {audit.unused_cite_keys.length === 0 && (
            <span className="text-xs text-muted-foreground">全部文献均已被引用。</span>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function QualityPanel({ report }: { report: QualityReport | undefined }) {
  if (!report) return <p className="text-sm text-muted-foreground">暂无质量报告。</p>;
  const metrics = [
    { label: '总字数', value: report.word_count.toLocaleString() },
    { label: '引用条数', value: String(report.cite_count) },
    { label: '每千字引用', value: report.citation_density.toFixed(1) },
    { label: '文献利用率', value: `${Math.round(report.library_coverage * 100)}%` },
    { label: '近 5 年占比', value: `${Math.round(report.recent_ratio * 100)}%` },
    { label: '全文卡片覆盖', value: `${Math.round(report.fulltext_coverage * 100)}%` },
  ];
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">质量评分（仅提示，不设门槛）</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-3 gap-3">
          {metrics.map((metric) => (
            <div key={metric.label} className="rounded-md border p-2 text-center">
              <p className="text-lg font-semibold tabular-nums">{metric.value}</p>
              <p className="text-xs text-muted-foreground">{metric.label}</p>
            </div>
          ))}
        </CardContent>
      </Card>

      {report.hints.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">覆盖建议</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-xs">
            {report.hints.map((hint, index) => (
              <p key={index}>· {hint.message}</p>
            ))}
          </CardContent>
        </Card>
      )}

      {report.soft_check.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">语义引用软校验（弱相关）</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5 text-xs">
            {report.soft_check.map((finding, index) => (
              <p key={index}>
                <Badge variant="warning" className="mr-1.5 font-mono">
                  {finding.cite_key}
                </Badge>
                {finding.reason}
              </p>
            ))}
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
