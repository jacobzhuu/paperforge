'use client';

import * as React from 'react';
import {
  AlertTriangle,
  Check,
  ChevronDown,
  Download,
  History,
  Loader2,
  RefreshCw,
  Settings2,
  X,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { visualRenditionUrl } from '@/lib/api';
import type { PaperSection, SectionIR, VisualAsset } from '@/lib/types';
import { insertPositions } from '@/lib/section-positions';
import { cn } from '@/lib/utils';
import type { VisualGroup } from './use-visuals';
import type { TrackedJob } from '@/lib/useJobTracker';

const KIND_LABEL: Record<VisualAsset['kind'], string> = {
  chart: '数据图表',
  diagram: '学术示意图',
  ai_image: 'AI 概念插图',
};

export interface VisualCardActions {
  onEdit: (visual: VisualAsset) => void;
  onGenerate: (visual: VisualAsset) => void;
  onRevision: (visual: VisualAsset) => void;
  onApprove: (visual: VisualAsset, sectionKey: string, blockIndex: number) => void;
  onReject: (visual: VisualAsset) => void;
}

/**
 * 视觉卡片。**同一张卡片提供全部动作**——调整、生成/重试、创建新版本、版本比较、
 * 批准并插入、拒绝、下载、查看来源。
 *
 * 此前动作被拆在两个页面：素材中心能改规格却不能批准，写作台能批准却不能调整。
 * 一次「改描述 → 重生成 → 批准插入」要在两个页面之间来回跳三次。
 */
export function VisualCard({
  group,
  projectId,
  sections,
  activeSectionKey,
  job,
  aiGenerationAvailable,
  aiJobRunning,
  deterministicSlotsFull = false,
  actions,
  compact = false,
}: {
  group: VisualGroup;
  projectId: string;
  sections: PaperSection[];
  activeSectionKey?: string | null;
  job: TrackedJob | null;
  aiGenerationAvailable: boolean;
  aiJobRunning: boolean;
  /** 确定性渲染已达并发上限（2 个）。 */
  deterministicSlotsFull?: boolean;
  actions: VisualCardActions;
  /** 写作台里的紧凑排版：不展开版本历史与来源。 */
  compact?: boolean;
}) {
  const [showVersions, setShowVersions] = React.useState(false);
  // 默认展示最新版；已批准版本存在时用它，让「正文里现在是哪张」一目了然。
  const [previewId, setPreviewId] = React.useState<string | null>(null);
  const visual =
    group.versions.find((item) => item.id === previewId) ?? group.approved ?? group.latest;

  const preview = visual.renditions.png?.url || visual.renditions.svg?.url;
  const generating =
    job !== null ||
    visual.generation_status === 'queued' ||
    visual.generation_status === 'running';
  const aiBlocked = visual.kind === 'ai_image' && !aiGenerationAvailable;
  const queueFull =
    visual.kind === 'ai_image' ? aiJobRunning : deterministicSlotsFull;

  const defaultSection =
    visual.target_section_key || activeSectionKey || sections[0]?.section_key || '';
  const [sectionKey, setSectionKey] = React.useState(defaultSection);
  React.useEffect(() => setSectionKey(defaultSection), [defaultSection]);

  const selectedSection = sections.find((section) => section.section_key === sectionKey);
  const positions = React.useMemo(
    () => insertPositions(selectedSection?.body_ir as SectionIR | undefined),
    [selectedSection],
  );
  const maxBlockIndex = positions[positions.length - 1]?.index ?? 0;
  const [blockIndex, setBlockIndex] = React.useState(maxBlockIndex);
  React.useEffect(() => {
    // 建议里的下标未必落在可选位置上（正文改过），取最接近的一个。
    const wanted = Math.min(visual.suggested_block_index ?? maxBlockIndex, maxBlockIndex);
    const nearest = positions.reduce(
      (best, item) =>
        Math.abs(item.index - wanted) < Math.abs(best - wanted) ? item.index : best,
      maxBlockIndex,
    );
    setBlockIndex(nearest);
  }, [visual.suggested_block_index, maxBlockIndex, positions]);

  return (
    <article className="overflow-hidden rounded-lg border bg-background" data-testid="visual-card">
      {preview ? (
        // eslint-disable-next-line @next/next/no-img-element -- authenticated API rendition URL
        <img
          src={preview}
          alt={visual.alt_text || visual.caption}
          className={cn('w-full bg-white object-contain', compact ? 'h-40' : 'h-52')}
        />
      ) : (
        <div className="flex h-28 items-center justify-center bg-muted/50 text-xs text-muted-foreground">
          {generating ? (
            <span className="flex items-center gap-2">
              <Loader2 className="h-4 w-4 animate-spin" />
              {job?.label ?? '生成中'}
            </span>
          ) : (
            '尚未生成预览'
          )}
        </div>
      )}

      <div className="space-y-2 p-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">
              {visual.title || visual.caption || '未命名视觉'}
            </p>
            <p className="text-xs text-muted-foreground">
              {KIND_LABEL[visual.kind]} · v{visual.version}
              {visual.output_width && visual.output_height && (
                // 实际尺寸而不是「你当初选了什么」：Cloudflare 根本不接受尺寸参数。
                <> · 实际 {visual.output_width}×{visual.output_height}</>
              )}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {visual.kind === 'ai_image' && <Badge variant="warning">外部 AI</Badge>}
            <StatusBadge visual={visual} />
          </div>
        </div>

        {visual.stale && (
          <p className="flex items-start gap-1.5 rounded border border-warning/40 bg-warning/10 px-2 py-1 text-xs text-warning-foreground">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>建议基于旧版正文。可以继续使用，也可以重新分析全文。</span>
          </p>
        )}
        {visual.suggestion_reason && !compact && (
          <p className="text-xs text-muted-foreground">建议依据：{visual.suggestion_reason}</p>
        )}
        {visual.caption_hint && (
          <p className="text-xs text-warning-foreground">{visual.caption_hint}</p>
        )}

        <VisualErrorNotice visual={visual} />

        {visual.generation_status === 'ready' && visual.review_status === 'pending' && (
          <div className="grid gap-2 sm:grid-cols-2">
            <select
              value={sectionKey}
              onChange={(event) => {
                const next = event.target.value;
                setSectionKey(next);
                const section = sections.find((item) => item.section_key === next);
                setBlockIndex(((section?.body_ir as SectionIR | undefined)?.blocks ?? []).length);
              }}
              className="h-9 w-full rounded-md border bg-background px-2 text-sm"
              aria-label="插入章节"
            >
              {sections.length === 0 && <option value="">（尚无章节）</option>}
              {sections.map((section) => (
                <option key={section.section_key} value={section.section_key}>
                  {section.title}
                </option>
              ))}
            </select>
            {/*
              位置是可读的锚点而不是 block 下标：用户看到的是段落和图，
              不是 IR 数组——让他去猜「位置 6」是哪儿毫无道理。
            */}
            <select
              value={blockIndex}
              onChange={(event) => setBlockIndex(Number(event.target.value))}
              className="h-9 w-full rounded-md border bg-background px-2 text-sm"
              aria-label="插入位置"
            >
              {positions.map((position) => (
                <option key={position.index} value={position.index}>
                  {position.label}
                </option>
              ))}
            </select>
          </div>
        )}

        <div className="flex flex-wrap gap-1.5">
          <Button variant="outline" size="sm" onClick={() => actions.onEdit(visual)}>
            <Settings2 className="h-3.5 w-3.5" /> 调整
          </Button>

          {visual.generation_status !== 'ready' && visual.review_status === 'pending' && (
            <Button
              size="sm"
              onClick={() => actions.onGenerate(visual)}
              disabled={generating || aiBlocked || queueFull}
              title={
                aiBlocked
                  ? 'AI 图像生成未启用或未配置密钥'
                  : visual.kind === 'ai_image' && aiJobRunning
                    ? '已有一个 AI 生图在跑——同一项目一次只允许一个，避免重复计费'
                    : queueFull
                      ? '已有两个渲染任务在跑，等其中一个完成再开始'
                      : undefined
              }
            >
              {generating && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {visual.generation_status === 'failed' ? '重试' : '生成预览'}
            </Button>
          )}

          {visual.generation_status === 'ready' && visual.review_status === 'pending' && (
            <Button
              size="sm"
              onClick={() => actions.onApprove(visual, sectionKey, blockIndex)}
              disabled={!sectionKey}
            >
              <Check className="h-3.5 w-3.5" /> 批准并插入
            </Button>
          )}

          {visual.generation_status === 'ready' && (
            <Button variant="outline" size="sm" onClick={() => actions.onRevision(visual)}>
              <RefreshCw className="h-3.5 w-3.5" /> 新版本
            </Button>
          )}

          {visual.review_status === 'pending' && (
            <Button variant="ghost" size="sm" onClick={() => actions.onReject(visual)}>
              <X className="h-3.5 w-3.5" /> 拒绝
            </Button>
          )}

          {!compact && preview && <DownloadMenu projectId={projectId} visual={visual} />}

          {!compact && group.versions.length > 1 && (
            <Button variant="ghost" size="sm" onClick={() => setShowVersions((v) => !v)}>
              <History className="h-3.5 w-3.5" />
              {group.versions.length} 个版本
              <ChevronDown
                className={cn('h-3.5 w-3.5 transition-transform', showVersions && 'rotate-180')}
              />
            </Button>
          )}
        </div>

        {showVersions && group.versions.length > 1 && (
          <VersionList
            group={group}
            currentId={visual.id}
            onSelect={(id) => setPreviewId(id)}
          />
        )}
      </div>
    </article>
  );
}

function StatusBadge({ visual }: { visual: VisualAsset }) {
  if (visual.review_status === 'approved') return <Badge variant="success">已插入</Badge>;
  if (visual.review_status === 'rejected') return <Badge variant="muted">已拒绝</Badge>;
  if (visual.generation_status === 'failed') return <Badge variant="destructive">失败</Badge>;
  if (visual.generation_status === 'ready') return <Badge variant="secondary">可批准</Badge>;
  return <Badge variant="muted">待处理</Badge>;
}

/**
 * 失败提示。
 *
 * 后端把厂商错误归到统一词表并给出可执行提示，这里照实呈现：能重试的给「重试」
 * 的语气，不能重试的说清楚要改什么。此前所有 400/422 都被压成同一句
 * 「Cloudflare image request rejected」，用户只能反复试提示词。
 */
function VisualErrorNotice({ visual }: { visual: VisualAsset }) {
  const [open, setOpen] = React.useState(false);
  const error = visual.error;
  if (!error) {
    // 过渡期：老后端只回平铺字段。
    return visual.error_message ? (
      <p className="text-xs text-destructive-strong">{visual.error_message}</p>
    ) : null;
  }
  return (
    <div className="space-y-1 rounded border border-destructive/40 bg-destructive/10 px-2 py-1.5 text-xs">
      <p className="flex items-start gap-1.5 text-destructive-strong">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <span>{error.message}</span>
      </p>
      <p className="text-muted-foreground">
        {error.retryable ? '这类失败重试通常有效。' : '重试不会改变结果，需要先按上面的提示调整。'}
        {error.request_id && <> 追踪 ID：<code className="font-mono">{error.request_id}</code></>}
      </p>
      {error.detail && (
        <>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="text-muted-foreground underline"
          >
            {open ? '收起技术细节' : '展开技术细节'}
          </button>
          {open && (
            <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded bg-muted/60 p-1.5 font-mono text-[11px]">
              {error.detail}
            </pre>
          )}
        </>
      )}
    </div>
  );
}

function DownloadMenu({ projectId, visual }: { projectId: string; visual: VisualAsset }) {
  const formats = (['png', 'svg', 'pdf'] as const).filter((format) => visual.renditions[format]);
  if (formats.length === 0) return null;
  return (
    <span className="inline-flex items-center gap-0.5">
      <Download className="h-3.5 w-3.5 text-muted-foreground" />
      {formats.map((format) => (
        <a
          key={format}
          href={`${visualRenditionUrl(projectId, visual.id, format)}?disposition=attachment`}
          download
          className="rounded px-1.5 py-0.5 text-xs uppercase text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          {format}
        </a>
      ))}
    </span>
  );
}

/** 版本组：默认折叠，展开后可切换预览做前后比较。 */
function VersionList({
  group,
  currentId,
  onSelect,
}: {
  group: VisualGroup;
  currentId: string;
  onSelect: (id: string) => void;
}) {
  return (
    <ul className="space-y-1 rounded border bg-muted/30 p-1.5">
      {group.versions.map((item) => (
        <li key={item.id}>
          <button
            type="button"
            onClick={() => onSelect(item.id)}
            aria-current={item.id === currentId}
            className={cn(
              'flex w-full items-center justify-between gap-2 rounded px-2 py-1 text-left text-xs transition-colors hover:bg-accent',
              item.id === currentId && 'bg-accent font-medium',
            )}
          >
            <span>v{item.version}</span>
            <span className="text-muted-foreground">
              {item.review_status === 'approved'
                ? '正文中'
                : item.generation_status === 'ready'
                  ? '可批准'
                  : item.generation_status === 'failed'
                    ? '失败'
                    : '待生成'}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}
