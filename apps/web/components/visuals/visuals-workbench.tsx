'use client';

import * as React from 'react';
import { Image as ImageIcon, Info, Loader2, Plus, RefreshCw } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useToast } from '@/components/ui/toast';
import { LoadState } from '@/components/layout/load-state';
import { ModuleError } from '@/components/layout/module-error';
import { WorkbenchHeader } from '@/components/project/workbench-header';
import { WorkbenchFooterNav } from '@/components/project/workbench-footer-nav';
import { useProject } from '@/components/project/project-context';
import { listAssets, listSections, suggestVisuals } from '@/lib/api';
import { describeError } from '@/lib/errors';
import type { CreateVisualRequest, PaperSection, UserAsset, VisualAsset } from '@/lib/types';
import { useAsyncModule } from '@/lib/useAsyncModule';
import { cn } from '@/lib/utils';
import { AIGenerationDialog } from './ai-generation-dialog';
import { VisualCard } from './visual-card';
import { VisualEditorDrawer } from './visual-editor-drawer';
import { groupVisualsByLineage, useVisuals, type VisualGroup } from './use-visuals';

type FilterId = 'all' | 'pending' | 'generating' | 'ready' | 'approved' | 'failed' | 'rejected';

const FILTERS: { id: FilterId; label: string }[] = [
  { id: 'all', label: '全部' },
  { id: 'pending', label: '待处理' },
  { id: 'generating', label: '生成中' },
  { id: 'ready', label: '可批准' },
  { id: 'approved', label: '已插入' },
  { id: 'failed', label: '失败' },
  { id: 'rejected', label: '已拒绝' },
];

/**
 * 视觉工作台。**两类论文都有**这一步。
 *
 * 此前视觉能力被放在「原创论文才有的素材中心」内部，综述论文支持完整的建议、
 * 生图、审核与导出，却没有任何正常入口——用户只能手工改 URL。
 *
 * 每个数据模块独立加载：章节列表拿不到时仍能生成和调整图，只是暂时不能选插入
 * 位置；素材拿不到只影响新建数据图表。
 */
export function VisualsWorkbench() {
  const { projectId, busy, startJob } = useProject();
  const { toast } = useToast();
  const [filter, setFilter] = React.useState<FilterId>('all');
  const [editing, setEditing] = React.useState<VisualAsset | null>(null);
  const [creating, setCreating] = React.useState(false);
  const [confirming, setConfirming] = React.useState<VisualAsset | null>(null);

  const controller = useVisuals(projectId);

  /*
   * 章节列表只在需要「批准插入」时才真正有用，但它同时也是本页最便宜的一个请求。
   * 关键在于它**不能**和视觉列表绑在一起：拿不到章节不该让整页视觉消失。
   */
  const sectionsModule = useAsyncModule<PaperSection[]>(
    () => listSections(projectId).then((res) => res.data),
    [],
    [projectId],
  );

  /*
   * 原始表格素材只有「新建数据图表」时才需要。延迟到用户打开编辑器再拉，
   * 平时不为一个可能用不到的表格付一次往返。
   */
  const [assetsWanted, setAssetsWanted] = React.useState(false);
  const assetsModule = useAsyncModule<UserAsset[]>(
    () => (assetsWanted ? listAssets(projectId).then((res) => res.data) : Promise.resolve([])),
    [],
    [projectId, assetsWanted],
  );

  const groups = React.useMemo(
    () => groupVisualsByLineage(controller.visuals),
    [controller.visuals],
  );
  const visible = React.useMemo(() => groups.filter((g) => matches(g, filter)), [groups, filter]);
  const counts = React.useMemo(() => countByFilter(groups), [groups]);

  const analyse = async () => {
    try {
      const started = await suggestVisuals(projectId);
      startJob(started.data, '后端不可用：无法生成视觉建议');
    } catch (err) {
      toast({ title: '视觉建议未能启动', description: describeError(err), variant: 'error' });
    }
  };

  const openEditor = (visual: VisualAsset) => {
    // 打开编辑器时才需要素材列表（数据图表要显示来源）。
    if (visual.kind === 'chart') setAssetsWanted(true);
    setEditing(visual);
  };

  /** 新建要选数据来源，因此必须先把表格素材拉回来。 */
  const openCreate = () => {
    setAssetsWanted(true);
    setEditing(null);
    setCreating(true);
  };

  /** AI 生图必须先过确认框；确定性图表直接生成，它们没有外部调用也不计费。 */
  const requestGenerate = (visual: VisualAsset) => {
    if (visual.kind === 'ai_image') setConfirming(visual);
    else void controller.generate(visual);
  };

  const approve = async (visual: VisualAsset, sectionKey: string, blockIndex: number) => {
    const section = sectionsModule.data.find((item) => item.section_key === sectionKey);
    try {
      await controller.approve(visual, sectionKey, blockIndex, section?.updated_at ?? null);
      toast({ title: '图片已插入论文', variant: 'success' });
      sectionsModule.reload();
    } catch (err) {
      toast({ title: '图片未能插入', description: describeError(err), variant: 'error' });
    }
  };

  return (
    <div className="space-y-4" data-testid="visuals-workbench">
      <WorkbenchHeader
        title="视觉工作台"
        description="图表、示意图与 AI 插图的生成、审核与插入。建议不会自动进入论文。"
        actions={
          <>
            <Button variant="outline" onClick={analyse} disabled={busy || !projectId}>
              {busy ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="h-4 w-4" />
              )}
              分析全文
            </Button>
            <Button onClick={openCreate} disabled={!projectId}>
              <Plus className="h-4 w-4" />
              新建视觉
            </Button>
          </>
        }
      />

      <AIAvailabilityNotice
        available={controller.aiGenerationAvailable}
        settingsLoaded={!controller.settingsError}
      />

      {/* 设置接口失败只该禁用 AI 按钮，确定性图表照常可用。 */}
      <ModuleError
        label="图像服务配置"
        error={controller.settingsError}
        onRetry={controller.reloadSettings}
      />
      <ModuleError label="章节列表" error={sectionsModule.error} onRetry={sectionsModule.reload} />
      <ModuleError label="素材列表" error={assetsModule.error} onRetry={assetsModule.reload} />

      <div className="grid gap-4 lg:grid-cols-[11rem,minmax(0,1fr)]">
        <nav aria-label="视觉筛选" className="lg:sticky lg:top-4 lg:self-start">
          <ul className="flex gap-1 overflow-x-auto pb-1 lg:flex-col lg:overflow-visible">
            {FILTERS.map((item) => (
              <li key={item.id} className="shrink-0">
                <button
                  type="button"
                  onClick={() => setFilter(item.id)}
                  aria-current={filter === item.id}
                  className={cn(
                    'flex w-full items-center justify-between gap-2 rounded-md px-2.5 py-1.5 text-left text-sm transition-colors',
                    filter === item.id
                      ? 'bg-accent font-medium text-foreground'
                      : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground',
                  )}
                >
                  <span className="whitespace-nowrap">{item.label}</span>
                  {counts[item.id] > 0 && (
                    <span className="tabular-nums text-xs text-muted-foreground">
                      {counts[item.id]}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </nav>

        <LoadState
          loading={controller.loading && !controller.ready}
          error={controller.error}
          onRetry={controller.reload}
          skeletonClassName="h-64"
        >
          {visible.length === 0 ? (
            <EmptyState hasAny={groups.length > 0} onAnalyse={analyse} busy={busy} />
          ) : (
            <div className="grid gap-3 xl:grid-cols-2">
              {visible.map((group) => (
                <VisualCard
                  key={group.rootId}
                  group={group}
                  projectId={projectId}
                  sections={sectionsModule.data}
                  job={controller.jobFor(group.latest.id)}
                  aiGenerationAvailable={controller.aiGenerationAvailable}
                  aiJobRunning={controller.aiJobRunning}
                  deterministicSlotsFull={controller.deterministicSlotsFull}
                  actions={{
                    onEdit: openEditor,
                    onGenerate: requestGenerate,
                    onRevision: (visual) => void controller.createRevision(visual),
                    onApprove: approve,
                    onReject: (visual) => void controller.reject(visual),
                  }}
                />
              ))}
            </div>
          )}
        </LoadState>
      </div>

      <VisualEditorDrawer
        open={editing !== null || creating}
        onClose={() => {
          setEditing(null);
          setCreating(false);
        }}
        projectId={projectId}
        visual={editing}
        assets={assetsModule.data}
        targetSectionKey={sectionsModule.data[0]?.section_key ?? null}
        capabilities={controller.capabilities}
        aiGenerationAvailable={controller.aiGenerationAvailable}
        onSubmit={(payload: Partial<CreateVisualRequest>) => {
          if (editing) {
            void controller.edit(editing, payload);
            return;
          }
          // 新建的确定性图表/示意图直接出预览（无外部调用、不计费）；
          // AI 插图只创建 proposal，生成仍要走确认框。
          const spec = payload.spec as { kind?: string } | undefined;
          void controller.create(
            payload as CreateVisualRequest,
            spec?.kind !== 'ai_image',
          );
        }}
      />

      <AIGenerationDialog
        visual={confirming}
        capabilities={controller.capabilities}
        onCancel={() => setConfirming(null)}
        onConfirm={(visual) => {
          setConfirming(null);
          void controller.generate(visual);
        }}
      />

      <WorkbenchFooterNav current="visuals" />
    </div>
  );
}

/**
 * AI 生图不可用时**明说原因**。
 *
 * 此前它只是悄悄不出现：建议里全是启发式示意图，新建里也没有 AI 选项，
 * 用户看不出这是「功能没开」还是「产品就没有这个能力」。
 */
function AIAvailabilityNotice({
  available,
  settingsLoaded,
}: {
  available: boolean;
  settingsLoaded: boolean;
}) {
  if (available || !settingsLoaded) return null;
  return (
    <p className="flex items-start gap-2 rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
      <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>
        AI 概念插图当前不可用：需要在服务端启用 <code className="font-mono">AI_IMAGES_ENABLED</code>{' '}
        并配置图像服务凭据。图表与示意图不受影响——它们由本地渲染，不调用外部服务，也不计费。
      </span>
    </p>
  );
}

function EmptyState({
  hasAny,
  onAnalyse,
  busy,
}: {
  hasAny: boolean;
  onAnalyse: () => void;
  busy: boolean;
}) {
  return (
    <div className="rounded-xl border border-dashed p-8 text-center">
      <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted">
        <ImageIcon className="h-6 w-6 text-muted-foreground" />
      </div>
      <p className="mt-3 font-medium">{hasAny ? '这个筛选下没有视觉' : '还没有视觉建议'}</p>
      <p className="mx-auto mt-1 max-w-lg text-sm text-muted-foreground">
        {hasAny
          ? '换一个筛选条件看看。'
          : '正文写完后点「分析全文」，系统会给出图表与示意图建议。分析本身不会调用付费的图像服务。'}
      </p>
      {!hasAny && (
        <Button className="mt-4" onClick={onAnalyse} disabled={busy}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          分析全文
        </Button>
      )}
    </div>
  );
}

/** 组的状态取最新版本——用户关心的是「这张图现在到哪一步了」。 */
function matches(group: VisualGroup, filter: FilterId): boolean {
  if (filter === 'all') return true;
  const visual = group.approved ?? group.latest;
  switch (filter) {
    case 'approved':
      return visual.review_status === 'approved';
    case 'rejected':
      return visual.review_status === 'rejected';
    case 'generating':
      return (
        visual.review_status === 'pending' &&
        (visual.generation_status === 'queued' || visual.generation_status === 'running')
      );
    case 'failed':
      return visual.review_status === 'pending' && visual.generation_status === 'failed';
    case 'ready':
      return visual.review_status === 'pending' && visual.generation_status === 'ready';
    case 'pending':
      return visual.review_status === 'pending' && visual.generation_status === 'proposed';
    default:
      return true;
  }
}

function countByFilter(groups: VisualGroup[]): Record<FilterId, number> {
  const counts = {
    all: groups.length,
    pending: 0,
    generating: 0,
    ready: 0,
    approved: 0,
    failed: 0,
    rejected: 0,
  } as Record<FilterId, number>;
  for (const group of groups) {
    for (const id of FILTERS.map((item) => item.id)) {
      if (id !== 'all' && matches(group, id)) counts[id] += 1;
    }
  }
  return counts;
}
