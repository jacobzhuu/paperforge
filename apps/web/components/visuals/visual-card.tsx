'use client';

import * as React from 'react';
import Link from 'next/link';
import {
  AlertTriangle,
  Check,
  Download,
  Eye,
  FileClock,
  History,
  Loader2,
  RefreshCw,
  Settings2,
  X,
} from 'lucide-react';
import { ActionMenu, type ActionMenuItem } from '@/components/ui/action-menu';
import { Badge } from '@/components/ui/badge';
import { Button, buttonVariants } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { visualRenditionUrl } from '@/lib/api';
import { projectHref } from '@/lib/pipeline';
import { insertPositions } from '@/lib/section-positions';
import type { PaperSection, RegenerateVisualRequest, SectionIR, VisualAsset } from '@/lib/types';
import type { TrackedJob } from '@/lib/useJobTracker';
import { cn } from '@/lib/utils';
import type { VisualGroup } from './use-visuals';

const KIND_LABEL: Record<VisualAsset['kind'], string> = {
  chart: '数据图表',
  diagram: '学术示意图',
  ai_image: 'AI 概念插图',
};

export interface VisualCardActions {
  onEdit: (visual: VisualAsset) => void;
  onGenerate: (visual: VisualAsset) => void;
  onRevision: (
    visual: VisualAsset,
    payload?: RegenerateVisualRequest,
    generateNow?: boolean,
  ) => void;
  onApprove: (visual: VisualAsset, sectionKey: string, blockIndex: number) => void;
  onReject: (visual: VisualAsset) => void;
}

/** One state, one primary action. Placement and implementation details appear only on demand. */
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
  deterministicSlotsFull?: boolean;
  actions: VisualCardActions;
  compact?: boolean;
}) {
  const [previewId, setPreviewId] = React.useState<string | null>(null);
  const [showVersions, setShowVersions] = React.useState(false);
  const [showDetails, setShowDetails] = React.useState(false);
  const [approving, setApproving] = React.useState(false);
  const visual = group.versions.find((item) => item.id === previewId) ?? group.latest;
  const preview = visual.renditions.png?.url || visual.renditions.svg?.url;
  const generating =
    job !== null || visual.generation_status === 'queued' || visual.generation_status === 'running';
  const aiBlocked = visual.kind === 'ai_image' && !aiGenerationAvailable;
  const queueFull = visual.kind === 'ai_image' ? aiJobRunning : deterministicSlotsFull;

  const defaultSection =
    visual.target_section_key || activeSectionKey || sections[0]?.section_key || '';
  const [sectionKey, setSectionKey] = React.useState(defaultSection);
  React.useEffect(() => setSectionKey(defaultSection), [defaultSection]);
  const selectedSection = sections.find((section) => section.section_key === sectionKey);
  const positions = React.useMemo(
    () => insertPositions(selectedSection?.body_ir as SectionIR | undefined),
    [selectedSection],
  );
  const lastPosition = positions[positions.length - 1]?.index ?? 0;
  const [blockIndex, setBlockIndex] = React.useState(lastPosition);
  React.useEffect(() => {
    const wanted = Math.min(visual.suggested_block_index ?? lastPosition, lastPosition);
    const nearest = positions.reduce(
      (best, item) =>
        Math.abs(item.index - wanted) < Math.abs(best - wanted) ? item.index : best,
      lastPosition,
    );
    setBlockIndex(nearest);
  }, [lastPosition, positions, visual.suggested_block_index]);

  const downloadItems = (['png', 'svg', 'pdf'] as const)
    .filter((format) => visual.renditions[format])
    .map<ActionMenuItem>((format) => ({
      label: `下载 ${format.toUpperCase()}`,
      icon: Download,
      onSelect: () => downloadVisual(projectId, visual, format),
    }));
  const menuItems: ActionMenuItem[] = [
    ...(visual.review_status !== 'rejected'
      ? [{
          label: visual.review_status === 'approved' ? '调整并创建新版本' : '调整视觉',
          description: '用自然语言修订；旧版本保持不变。',
          icon: Settings2,
          onSelect: () => actions.onEdit(visual),
        }]
      : []),
    ...(visual.review_status === 'pending'
      ? [{
          label: '拒绝这条建议',
          icon: X,
          onSelect: () => actions.onReject(visual),
        }]
      : []),
    ...(group.versions.length > 1
      ? [{
          label: `查看 ${group.versions.length} 个版本`,
          icon: History,
          onSelect: () => setShowVersions((value) => !value),
        }]
      : []),
    ...downloadItems,
    {
      label: '来源与技术细节',
      icon: FileClock,
      onSelect: () => setShowDetails((value) => !value),
    },
  ];

  return (
    <article className="rounded-xl border bg-background shadow-sm" data-testid="visual-card">
      {preview ? (
        // eslint-disable-next-line @next/next/no-img-element -- authenticated rendition endpoint
        <img
          src={preview}
          alt={visual.alt_text || visual.caption}
          className={cn('w-full rounded-t-xl bg-white object-contain', compact ? 'h-40' : 'h-56')}
        />
      ) : (
        <div className="flex h-36 items-center justify-center rounded-t-xl bg-muted/45 text-sm text-muted-foreground">
          {generating ? (
            <span className="flex items-center gap-2">
              <Loader2 className="h-4 w-4 animate-spin" /> {job?.label ?? '正在生成预览'}
            </span>
          ) : '等待生成预览'}
        </div>
      )}

      <div className="space-y-3 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 className="truncate text-sm font-semibold">{visual.title || visual.caption || '未命名视觉'}</h3>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {KIND_LABEL[visual.kind]} · v{visual.version}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {visual.kind === 'ai_image' && <Badge variant="warning">外部 AI</Badge>}
            <StatusBadge visual={visual} generating={generating} />
          </div>
        </div>

        <VisualErrorNotice visual={visual} />

        <div className="flex items-center justify-between gap-2">
          <PrimaryAction
            visual={visual}
            projectId={projectId}
            generating={generating}
            aiBlocked={aiBlocked}
            queueFull={queueFull}
            hasSection={sections.length > 0}
            onGenerate={() => actions.onGenerate(visual)}
            onApprove={() => setApproving(true)}
            onEdit={() => actions.onEdit(visual)}
            onRestore={() => actions.onRevision(visual, {}, false)}
          />
          {menuItems.length > 0 && <ActionMenu items={menuItems} />}
        </div>

        {showVersions && group.versions.length > 1 && (
          <VersionList group={group} currentId={visual.id} onSelect={setPreviewId} />
        )}
        {showDetails && (
          <dl className="grid grid-cols-[7rem,minmax(0,1fr)] gap-x-2 gap-y-1 rounded-lg bg-muted/45 p-3 text-xs">
            <dt className="text-muted-foreground">选择依据</dt>
            <dd>{visual.suggestion_reason || '手动创建'}</dd>
            <dt className="text-muted-foreground">正文来源</dt>
            <dd>{visual.source_section_keys?.join('、') || '当前意图'}</dd>
            <dt className="text-muted-foreground">目标章节</dt>
            <dd>{visual.target_section_key || '插入时选择'}</dd>
            <dt className="text-muted-foreground">实际尺寸</dt>
            <dd>{visual.output_width && visual.output_height ? `${visual.output_width} × ${visual.output_height}` : '预览生成后确定'}</dd>
          </dl>
        )}
      </div>

      <Dialog
        open={approving}
        onClose={() => setApproving(false)}
        title="插入论文"
        description="系统已给出建议位置；你可以在确认前调整。"
        footer={
          <>
            <Button variant="outline" onClick={() => setApproving(false)}>取消</Button>
            <Button
              onClick={() => {
                actions.onApprove(visual, sectionKey, blockIndex);
                setApproving(false);
              }}
              disabled={!sectionKey}
            ><Check /> 插入论文</Button>
          </>
        }
      >
        <div className="space-y-4">
          <label className="space-y-2 text-sm">
            <span className="font-medium">章节</span>
            <select
              value={sectionKey}
              onChange={(event) => {
                const next = event.target.value;
                setSectionKey(next);
                const section = sections.find((item) => item.section_key === next);
                setBlockIndex(((section?.body_ir as SectionIR | undefined)?.blocks ?? []).length);
              }}
              className="h-11 w-full rounded-md border bg-background px-3"
              aria-label="插入章节"
            >
              {sections.length === 0 && <option value="">尚无可插入章节</option>}
              {sections.map((section) => <option key={section.section_key} value={section.section_key}>{section.title}</option>)}
            </select>
          </label>
          <label className="space-y-2 text-sm">
            <span className="font-medium">位置</span>
            <select value={blockIndex} onChange={(event) => setBlockIndex(Number(event.target.value))} className="h-11 w-full rounded-md border bg-background px-3" aria-label="插入位置">
              {positions.map((position) => <option key={position.index} value={position.index}>{position.label}</option>)}
            </select>
          </label>
        </div>
      </Dialog>
    </article>
  );
}

function PrimaryAction({
  visual,
  projectId,
  generating,
  aiBlocked,
  queueFull,
  hasSection,
  onGenerate,
  onApprove,
  onEdit,
  onRestore,
}: {
  visual: VisualAsset;
  projectId: string;
  generating: boolean;
  aiBlocked: boolean;
  queueFull: boolean;
  hasSection: boolean;
  onGenerate: () => void;
  onApprove: () => void;
  onEdit: () => void;
  onRestore: () => void;
}) {
  if (visual.review_status === 'approved') {
    return (
      <Link href={projectHref(projectId, 'write')} className={buttonVariants({ size: 'default' })}>
        <Eye /> 查看正文位置
      </Link>
    );
  }
  if (visual.review_status === 'rejected') {
    return <Button onClick={onRestore}><RefreshCw /> 恢复为新草稿</Button>;
  }
  if (generating) {
    return <Button disabled><Loader2 className="animate-spin" /> 正在生成预览</Button>;
  }
  if (visual.generation_status === 'ready') {
    return <Button onClick={onApprove} disabled={!hasSection}><Check /> 插入论文</Button>;
  }
  if (visual.generation_status === 'failed') {
    const directRetry = visual.error?.retryable ?? true;
    return (
      <Button onClick={directRetry ? onGenerate : onEdit} disabled={aiBlocked || queueFull}>
        <RefreshCw /> {directRetry ? '重试生成' : '修复并重试'}
      </Button>
    );
  }
  return (
    <Button onClick={onGenerate} disabled={aiBlocked || queueFull} title={aiBlocked ? 'AI 图像生成未启用或未配置' : undefined}>
      生成预览
    </Button>
  );
}

function StatusBadge({ visual, generating }: { visual: VisualAsset; generating: boolean }) {
  if (visual.review_status === 'approved') return <Badge variant="success">已插入</Badge>;
  if (visual.review_status === 'rejected') return <Badge variant="muted">已拒绝</Badge>;
  if (generating) return <Badge variant="secondary">生成中</Badge>;
  if (visual.generation_status === 'failed') return <Badge variant="destructive">失败</Badge>;
  if (visual.generation_status === 'ready') return <Badge variant="secondary">可批准</Badge>;
  return <Badge variant="muted">待生成</Badge>;
}

function VisualErrorNotice({ visual }: { visual: VisualAsset }) {
  const [open, setOpen] = React.useState(false);
  const error = visual.error;
  if (!error) return visual.error_message ? <p className="text-xs text-destructive-strong">{visual.error_message}</p> : null;
  return (
    <div className="space-y-1 rounded-lg border border-destructive/35 bg-destructive/10 px-3 py-2 text-xs">
      <p className="flex items-start gap-1.5 text-destructive-strong">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> <span>{error.message}</span>
      </p>
      <p className="text-muted-foreground">
        {error.retryable ? '这类失败重试通常有效。' : '需要先调整描述，再生成新版本。'}
        {error.request_id && <> 追踪 ID：<code className="font-mono">{error.request_id}</code></>}
      </p>
      {error.detail && (
        <>
          <button type="button" onClick={() => setOpen((value) => !value)} className="underline underline-offset-2">
            {open ? '收起技术细节' : '展开技术细节'}
          </button>
          {open && <pre className="whitespace-pre-wrap break-all rounded bg-muted p-2 font-mono text-[11px]">{error.detail}</pre>}
        </>
      )}
    </div>
  );
}

function VersionList({ group, currentId, onSelect }: { group: VisualGroup; currentId: string; onSelect: (id: string) => void }) {
  return (
    <ul className="space-y-1 rounded-lg border bg-muted/30 p-2">
      {group.versions.map((item) => (
        <li key={item.id}>
          <button type="button" onClick={() => onSelect(item.id)} aria-current={item.id === currentId} className={cn('flex min-h-10 w-full items-center justify-between rounded px-2 text-left text-xs hover:bg-accent', item.id === currentId && 'bg-accent font-medium')}>
            <span>v{item.version}</span>
            <span className="text-muted-foreground">{item.review_status === 'approved' ? '正文中' : item.generation_status === 'ready' ? '可批准' : item.generation_status === 'failed' ? '失败' : '草稿'}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function downloadVisual(projectId: string, visual: VisualAsset, format: 'png' | 'svg' | 'pdf') {
  const anchor = document.createElement('a');
  anchor.href = `${visualRenditionUrl(projectId, visual.id, format)}?disposition=attachment`;
  anchor.download = `${visual.title || 'visual'}.${format}`;
  anchor.click();
}
