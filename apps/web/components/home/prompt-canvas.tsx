'use client';

import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { AlertCircle, ArrowRight, BookOpen, CheckCircle2, ChevronDown, FileText, FlaskConical, Loader2, Paperclip, RefreshCw, Settings2, X } from 'lucide-react';
import { ContributionEditor } from '@/components/projects/contribution-editor';
import { Button } from '@/components/ui/button';
import { Callout } from '@/components/ui/callout';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { createProject, generateAll, getAssetCapabilities, getMaterialPreflight, listProjects, uploadAsset } from '@/lib/api';
import { describeError, networkHelp } from '@/lib/errors';
import { CITATION_STYLE_LABEL, LANGUAGE_LABEL, VENUE_TEMPLATES } from '@/lib/labels';
import type { AssetCapabilities, CitationStyle, Language, MaterialPreflight, PaperType, Project, UserAsset, WritingMode } from '@/lib/types';
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

/**
 * 首页：从研究意图开始，而不是从功能导航开始（docs/ui-design.md 原则 02 / §3.2）。
 *
 * 此前 `app/page.tsx` 直接 redirect 到 `/projects`——一个带搜索框和两个筛选下拉的
 * SaaS 卡片网格。那个页面回答的是「我有哪些项目」，但用户打开 PaperForge 时
 * 想做的第一件事是**开始一篇论文**。
 *
 * 类型选择降级为输入框内的下拉，不再是两张对等的大卡片；模板 / 语言 / 引用样式 /
 * 写作模式收进「更多设置」（原则 06 Progressive disclosure）。
 */
export function PromptCanvas() {
  const router = useRouter();

  const [text, setText] = React.useState('');
  const [paperType, setPaperType] = React.useState<PaperType>('review');
  /**
   * 用户是否手动选过类型——选过之后拖文件不再自动改写它。
   *
   * 用 ref 而不是 state：这个值只在事件回调里读，从不参与渲染。用 state 时
   * `addFiles` 读到的是**本次渲染闭包里的旧值**，「点完类型立刻拖文件」会漏判
   * （实测：选中综述后马上拖一个 csv，类型仍被改回研究型）。
   */
  const typePinned = React.useRef(false);
  const [files, setFiles] = React.useState<File[]>([]);
  const [uploadStates, setUploadStates] = React.useState<Record<string, UploadItemState>>({});
  const [capabilities, setCapabilities] = React.useState<AssetCapabilities | null>(null);
  const [createdProject, setCreatedProject] = React.useState<Project | null>(null);
  const [materialPreflight, setMaterialPreflight] = React.useState<MaterialPreflight | null>(null);
  const [dragging, setDragging] = React.useState(false);
  const [showSettings, setShowSettings] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const [language, setLanguage] = React.useState<Language>('zh');
  const [citationStyle, setCitationStyle] = React.useState<CitationStyle>('gbt7714');
  const [venueTemplate, setVenueTemplate] = React.useState('article');
  const [writingMode, setWritingMode] = React.useState<WritingMode>('assisted');
  const [customTitle, setCustomTitle] = React.useState('');
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

  /**
   * 带上文件就切到研究型论文。
   *
   * ui-design.md §4.2：研究型论文的起点是一批实验数据，不是一句话
   * （`lib/pipeline.ts` 的 ORIGINAL_FLOW 把 assets 排在第一位）。一个只收文本的
   * 输入框对这条管线是结构性错配，所以这里让文件本身承担分流。
   * 用户手动选过类型就不再自动改——显式选择永远压过推断。
   */
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
    if (!typePinned.current) {
      setPaperType('original');
      setCitationStyle('ieee');
    }
  };

  const pickType = (next: PaperType) => {
    setPaperType(next);
    typePinned.current = true;
    setCitationStyle(next === 'original' ? 'ieee' : 'gbt7714');
  };

  const hasUsableFile = files.some((file) => uploadStates[fileKey(file)]?.status !== 'invalid');
  const canSubmit = !submitting && (
    paperType === 'review' ? text.trim().length > 0 : text.trim().length > 0 || hasUsableFile
  );

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
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    const topic = text.trim();
    try {
      const res = createdProject ? { data: createdProject, source: 'live' as const } : await createProject({
        title: customTitle.trim() || (topic ? deriveTitle(topic) : titleFromFile(files[0])),
        paper_type: paperType,
        writing_mode: writingMode,
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
      if (paperType === 'original') {
        const preflight = await getMaterialPreflight(res.data.id);
        setMaterialPreflight(preflight);
        if (!preflight.ready) {
          try {
            sessionStorage.setItem(`paperforge:material-preflight:${res.data.id}`, JSON.stringify(preflight));
          } catch { /* 存储不可用不阻断导航，素材页会重新请求 */ }
          router.push(`${target}/assets?preflight=1`);
          return;
        }
      }
      if (writingMode === 'auto') {
        // 「全自动（一次跑到 PDF）」是执行承诺，不只是一个项目标签。必须等素材
        // 上传完再启动：原创论文的 generate 预检会读取这些素材来确认方法与结果
        // 可核验。拿到真实 job 后再导航，项目页首次加载即可订阅这条全管线。
        // 档位固定为 draft：一键全流程的承诺是「一次跑到 PDF」，而 scholarly
        // 会在写完之后再跑最多两轮「重写失败章节 + 全文重新评估」，并且没过质量门
        // 就连导出都不做——用户等了更久，最后拿到的是一个 needs_input 而不是稿子。
        // draft 档同样跑完整评估、warnings 一条不少，只是不把发现项升级成阻断项，
        // 修复改由用户在概览页显式发起（quality_repair.available）。
        const started = await generateAll(res.data.id, {
          quality_profile: 'draft',
          review_style: 'narrative',
        });
        if (!started.data) {
          throw new Error(started.note ?? '后端未返回全管线任务');
        }
      }
      router.push(!topic && files.length > 0 ? `${target}/assets` : target);
    } catch (err) {
      setError(describeError(err));
      setSubmitting(false);
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col px-4 pb-16 pt-8 md:pt-[12vh]">
      <h1 className="text-center font-serif text-display font-semibold tracking-tight">
        {greeting()}
      </h1>
      <p className="mt-2 text-center font-serif text-heading text-muted-foreground">
        今天想研究什么？
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
          'mt-8 rounded-lg border bg-card shadow-sm transition-colors',
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
          placeholder="描述你的研究主题、问题或论文目标…"
          aria-invalid={Boolean(error)}
          aria-describedby={error ? 'create-project-error' : undefined}
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
                  disabled={uploadStates[fileKey(file)]?.status === 'uploading'}
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
          <div className="flex items-center gap-1">
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
            <Button variant="ghost" size="sm" onClick={() => fileInputRef.current?.click()}>
              <Paperclip className="h-3.5 w-3.5" /> 材料
            </Button>
            <PaperTypeSelect value={paperType} onChange={pickType} />
            <Button variant="ghost" size="sm" onClick={() => setShowSettings((v) => !v)}>
              <Settings2 className="h-3.5 w-3.5" />
              更多设置
              <ChevronDown
                className={cn('h-3 w-3 transition-transform', showSettings && 'rotate-180')}
              />
            </Button>
          </div>
          <Button
            onClick={() => void submit()}
            disabled={!canSubmit}
            loading={submitting}
            loadingLabel={writingMode === 'auto' ? '正在启动全流程…' : '正在创建…'}
          >
            <ArrowRight className="h-4 w-4" />
            {createdProject
              ? '继续处理文件'
              : writingMode === 'auto'
                ? '创建并启动全流程'
                : paperType === 'original' && !text.trim()
                  ? '从材料创建研究项目'
                  : '创建项目'}
          </Button>
        </div>

        {showSettings && (
          <div className="grid gap-3 border-t px-4 py-3 sm:grid-cols-2">
            <Field label="自定义题目（可选）" id="custom-title" className="sm:col-span-2">
              <Input
                id="custom-title"
                value={customTitle}
                onChange={(event) => setCustomTitle(event.target.value)}
                placeholder={text.trim() ? deriveTitle(text) : '留空则从研究描述自动生成'}
              />
            </Field>
            <Field label="语言" id="lang">
              <Select
                id="lang"
                value={language}
                onChange={(e) => setLanguage(e.target.value as Language)}
              >
                {(['zh', 'en'] as Language[]).map((l) => (
                  <option key={l} value={l}>
                    {LANGUAGE_LABEL[l]}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="引用样式" id="cite">
              <Select
                id="cite"
                value={citationStyle}
                onChange={(e) => setCitationStyle(e.target.value as CitationStyle)}
              >
                {(Object.keys(CITATION_STYLE_LABEL) as CitationStyle[]).map((c) => (
                  <option key={c} value={c}>
                    {CITATION_STYLE_LABEL[c]}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="投稿模板" id="tpl">
              <Select
                id="tpl"
                value={venueTemplate}
                onChange={(e) => setVenueTemplate(e.target.value)}
              >
                {VENUE_TEMPLATES.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.label}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="写作模式" id="mode">
              <Select
                id="mode"
                value={writingMode}
                onChange={(e) => setWritingMode(e.target.value as WritingMode)}
              >
                <option value="assisted">协作（逐步确认）</option>
                <option value="auto">全自动（一次跑到 PDF）</option>
              </Select>
            </Field>
            {paperType === 'original' && (
              <ContributionEditor points={contributionPoints} onChange={setContributionPoints} />
            )}
          </div>
        )}
      </div>

      {recent?.length === 0 && (
        <div className="mt-3 space-y-2 text-center text-meta text-muted-foreground">
          <p>描述研究主题，或为研究型论文直接上传材料。</p>
          <div className="flex flex-wrap justify-center gap-2" aria-label="研究意图示例">
            {[
              '比较近五年大语言模型事实一致性评估方法',
              '系统综述可解释医学影像中的证据与局限',
              '根据上传的实验结果撰写研究型论文',
            ].map((example) => (
              <button
                key={example}
                type="button"
                onClick={() => setText(example)}
                className="rounded-full border px-3 py-1 hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {example}
              </button>
            ))}
          </div>
        </div>
      )}

      {capabilities && (
        <p className="mt-2 text-center text-meta text-muted-foreground">
          单文件上限 {capabilities.max_mib} MiB；推荐 {capabilities.preferred_extensions.join('、')}。
          其它类型会保留并作为方法备注尝试解析。
        </p>
      )}

      {materialPreflight && !materialPreflight.ready && (
        <Callout variant="warning" className="mt-3">
          <span className="flex items-center gap-2 font-medium"><AlertCircle className="h-4 w-4" />项目已保存，还需补充材料</span>
          <ul className="mt-1 list-disc pl-5">
            {materialPreflight.issues.map((issue) => <li key={issue.code}>{issue.message}</li>)}
          </ul>
        </Callout>
      )}

      {error && (
        <Callout id="create-project-error" role="alert" variant="error" className="mt-3">
          {error}
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

/** 类型选择：输入框内的一个轻下拉，而不是首页上两张对等的大卡片。 */
function PaperTypeSelect({
  value,
  onChange,
}: {
  value: PaperType;
  onChange: (next: PaperType) => void;
}) {
  return (
    <>
      <label className="sr-only" htmlFor="paper-type">论文类型</label>
      <select
        id="paper-type"
        aria-label="论文类型"
        value={value}
        onChange={(event) => onChange(event.target.value as PaperType)}
        className="h-9 rounded-md border-0 bg-transparent px-2 text-sm font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <option value="review">综述论文</option>
        <option value="original">研究型论文</option>
      </select>
    </>
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
            两条管线共用同一个入口，类型、素材和贡献点都可以在上方一次说明。
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
              className="flex items-baseline justify-between gap-4 py-2.5 transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <span className="min-w-0 flex-1 truncate font-serif text-subheading">{project.title}</span>
              <span className="shrink-0 text-meta text-muted-foreground">
                {formatDate(project.updated_at)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
