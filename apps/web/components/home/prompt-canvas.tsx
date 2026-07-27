'use client';

import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ArrowRight, ChevronDown, FileText, Loader2, Paperclip, Settings2, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { createProject, listProjects, uploadAsset } from '@/lib/api';
import { describeError } from '@/lib/errors';
import { CITATION_STYLE_LABEL, LANGUAGE_LABEL, VENUE_TEMPLATES } from '@/lib/labels';
import type { CitationStyle, Language, PaperType, Project, WritingMode } from '@/lib/types';
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
  const [dragging, setDragging] = React.useState(false);
  const [showSettings, setShowSettings] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const [language, setLanguage] = React.useState<Language>('zh');
  const [citationStyle, setCitationStyle] = React.useState<CitationStyle>('gbt7714');
  const [venueTemplate, setVenueTemplate] = React.useState('article');
  const [writingMode, setWritingMode] = React.useState<WritingMode>('assisted');

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
    setFiles((prev) => [...prev, ...incoming]);
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

  const canSubmit = text.trim().length > 0 && !submitting;

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    const topic = text.trim();
    try {
      const res = await createProject({
        title: deriveTitle(topic),
        paper_type: paperType,
        writing_mode: writingMode,
        language,
        topic,
        venue_template: venueTemplate,
        citation_style: citationStyle,
      });
      if (res.source === 'mock') {
        // createProject 降级时返回的是本地乐观桩（id: local-…）。导航过去会让用户
        // 进入一个并不存在的项目，还会看到示例文献——宁可停在这里说清楚。
        setError(
          `项目未创建：${res.note ?? '后端不可用'}。请确认 ./scripts/dev up 已启动后重试。`,
        );
        setSubmitting(false);
        return;
      }
      // 文件必须等项目建好才能传（uploadAsset 需要 projectId）。传失败不阻断——
      // 项目已经存在了，把人送进去、在素材中心重传，比回滚掉整个项目好。
      // 注意 uploadAsset 降级时**不抛异常**，而是返回 data===undefined，
      // 所以这里靠返回值判断成败，不能只包一个 try/catch。
      const failed: string[] = [];
      for (const file of files) {
        try {
          const uploaded = await uploadAsset(res.data.id, file);
          if (!uploaded.data) failed.push(file.name);
        } catch {
          failed.push(file.name);
        }
      }
      const target = `/projects/${res.data.id}`;
      router.push(failed.length > 0 ? `${target}/assets` : target);
    } catch (err) {
      setError(describeError(err));
      setSubmitting(false);
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col px-4 pb-16 pt-[12vh]">
      <h1 className="text-center font-serif text-3xl font-semibold tracking-tight">
        {greeting()}
      </h1>
      <p className="mt-2 text-center font-serif text-xl text-muted-foreground">
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
          'mt-8 rounded-xl border bg-card shadow-sm transition-colors',
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
          className="w-full resize-none bg-transparent px-4 pt-4 text-base leading-relaxed placeholder:text-muted-foreground focus-visible:outline-none"
        />

        {files.length > 0 && (
          <ul className="flex flex-wrap gap-1.5 px-4 pb-1">
            {files.map((file, i) => (
              <li
                key={`${file.name}-${i}`}
                className="flex items-center gap-1.5 rounded-md border bg-muted/40 px-2 py-1 text-xs"
              >
                <FileText className="h-3 w-3 shrink-0 text-muted-foreground" />
                <span className="max-w-48 truncate">{file.name}</span>
                <button
                  type="button"
                  onClick={() => setFiles((prev) => prev.filter((_, j) => j !== i))}
                  aria-label={`移除 ${file.name}`}
                  className="rounded text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
          <Button onClick={() => void submit()} disabled={!canSubmit}>
            {submitting ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <ArrowRight className="h-4 w-4" />
            )}
            开始
          </Button>
        </div>

        {showSettings && (
          <div className="grid gap-3 border-t px-4 py-3 sm:grid-cols-2">
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
          </div>
        )}
      </div>

      {error && (
        <p className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive-strong">
          {error}
        </p>
      )}

      <RecentPapers projects={recent} />
    </div>
  );
}

function Field({
  label,
  id,
  children,
}: {
  label: string;
  id: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id} className="text-xs text-muted-foreground">
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
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const OPTIONS: { id: PaperType; label: string; hint: string }[] = [
    { id: 'review', label: '综述论文', hint: '从题目出发，检索并综合文献' },
    { id: 'original', label: '研究型论文', hint: '从你的素材与结果出发' },
  ];
  const current = OPTIONS.find((o) => o.id === value) ?? OPTIONS[0];

  return (
    <div ref={ref} className="relative">
      <Button variant="ghost" size="sm" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        {current.label}
        <ChevronDown className={cn('h-3 w-3 transition-transform', open && 'rotate-180')} />
      </Button>
      {open && (
        <ul
          role="listbox"
          className="absolute bottom-full z-20 mb-1 w-64 rounded-lg border bg-popover p-1 shadow-md"
        >
          {OPTIONS.map((option) => (
            <li key={option.id}>
              <button
                type="button"
                role="option"
                aria-selected={option.id === value}
                onClick={() => {
                  onChange(option.id);
                  setOpen(false);
                }}
                className={cn(
                  'w-full rounded-md px-2.5 py-2 text-left transition-colors hover:bg-accent',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  option.id === value && 'bg-accent/60',
                )}
              >
                <span className="block text-sm font-medium">{option.label}</span>
                <span className="block text-xs text-muted-foreground">{option.hint}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
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
  if (projects.length === 0) return null;

  return (
    <section className="mt-12 space-y-1">
      <div className="flex items-baseline justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          最近的论文
        </h2>
        <Link
          href="/projects"
          className="rounded text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
              <span className="min-w-0 flex-1 truncate font-serif text-lg">{project.title}</span>
              <span className="shrink-0 text-xs text-muted-foreground">
                {formatDate(project.updated_at)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
