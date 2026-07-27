'use client';

import * as React from 'react';
import { useRouter } from 'next/navigation';
import { BookOpen, FlaskConical, Check, Plus, X, Loader2 } from 'lucide-react';
import { Dialog } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { cn } from '@/lib/utils';
import { createProject } from '@/lib/api';
import type {
  CitationStyle,
  CreateProjectRequest,
  Language,
  PaperType,
  WritingMode,
} from '@/lib/types';
import { CITATION_STYLE_LABEL, LANGUAGE_LABEL, VENUE_TEMPLATES } from '@/lib/labels';
import { describeError } from '@/lib/errors';

const STEPS = ['类型', '主题 / 贡献点', '模板 / 语言', '模式'];

interface FormState {
  paper_type: PaperType;
  title: string;
  topic: string;
  contribution_points: string[];
  venue_template: string;
  language: Language;
  citation_style: CitationStyle;
  writing_mode: WritingMode;
}

const INITIAL: FormState = {
  paper_type: 'review',
  title: '',
  topic: '',
  contribution_points: [''],
  venue_template: 'article',
  language: 'zh',
  citation_style: 'gbt7714',
  writing_mode: 'assisted',
};

export function NewProjectWizard({
  open,
  onClose,
  initialPaperType,
}: {
  open: boolean;
  onClose: () => void;
  /** 从首次运行的两张引导卡进入时，类型已经选好，直接跳到第 2 步。 */
  initialPaperType?: PaperType;
}) {
  const router = useRouter();
  const [step, setStep] = React.useState(0);
  const [form, setForm] = React.useState<FormState>(INITIAL);
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const patch = (p: Partial<FormState>) => setForm((f) => ({ ...f, ...p }));

  // 打开时套用预选类型（含该类型的默认引用样式）。
  React.useEffect(() => {
    if (!open) return;
    if (!initialPaperType) return;
    setForm((f) => ({
      ...f,
      paper_type: initialPaperType,
      citation_style: initialPaperType === 'original' ? 'ieee' : 'gbt7714',
    }));
    setStep(1);
  }, [open, initialPaperType]);

  const reset = () => {
    setStep(0);
    setForm(INITIAL);
    setError(null);
    setSubmitting(false);
  };

  const close = () => {
    reset();
    onClose();
  };

  const canNext = () => {
    if (step === 1) return form.title.trim().length > 0;
    return true;
  };

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    const body: CreateProjectRequest = {
      title: form.title.trim(),
      paper_type: form.paper_type,
      writing_mode: form.writing_mode,
      language: form.language,
      topic: form.topic.trim() || undefined,
      venue_template: form.venue_template,
      citation_style: form.citation_style,
      contribution_points:
        form.paper_type === 'original'
          ? form.contribution_points.map((c) => c.trim()).filter(Boolean)
          : undefined,
    };
    try {
      const res = await createProject(body);
      if (res.source === 'mock') {
        // 降级时 createProject 返回的是本地乐观桩（id: local-…）。此前会直接导航过去，
        // 用户进入一个并不存在的项目，还会看到 MOCK_LIBRARY 里 42 篇虚构文献。
        setError(
          `项目未创建：${res.note ?? '后端不可用'}。请确认 ./scripts/dev up 已启动后重试——` +
            '这里不会为你保留一个假的项目。',
        );
        setSubmitting(false);
        return;
      }
      close();
      // 落到项目概览而不是文献工作台：研究型论文的第一步是上传素材而非检索，
      // 概览的「下一步」卡会按 paper_type 给出正确的入口。
      router.push(`/projects/${res.data.id}`);
    } catch (e) {
      setError(describeError(e));
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={close}
      className="max-w-2xl"
      title="新建论文项目"
      description={`第 ${step + 1} / ${STEPS.length} 步 · ${STEPS[step]}`}
      footer={
        <>
          {step > 0 && (
            <Button variant="ghost" onClick={() => setStep((s) => s - 1)} disabled={submitting}>
              上一步
            </Button>
          )}
          {step < STEPS.length - 1 ? (
            <Button onClick={() => setStep((s) => s + 1)} disabled={!canNext()}>
              下一步
            </Button>
          ) : (
            <Button onClick={submit} disabled={submitting}>
              {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
              创建项目
            </Button>
          )}
        </>
      }
    >
      <Stepper step={step} />

      {error && (
        <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive-strong">
          {error}
        </div>
      )}

      {step === 0 && (
        <div className="grid gap-3 sm:grid-cols-2">
          <TypeCard
            active={form.paper_type === 'review'}
            icon={<BookOpen className="h-5 w-5" />}
            title="综述论文"
            desc="题目 → 检索 → 文献库 → 大纲 → 分节写作 → 引用校验 → LaTeX/PDF"
            onClick={() => patch({ paper_type: 'review', citation_style: 'gbt7714' })}
          />
          <TypeCard
            active={form.paper_type === 'original'}
            icon={<FlaskConical className="h-5 w-5" />}
            title="研究型论文"
            desc="素材摄取 → 相关工作检索 → IMRaD 写作 → 数字一致性 lint → LaTeX/PDF"
            onClick={() => patch({ paper_type: 'original', citation_style: 'ieee' })}
          />
        </div>
      )}

      {step === 1 && (
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="title">论文题目 *</Label>
            <Input
              id="title"
              value={form.title}
              onChange={(e) => patch({ title: e.target.value })}
              placeholder={
                form.paper_type === 'review'
                  ? '例如：扩散模型在医学影像分割中的研究综述'
                  : '例如：A Lightweight Retrieval-Augmented Transformer'
              }
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="topic">主题 / 关键词</Label>
            <Input
              id="topic"
              value={form.topic}
              onChange={(e) => patch({ topic: e.target.value })}
              placeholder="用于生成研究范围与检索矩阵，例如：扩散模型 · 医学影像 · 语义分割"
            />
          </div>
          {form.paper_type === 'original' && (
            <ContributionEditor
              points={form.contribution_points}
              onChange={(contribution_points) => patch({ contribution_points })}
            />
          )}
        </div>
      )}

      {step === 2 && (
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="template">投稿模板</Label>
            <Select
              id="template"
              value={form.venue_template}
              onChange={(e) => patch({ venue_template: e.target.value })}
            >
              {VENUE_TEMPLATES.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
                </option>
              ))}
            </Select>
            <p className="text-xs text-muted-foreground">
              模板锁定导言区与宏包，LLM 不可修改（方案 §4.6）。
            </p>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="language">语言</Label>
              <Select
                id="language"
                value={form.language}
                onChange={(e) => patch({ language: e.target.value as Language })}
              >
                {(['zh', 'en'] as Language[]).map((l) => (
                  <option key={l} value={l}>
                    {LANGUAGE_LABEL[l]}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="citation">引用样式</Label>
              <Select
                id="citation"
                value={form.citation_style}
                onChange={(e) => patch({ citation_style: e.target.value as CitationStyle })}
              >
                {(Object.keys(CITATION_STYLE_LABEL) as CitationStyle[]).map((c) => (
                  <option key={c} value={c}>
                    {CITATION_STYLE_LABEL[c]}
                  </option>
                ))}
              </Select>
            </div>
          </div>
        </div>
      )}

      {step === 3 && (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <TypeCard
              active={form.writing_mode === 'assisted'}
              title="协作模式"
              desc="用户圈选入库、逐章确认；每一步可人工干预，推荐用于正式投稿。"
              onClick={() => patch({ writing_mode: 'assisted' })}
            />
            <TypeCard
              active={form.writing_mode === 'auto'}
              title="全自动模式"
              desc="top-K 自动入库；在项目概览点「跑通全管线」一次跑到 PDF，任何阶段失败降级不阻断。"
              onClick={() => patch({ writing_mode: 'auto' })}
            />
          </div>
          <Summary form={form} />
        </div>
      )}
    </Dialog>
  );
}

function Stepper({ step }: { step: number }) {
  return (
    <div className="mb-5 flex items-center gap-2">
      {STEPS.map((label, i) => (
        <React.Fragment key={label}>
          <div className="flex items-center gap-2">
            <div
              className={cn(
                'flex h-6 w-6 items-center justify-center rounded-full text-xs font-medium',
                i < step && 'bg-primary text-primary-foreground',
                i === step && 'bg-primary text-primary-foreground ring-2 ring-ring ring-offset-2',
                i > step && 'bg-muted text-muted-foreground',
              )}
            >
              {i < step ? <Check className="h-3 w-3" /> : i + 1}
            </div>
            <span
              className={cn(
                'hidden text-xs sm:inline',
                i === step ? 'font-medium text-foreground' : 'text-muted-foreground',
              )}
            >
              {label}
            </span>
          </div>
          {i < STEPS.length - 1 && <div className="h-px flex-1 bg-border" />}
        </React.Fragment>
      ))}
    </div>
  );
}

function TypeCard({
  active,
  icon,
  title,
  desc,
  onClick,
}: {
  active: boolean;
  icon?: React.ReactNode;
  title: string;
  desc: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'flex flex-col gap-2 rounded-lg border p-4 text-left transition-colors',
        active ? 'border-primary bg-accent/50 ring-1 ring-primary' : 'hover:border-input hover:bg-accent/30',
      )}
    >
      <div className="flex items-center gap-2">
        {icon}
        <span className="font-medium">{title}</span>
        {active && <Check className="ml-auto h-4 w-4 text-primary" />}
      </div>
      <p className="text-xs leading-relaxed text-muted-foreground">{desc}</p>
    </button>
  );
}

function ContributionEditor({
  points,
  onChange,
}: {
  points: string[];
  onChange: (points: string[]) => void;
}) {
  return (
    <div className="space-y-2">
      <Label>贡献点（研究型论文）</Label>
      {points.map((point, i) => (
        <div key={i} className="flex gap-2">
          <Textarea
            value={point}
            onChange={(e) => onChange(points.map((p, j) => (j === i ? e.target.value : p)))}
            placeholder={`贡献点 ${i + 1}`}
            className="min-h-[44px]"
          />
          {points.length > 1 && (
            <Button
              variant="ghost"
              size="icon"
              onClick={() => onChange(points.filter((_, j) => j !== i))}
              aria-label="删除贡献点"
            >
              <X className="h-4 w-4" />
            </Button>
          )}
        </div>
      ))}
      <Button variant="outline" size="sm" onClick={() => onChange([...points, ''])}>
        <Plus className="h-4 w-4" /> 添加贡献点
      </Button>
    </div>
  );
}

function Summary({ form }: { form: FormState }) {
  const rows: [string, string][] = [
    ['类型', form.paper_type === 'review' ? '综述论文' : '研究型论文'],
    ['题目', form.title || '（未填写）'],
    ['模板', VENUE_TEMPLATES.find((t) => t.id === form.venue_template)?.label ?? form.venue_template],
    ['语言', LANGUAGE_LABEL[form.language]],
    ['引用样式', CITATION_STYLE_LABEL[form.citation_style]],
    ['模式', form.writing_mode === 'assisted' ? '协作' : '全自动'],
  ];
  return (
    <div className="rounded-lg border bg-muted/40 p-4">
      <p className="mb-2 text-xs font-medium text-muted-foreground">确认信息</p>
      <dl className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1.5 text-sm">
        {rows.map(([k, v]) => (
          <React.Fragment key={k}>
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="truncate font-medium">{v}</dd>
          </React.Fragment>
        ))}
      </dl>
    </div>
  );
}
