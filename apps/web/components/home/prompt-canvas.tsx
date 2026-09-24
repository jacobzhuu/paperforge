'use client';

import { projectActivity } from '@/lib/project-activity';

import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ArrowRight, BookOpen, CheckCircle2, FileText, FlaskConical, Loader2, Paperclip, RefreshCw, Settings2, X } from 'lucide-react';
import { ContributionEditor } from '@/components/projects/contribution-editor';
import { Button } from '@/components/ui/button';
import { Callout } from '@/components/ui/callout';
import { Dialog } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { FastDraftToggle } from '@/components/ui/fast-draft-toggle';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { createProject, submitIntake, getAssetCapabilities, listProjects, uploadAsset } from '@/lib/api';
import { describeError, networkHelp } from '@/lib/errors';
import { CITATION_STYLE_LABEL, LANGUAGE_LABEL, VENUE_TEMPLATES } from '@/lib/labels';
import type { AssetCapabilities, CitationStyle, ExecutionProfile, Language, PaperType, Project, UserAsset, WritingMode } from '@/lib/types';
import { cn, formatDate } from '@/lib/utils';

/** 从一段自由描述里取出一个像样的题目。 */
function deriveTitle(text: string): string {
  const first = text
    .trim()
    .split(/\n|[。！？.!?]/)
    .map((s) => s.trim())
    .find(Boolean);
  const raw = (first || text.trim()).replace(/^(我想|帮我|请)?(写一篇|写个|做一篇|研究)?\s*/, '');
  // 题目栏放不下一整段话；截断到 60 字，用户可以在项目页就地改名。
  return raw.length > 60 ? `${raw.slice(0, 60)}…` : raw || '未命名论文';
}

function greeting(): string {
  const h = new Date().getHours();
  if (h < 5) return '夜深了';
  if (h < 12) return '早上好';
  if (h < 14) return '中午好';
  if (h < 18) return '下午好';
  return '晚上好';
}

type UploadStatus = 'queued' | 'uploading' | 'received' | 'warning' | 'failed' | 'invalid';
type UploadItemState = { status: UploadStatus; error?: string; asset?: UserAsset };
const fileKey = (file: File) => `${file.name}:${file.size}:${file.lastModified}`;
const formatBytes = (bytes: number) => bytes < 1024 * 1024
  ? `${Math.max(1, Math.round(bytes / 1024))} KiB`
  : `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
const titleFromFile = (file: File) => file.name.replace(/\.[^.]+$/, '').trim() || '未命名论文';
function uploadStatusLabel(status: UploadStatus): string {
  return {
    queued: '排队中',
    uploading: '上传中',
    received: '已接收',
    warning: '已接收，有解析警告',
    failed: '上传失败',
    invalid: '本地校验未通过',
  }[status];
}

/** 从研究意图开始；设置按需打开，提交后交给持久化的理解任务。 */
export function PromptCanvas() {
  const router = useRouter();

  const [text, setText] = React.useState('');
  const [paperType, setPaperType] = React.useState<PaperType | 'auto'>('auto');
  const [files, setFiles] = React.useState<File[]>([]);
  const [uploadStates, setUploadStates] = React.useState<Record<string, UploadItemState>>({});
  const [capabilities, setCapabilities] = React.useState<AssetCapabilities | null>(null);
  const [createdProject, setCreatedProject] = React.useState<Project | null>(null);
  const [dragging, setDragging] = React.useState(false);
  const [showSettings, setShowSettings] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const [language, setLanguage] = React.useState<Language>('zh');
  const [citationStyle, setCitationStyle] = React.useState<CitationStyle>('gbt7714');
  const [venueTemplate, setVenueTemplate] = React.useState('article');
  const [writingMode, setWritingMode] = React.useState<WritingMode>('assisted');
  const [executionProfile, setExecutionProfile] = React.useState<ExecutionProfile>('standard');
  const [languagePinned, setLanguagePinned] = React.useState(false);
  const [submissionTarget, setSubmissionTarget] = React.useState('');
  const submittingRef = React.useRef(false);
  const [contributionPoints, setContributionPoints] = React.useState<string[]>(['']);

  const [recent, setRecent] = React.useState<Project[] | null>(null);
  const fileInputRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    let alive = true;
    listProjects()
      .then((res) => alive && setRecent(res.data.slice(0, 5)))
      .catch(() => alive && setRecent([]));
    return () => {
      alive = false;
    };
  }, []);

  React.useEffect(() => {
    const controller = new AbortController();
    getAssetCapabilities(controller.signal)
      .then(setCapabilities)
      .catch(() => setCapabilities(null));
    return () => controller.abort();
  }, []);

  React.useEffect(() => {
    if (!submitting) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [submitting]);

  React.useEffect(() => {
    const preset = new URLSearchParams(window.location.search).get('type');
    if (preset === 'review' || preset === 'original') {
      pickType(preset);
      setShowSettings(true);
    }
  }, []);

  const addFiles = (incoming: File[]) => {
    if (incoming.length === 0) return;
    const unique = incoming.filter((file) => !files.some((existing) => fileKey(existing) === fileKey(file)));
    setFiles((prev) => [...prev, ...unique]);
    setUploadStates((current) => {
      const next = { ...current };
      for (const file of unique) {
        const invalid = file.size === 0
          ? '空文件无法上传'
          : capabilities && file.size > capabilities.max_bytes
            ? `超过 ${capabilities.max_mib} MiB 上限`
            : undefined;
        next[fileKey(file)] = { status: invalid ? 'invalid' : 'queued', error: invalid };
      }
      return next;
    });
  };

  const pickType = (next: PaperType | 'auto') => setPaperType(next);
  const hasUsableFile = files.some((file) => uploadStates[fileKey(file)]?.status !== 'invalid');
  const canSubmit = !submitting && (text.trim().length > 0 || hasUsableFile)
    && !files.some((file) => ['invalid', 'uploading'].includes(uploadStates[fileKey(file)]?.status ?? ''));

  const uploadOne = async (projectId: string, file: File): Promise<boolean> => {
    const key = fileKey(file);
    setUploadStates((current) => ({ ...current, [key]: { status: 'uploading' } }));
    try {
      const uploaded = await uploadAsset(projectId, file);
      if (!uploaded.data) throw new Error(uploaded.note ?? '服务未接收文件');
      const warning = (uploaded.data.warnings?.length ?? 0) > 0;
      setUploadStates((current) => ({
        ...current,
        [key]: { status: warning ? 'warning' : 'received', asset: uploaded.data },
      }));
      return true;
    } catch (uploadError) {
      setUploadStates((current) => ({
        ...current,
        [key]: { status: 'failed', error: describeError(uploadError) },
      }));
      return false;
    }
  };

  const submit = async () => {
    if (!canSubmit || submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    setError(null);
    const topic = text.trim();
    try {
      const res = createdProject ? { data: createdProject, source: 'live' as const } : await createProject({
        title: topic ? deriveTitle(topic) : titleFromFile(files[0]),
        intake: {
          ...(paperType !== 'auto' ? { paper_type: paperType } : {}),
          ...(languagePinned ? { language } : {}),
          ...(submissionTarget.trim() ? { submission_target: submissionTarget.trim() } : {}),
        },
        paper_type: paperType === 'auto' ? 'review' : paperType,
        writing_mode: writingMode,
        execution_profile: executionProfile,
        language,
        topic: topic || undefined,
        venue_template: venueTemplate,
        citation_style: citationStyle,
        contribution_points:
          paperType === 'original'
            ? contributionPoints.map((point) => point.trim()).filter(Boolean)
            : undefined,
      });
      if (res.source === 'mock') {
        // createProject 降级时返回的是本地乐观桩（id: local-…）。导航过去会让用户
        // 进入一个并不存在的项目，还会看到示例文献——宁可停在这里说清楚。
        setError(
          `项目未创建：${res.note ?? '后端不可用'}。${networkHelp()}`,
        );
        setSubmitting(false);
        return;
      }
      setCreatedProject(res.data);
      // 最多三个并发 worker；已成功文件不重复上传，单个失败也不取消其它文件。
      const pending = files.filter((file) => {
        const status = uploadStates[fileKey(file)]?.status;
        return status !== 'received' && status !== 'warning' && status !== 'invalid';
      });
      let cursor = 0;
      const results: boolean[] = [];
      await Promise.all(Array.from({ length: Math.min(3, pending.length) }, async () => {
        while (cursor < pending.length) {
          const file = pending[cursor++];
          results.push(await uploadOne(res.data.id, file));
        }
      }));
      const failed = results.some((ok) => !ok);
      const target = `/projects/${res.data.id}`;
      if (failed) {
        setError('项目已创建，部分文件上传失败。成功文件已保留，请单独重试失败项或进入素材中心继续。');
        setSubmitting(false);
        return;
      }
      await submitIntake(res.data.id, { version: 0 });
      router.push(target);
    } catch (err) {
      setError(describeError(err));
      setSubmitting(false);
    } finally {
      submittingRef.current = false;
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col px-4 pb-16 pt-8 md:pt-[8vh]">
      <h1 className="text-center font-serif text-display font-semibold tracking-tight">
        {greeting()}
      </h1>
      <p className="mt-2 text-center font-serif text-heading text-muted-foreground">
        说说你的研究问题，或上传已有材料
      </p>

      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          addFiles(Array.from(e.dataTransfer.files));
        }}
        className={cn(
          'mt-8 rounded-xl border bg-card shadow-sm transition-colors focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/15',
          dragging && 'border-primary bg-accent/40',
        )}
      >
        <label htmlFor="intent" className="sr-only">
          描述你的研究主题、问题或论文目标
        </label>
        <textarea
          id="intent"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // Enter 换行，⌘/Ctrl+Enter 提交——这是个多行输入框，回车直接提交会误伤。
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void submit();
          }}
          rows={4}
          disabled={submitting || Boolean(createdProject)}
          placeholder="描述研究目标、已有材料和希望得到的成果……"
          aria-invalid={Boolean(error)}
          aria-describedby={error ? 'intent-help create-project-error' : 'intent-help'}
          className="w-full resize-none bg-transparent px-4 pt-4 text-body placeholder:text-muted-foreground focus-visible:outline-none"
        />

        {files.length > 0 && (
          <ul className="space-y-1.5 px-4 pb-2">
            {files.map((file, i) => (
              <li
                key={fileKey(file)}
                className="flex items-center gap-2 rounded-md border bg-muted/40 px-2 py-1.5 text-meta"
              >
                <FileText className="h-3 w-3 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{file.name}</span>
                  <span className="text-muted-foreground">
                    {file.type || '未知类型'} · {formatBytes(file.size)} ·{' '}
                    {uploadStatusLabel(uploadStates[fileKey(file)]?.status ?? 'queued')}
                  </span>
                  {uploadStates[fileKey(file)]?.error && (
                    <span className="block text-destructive">{uploadStates[fileKey(file)]?.error}</span>
                  )}
                </span>
                {uploadStates[fileKey(file)]?.status === 'uploading' && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                {['received', 'warning'].includes(uploadStates[fileKey(file)]?.status ?? '') && <CheckCircle2 className="h-3.5 w-3.5 text-success-strong" />}
                {uploadStates[fileKey(file)]?.status === 'failed' && createdProject && (
                  <button
                    type="button"
                    onClick={() => void uploadOne(createdProject.id, file)}
                    aria-label={`重试 ${file.name}`}
                    className="relative z-10 flex h-8 items-center gap-1 rounded px-2 text-primary hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <RefreshCw className="h-3 w-3" /> 重试
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => {
                    setFiles((prev) => prev.filter((_, j) => j !== i));
                    setUploadStates((current) => {
                      const next = { ...current };
                      delete next[fileKey(file)];
                      return next;
                    });
                  }}
                  disabled={submitting || ['uploading', 'received', 'warning'].includes(uploadStates[fileKey(file)]?.status ?? '')}
                  aria-label={`移除 ${file.name}`}
                  className="flex h-11 w-11 items-center justify-center rounded text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:h-8 md:w-8"
                >
                  <X className="h-3 w-3" />
                </button>
              </li>
            ))}
          </ul>
        )}

        <div className="flex flex-wrap items-center justify-between gap-2 px-3 pb-3 pt-1">
          <div className="flex flex-wrap items-center gap-1">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              onChange={(e) => {
                addFiles(Array.from(e.target.files ?? []));
                e.target.value = '';
              }}
            />
            <Button variant="ghost" size="sm" disabled={submitting || Boolean(createdProject)} onClick={() => fileInputRef.current?.click()}>
              <Paperclip className="h-3.5 w-3.5" /> 上传材料
            </Button>
            <FastDraftToggle value={executionProfile} onChange={setExecutionProfile} disabled={submitting || Boolean(createdProject)} />
          </div>
          <Button
            className="w-full sm:w-auto"
            onClick={() => void submit()}
            disabled={!canSubmit}
            loading={submitting}
            loadingLabel="正在保存并启动研究…"
          >
            <ArrowRight className="h-4 w-4" />
            {createdProject ? '继续开始研究' : '开始研究'}
          </Button>
        </div>

      </div>
      <div className="mt-2 flex items-center justify-between text-sm text-muted-foreground">
        <span>{writingMode === 'auto' ? '全自动' : '协作'} · {LANGUAGE_LABEL[language]}{!languagePinned && '（默认）'}</span>
        <Button variant="ghost" size="sm" onClick={() => setShowSettings(true)} disabled={submitting || Boolean(createdProject)}>
          <Settings2 className="h-3.5 w-3.5" /> 调整
        </Button>
      </div>
      <Dialog open={showSettings} onClose={() => setShowSettings(false)} title="调整研究方案"
        description="这些设置无需提前确定。描述中的明确要求会在提交后识别，手动设置优先。"
        footer={<Button onClick={() => setShowSettings(false)}>完成</Button>}>
        <div className="space-y-5">
          <fieldset className="space-y-2">
            <legend className="mb-2 text-sm font-medium">合作方式</legend>
            {([
              ['assisted', '协作', '先理解目标并提出规划，由你决定何时继续检索与写作。'],
              ['auto', '全自动', '方向明确、材料齐备后继续生成论文；有歧义时仍会请你澄清。'],
            ] as const).map(([value, label, hint]) => (
              <label key={value} className="flex cursor-pointer items-start gap-3 rounded-md border p-3 has-[:checked]:bg-accent">
                <input className="mt-1 accent-current" type="radio" name="writing-mode" value={value}
                  checked={writingMode === value} onChange={() => setWritingMode(value)} />
                <span><span className="block text-sm font-medium">{label}</span><span className="text-xs text-muted-foreground">{hint}</span></span>
              </label>
            ))}
          </fieldset>
          <Field label="语言" id="lang">
            <Select id="lang" value={languagePinned ? language : 'auto'} onChange={(e) => {
              setLanguagePinned(e.target.value !== 'auto');
              setLanguage(e.target.value === 'auto' ? 'zh' : e.target.value as Language);
            }}>
              <option value="auto">按描述判断，默认中文</option>
              <option value="zh">中文</option><option value="en">English</option>
            </Select>
          </Field>
          <Field label="论文类型" id="paper-type">
            <Select id="paper-type" value={paperType} onChange={(e) => pickType(e.target.value as PaperType | 'auto')}>
              <option value="auto">根据研究目标判断</option>
              <option value="review">综述论文</option><option value="original">研究型论文</option>
            </Select>
          </Field>
          <details className="space-y-3">
            <summary className="cursor-pointer text-sm font-medium">投稿要求（可稍后修改）</summary>
            <Field label="目标期刊或会议" id="submission-target">
              <Input id="submission-target" value={submissionTarget} onChange={(e) => setSubmissionTarget(e.target.value)} placeholder="可选，也可以直接写在研究描述中" />
            </Field>
            <Field label="引用样式" id="cite">
              <Select id="cite" value={citationStyle} onChange={(e) => setCitationStyle(e.target.value as CitationStyle)}>
                {(Object.keys(CITATION_STYLE_LABEL) as CitationStyle[]).map((c) => <option key={c} value={c}>{CITATION_STYLE_LABEL[c]}</option>)}
              </Select>
            </Field>
            <Field label="投稿模板" id="tpl">
              <Select id="tpl" value={venueTemplate} onChange={(e) => setVenueTemplate(e.target.value)}>
                {VENUE_TEMPLATES.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
              </Select>
            </Field>
            <p className="text-xs text-muted-foreground">投稿目标用于规划；具体排版可在导出前确认。</p>
          </details>
          {paperType === 'original' && <details><summary className="cursor-pointer text-sm">贡献点（可选）</summary><ContributionEditor points={contributionPoints} onChange={setContributionPoints} /></details>}
        </div>
      </Dialog>

      {recent?.length === 0 && !createdProject && (
        <div className="mt-3 space-y-2 text-center text-meta text-muted-foreground">
          <p>描述研究目标，或直接上传已有材料。</p>
          <div className="flex flex-wrap justify-center gap-2" aria-label="研究意图示例">
            {[
              '比较近五年大语言模型事实一致性评估方法',
              '系统综述可解释医学影像中的证据与局限',
              '根据上传的实验结果撰写研究型论文',
            ].map((example) => (
              <button
                key={example}
                type="button"
                disabled={submitting}
                onClick={() => {
                  setText(current => current.trim() ? `${current}\n${example}` : example);
                  document.getElementById('intent')?.focus();
                }}
                className="min-h-11 rounded-lg border px-3 py-2 text-left leading-relaxed hover:bg-accent disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {example}
              </button>
            ))}
          </div>
        </div>
      )}

      <p id="intent-help" className="mt-4 text-center text-sm leading-relaxed text-muted-foreground">
        开始后会先理解目标并形成研究规划；需要补充的信息会在项目中提示。
      </p>
      <div className="mt-2 text-center text-meta text-muted-foreground">
        <p>支持文献、数据与代码{capabilities ? `，单文件最大 ${capabilities.max_mib} MiB。` : '。'}</p>
        {capabilities && <details className="mt-1"><summary className="cursor-pointer">查看支持格式</summary>
          <p className="mt-1">{capabilities.preferred_extensions.join('、')}。其它类型会保留并尝试作为方法备注解析。</p>
        </details>}
      </div>

      {error && (
        <Callout id="create-project-error" role="alert" variant="error" className="mt-3">
          {error}
          {createdProject && <Link className="ml-2 underline" href={`/projects/${createdProject.id}`}>进入已保存的项目</Link>}
        </Callout>
      )}

      <RecentPapers projects={recent} />
    </div>
  );
}

function Field({
  label,
  id,
  children,
  className,
}: {
  label: string;
  id: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('space-y-1.5', className)}>
      <Label htmlFor={id} className="text-meta text-muted-foreground">
        {label}
      </Label>
      {children}
    </div>
  );
}

/** 最近的论文：一列题目 + 时间，不是卡片网格（原则 07）。 */
function RecentPapers({ projects }: { projects: Project[] | null }) {
  if (projects === null) {
    return (
      <div className="mt-12 space-y-3">
        <Skeleton className="h-4 w-20" />
        <Skeleton className="h-5 w-2/3" />
        <Skeleton className="h-5 w-1/2" />
      </div>
    );
  }
  if (projects.length === 0) {
    return (
      <section className="mt-12 space-y-6 border-t pt-8" aria-labelledby="first-paper-title">
        <div>
          <h2 id="first-paper-title" className="font-serif text-subheading font-semibold">
            从一种工作方式开始
          </h2>
          <p className="mt-1 text-meta text-muted-foreground">
            说明你的目标，我们会先理解需求，再提出研究方案。
          </p>
        </div>
        <div className="grid gap-6 sm:grid-cols-2">
          <div className="space-y-2">
            <BookOpen className="h-5 w-5 text-muted-foreground" />
            <h3 className="font-medium">综述论文</h3>
            <p className="text-meta text-muted-foreground">
              从研究主题出发，检索并核验真实文献，再生成大纲、正文与 LaTeX/PDF。
            </p>
          </div>
          <div className="space-y-2">
            <FlaskConical className="h-5 w-5 text-muted-foreground" />
            <h3 className="font-medium">研究型论文</h3>
            <p className="text-meta text-muted-foreground">
              上传实验素材并说明贡献点，完成相关工作、IMRaD 写作与数字一致性检查。
            </p>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="mt-12 space-y-1">
      <div className="flex items-baseline justify-between">
        <h2 className="text-meta font-semibold uppercase tracking-wider text-muted-foreground">
          最近的论文
        </h2>
        <Link
          href="/projects"
          className="rounded text-meta text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          全部项目
        </Link>
      </div>
      <ul className="divide-y">
        {projects.map((project) => (
          <li key={project.id}>
            <Link
              href={`/projects/${project.id}`}
              className="flex flex-col gap-2 rounded-md px-2 py-4 sm:flex-row sm:items-baseline sm:justify-between sm:gap-4 transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate font-serif text-subheading">{project.title}</span>
                <span className="block text-meta text-muted-foreground">{
                  projectActivity(project)
                }</span>
              </span>
              <span className="shrink-0 text-meta text-muted-foreground">
                最近编辑 {formatDate(project.updated_at)} · 继续
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
