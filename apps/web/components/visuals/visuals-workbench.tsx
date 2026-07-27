'use client';

import * as React from 'react';
import { AlertTriangle, Image as ImageIcon, Info, Loader2, RefreshCw, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/toast';
import { LoadState } from '@/components/layout/load-state';
import { ModuleError } from '@/components/layout/module-error';
import { useProject } from '@/components/project/project-context';
import { WorkbenchFooterNav } from '@/components/project/workbench-footer-nav';
import { WorkbenchHeader } from '@/components/project/workbench-header';
import { draftVisual, listAssets, listSections, suggestVisuals } from '@/lib/api';
import { describeError } from '@/lib/errors';
import type {
  PaperSection,
  RegenerateVisualRequest,
  UserAsset,
  VisualAsset,
  VisualDraft,
} from '@/lib/types';
import { useAsyncModule } from '@/lib/useAsyncModule';
import { cn } from '@/lib/utils';
import { AIGenerationDialog } from './ai-generation-dialog';
import { VisualCard } from './visual-card';
import { VisualEditorDrawer } from './visual-editor-drawer';
import { groupVisualsByLineage, useVisuals, type VisualGroup } from './use-visuals';

type FilterId = 'attention' | 'ready' | 'failed' | 'generating' | 'history' | 'all';

const FILTERS: { id: FilterId; label: string }[] = [
  { id: 'attention', label: '需要处理' },
  { id: 'ready', label: '可批准' },
  { id: 'failed', label: '失败' },
  { id: 'generating', label: '生成中' },
  { id: 'history', label: '历史' },
  { id: 'all', label: '全部' },
];

/** Intent → contextual draft → preview → approve. Internal protocols stay in the inspector. */
export function VisualsWorkbench() {
  const { projectId, busy, startJob } = useProject();
  const { toast } = useToast();
  const [filter, setFilter] = React.useState<FilterId>('attention');
  const [editing, setEditing] = React.useState<VisualAsset | null>(null);
  const [confirming, setConfirming] = React.useState<VisualAsset | null>(null);
  const [assetsWanted, setAssetsWanted] = React.useState(false);
  const controller = useVisuals(projectId);

  const sectionsModule = useAsyncModule<PaperSection[]>(
    () => listSections(projectId).then((result) => result.data),
    [],
    [projectId],
  );
  const assetsModule = useAsyncModule<UserAsset[]>(
    () => (assetsWanted ? listAssets(projectId).then((result) => result.data) : Promise.resolve([])),
    [],
    [projectId, assetsWanted],
  );

  const groups = React.useMemo(() => groupVisualsByLineage(controller.visuals), [controller.visuals]);
  const visible = React.useMemo(
    () => groups.filter((group) => matches(group, filter)).sort(compareAttention),
    [filter, groups],
  );
  const counts = React.useMemo(() => countByFilter(groups), [groups]);
  const staleCount = groups.filter((group) => group.latest.stale).length;

  const analyse = async () => {
    try {
      const started = await suggestVisuals(projectId);
      startJob(started.data, '后端不可用：无法生成视觉建议');
    } catch (error) {
      toast({ title: '视觉建议未能启动', description: describeError(error), variant: 'error' });
    }
  };

  const requestGenerate = (visual: VisualAsset) => {
    if (visual.kind === 'ai_image') setConfirming(visual);
    else void controller.generate(visual);
  };

  const approve = async (visual: VisualAsset, sectionKey: string, blockIndex: number) => {
    const section = sectionsModule.data.find((item) => item.section_key === sectionKey);
    try {
      await controller.approve(visual, sectionKey, blockIndex, section?.updated_at ?? null);
      toast({ title: '视觉已插入论文', variant: 'success' });
      sectionsModule.reload();
    } catch (error) {
      toast({ title: '视觉未能插入', description: describeError(error), variant: 'error' });
    }
  };

  return (
    <div className="space-y-5" data-testid="visuals-workbench">
      <WorkbenchHeader
        title="视觉工作台"
        description="告诉系统读者应该理解什么，其余选型与上下文匹配交给系统。"
        actions={
          <Button variant="outline" onClick={analyse} disabled={busy || !projectId}>
            {busy ? <Loader2 className="animate-spin" /> : <RefreshCw />} 分析全文
          </Button>
        }
      />

      <IntentComposer
        projectId={projectId}
        sections={sectionsModule.data}
        assets={assetsModule.data}
        assetsLoaded={assetsWanted}
        onWantAssets={() => setAssetsWanted(true)}
        aiAvailable={controller.aiGenerationAvailable}
        onCreate={async (draft) => {
          await controller.create(
            {
              title: draft.title,
              caption: draft.caption,
              alt_text: draft.alt_text,
              spec: draft.spec,
              target_section_key: draft.target_section_key,
              suggested_block_index: draft.suggested_block_index,
            },
            draft.kind !== 'ai_image',
          );
        }}
      />

      <AIAvailabilityNotice available={controller.aiGenerationAvailable} settingsLoaded={!controller.settingsError} />
      {staleCount > 0 && (
        <div className="flex flex-col gap-2 rounded-lg border border-warning/35 bg-warning/10 px-3 py-2 text-sm sm:flex-row sm:items-center sm:justify-between">
          <span className="flex items-start gap-2 text-warning-foreground">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            {staleCount} 张视觉基于旧版正文。可继续使用，也可重新分析全文。
          </span>
          <Button variant="outline" size="sm" onClick={analyse} disabled={busy}>重新分析</Button>
        </div>
      )}

      <ModuleError label="图像服务配置" error={controller.settingsError} onRetry={controller.reloadSettings} />
      <ModuleError label="章节列表" error={sectionsModule.error} onRetry={sectionsModule.reload} />
      {assetsWanted && <ModuleError label="素材列表" error={assetsModule.error} onRetry={assetsModule.reload} />}

      <div className="grid gap-5 lg:grid-cols-[10rem,minmax(0,1fr)]">
        <nav aria-label="视觉筛选" className="lg:sticky lg:top-4 lg:self-start">
          <ul className="flex gap-1 overflow-x-auto pb-1 lg:flex-col lg:overflow-visible">
            {FILTERS.map((item) => (
              <li key={item.id} className="shrink-0">
                <button
                  type="button"
                  onClick={() => setFilter(item.id)}
                  aria-current={filter === item.id}
                  className={cn(
                    'flex min-h-11 w-full items-center justify-between gap-3 rounded-lg px-3 text-left text-sm transition-colors',
                    filter === item.id
                      ? 'bg-accent font-medium text-foreground'
                      : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground',
                  )}
                >
                  <span className="whitespace-nowrap">{item.label}</span>
                  {counts[item.id] > 0 && <span className="tabular-nums text-xs">{counts[item.id]}</span>}
                </button>
              </li>
            ))}
          </ul>
        </nav>

        <LoadState loading={controller.loading && !controller.ready} error={controller.error} onRetry={controller.reload} skeletonClassName="h-64">
          {visible.length === 0 ? (
            <EmptyState hasAny={groups.length > 0} />
          ) : (
            <div className="grid gap-4 xl:grid-cols-2">
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
                    onEdit: (visual) => {
                      if (visual.kind === 'chart') setAssetsWanted(true);
                      setEditing(visual);
                    },
                    onGenerate: requestGenerate,
                    onRevision: (visual, payload, generateNow) => void controller.createRevision(visual, payload, generateNow),
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
        open={editing !== null}
        onClose={() => setEditing(null)}
        projectId={projectId}
        visual={editing}
        assets={assetsModule.data}
        capabilities={controller.capabilities}
        aiGenerationAvailable={controller.aiGenerationAvailable}
        onSubmit={(payload: RegenerateVisualRequest) => {
          if (editing) void controller.createRevision(editing, payload);
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

function IntentComposer({
  projectId,
  sections,
  assets,
  assetsLoaded,
  onWantAssets,
  aiAvailable,
  onCreate,
}: {
  projectId: string;
  sections: PaperSection[];
  assets: UserAsset[];
  assetsLoaded: boolean;
  onWantAssets: () => void;
  aiAvailable: boolean;
  onCreate: (draft: VisualDraft) => Promise<void>;
}) {
  const { toast } = useToast();
  const [intent, setIntent] = React.useState('');
  const [kind, setKind] = React.useState<'auto' | 'chart' | 'diagram' | 'ai_image'>('auto');
  const [sectionKey, setSectionKey] = React.useState('');
  const [selectedAssets, setSelectedAssets] = React.useState<string[]>([]);
  const [drafting, setDrafting] = React.useState(false);
  const [result, setResult] = React.useState<VisualDraft | null>(null);
  const [detailsOpen, setDetailsOpen] = React.useState(false);

  const submit = async () => {
    if (!intent.trim() || drafting) return;
    setDrafting(true);
    try {
      const response = await draftVisual(projectId, {
        kind,
        intent: intent.trim(),
        target_section_key: sectionKey || null,
        source_asset_refs: selectedAssets,
      });
      if (!response.data) throw new Error('未能形成视觉草稿');
      setResult(response.data);
      await onCreate(response.data);
      setIntent('');
      toast({
        title: response.data.kind === 'ai_image' ? '插图草稿已创建' : '正在生成本地预览',
        description: response.data.reason,
        variant: 'success',
      });
    } catch (error) {
      toast({ title: '未能创建视觉草稿', description: describeError(error), variant: 'error' });
    } finally {
      setDrafting(false);
    }
  };

  return (
    <section className="rounded-2xl border bg-card p-4 shadow-sm sm:p-5" aria-labelledby="visual-intent-title">
      <div className="flex items-start gap-3">
        <div className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
          <Sparkles className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1 space-y-3">
          <div>
            <h3 id="visual-intent-title" className="font-medium">描述你想让读者理解什么</h3>
            <p className="mt-0.5 text-xs text-muted-foreground">默认读取全文并自动选择图表、示意图或概念插图。</p>
          </div>
          <Textarea
            id="visual-intent"
            value={intent}
            onChange={(event) => setIntent(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') void submit();
            }}
            placeholder="例如：让读者一眼看懂检索、筛选和证据整合之间的流程"
            aria-label="视觉意图"
            className="min-h-24 resize-none border-0 bg-muted/45 text-base shadow-none focus-visible:ring-1"
          />
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <button type="button" className="min-h-11 text-left text-xs text-muted-foreground underline-offset-4 hover:underline" onClick={() => setDetailsOpen((value) => !value)} aria-expanded={detailsOpen}>
              {detailsOpen ? '收起范围与类型' : '限定范围或覆盖类型（可选）'}
            </button>
            <Button className="h-11 sm:min-w-36" onClick={() => void submit()} disabled={!intent.trim() || drafting}>
              {drafting ? <Loader2 className="animate-spin" /> : <Sparkles />} {drafting ? '正在理解…' : '开始创作'}
            </Button>
          </div>

          {detailsOpen && (
            <div className="grid gap-4 border-t pt-4 md:grid-cols-2">
              <label className="space-y-2 text-sm">
                <span className="font-medium">正文范围</span>
                <select value={sectionKey} onChange={(event) => setSectionKey(event.target.value)} className="h-11 w-full rounded-md border bg-background px-3">
                  <option value="">全文自动匹配</option>
                  {sections.map((section) => <option key={section.section_key} value={section.section_key}>{section.title}</option>)}
                </select>
              </label>
              <fieldset className="space-y-2">
                <legend className="text-sm font-medium">视觉类型</legend>
                <div className="grid grid-cols-2 gap-2">
                  {([
                    ['auto', '自动选择'],
                    ['chart', '数据图表'],
                    ['diagram', '示意图'],
                    ['ai_image', '概念插图'],
                  ] as const).map(([value, label]) => (
                    <button
                      key={value}
                      type="button"
                      onClick={() => setKind(value)}
                      disabled={value === 'ai_image' && !aiAvailable}
                      aria-pressed={kind === value}
                      className={cn('min-h-11 rounded-lg border px-3 text-sm disabled:opacity-45', kind === value && 'border-primary bg-primary/5')}
                    >{label}</button>
                  ))}
                </div>
              </fieldset>
              <div className="md:col-span-2">
                {!assetsLoaded ? (
                  <Button variant="ghost" size="sm" onClick={onWantAssets}>选择项目素材（可选）</Button>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {assets.filter((asset) => asset.asset_ref).map((asset) => {
                      const ref = asset.asset_ref!;
                      const selected = selectedAssets.includes(ref);
                      return (
                        <button key={asset.id} type="button" aria-pressed={selected} onClick={() => setSelectedAssets((items) => selected ? items.filter((item) => item !== ref) : [...items, ref])} className={cn('min-h-9 rounded-full border px-3 text-xs', selected && 'border-primary bg-primary/5')}>
                          {asset.title || '未命名素材'}
                        </button>
                      );
                    })}
                    {assets.length === 0 && <span className="text-xs text-muted-foreground">暂无可用素材；系统不会因此编造数据图表。</span>}
                  </div>
                )}
              </div>
            </div>
          )}

          {result && (
            <div className="rounded-lg bg-muted/45 px-3 py-2 text-xs" role="status">
              <p className="font-medium">系统选择：{kindLabel(result.kind)}</p>
              <p className="mt-0.5 text-muted-foreground">{result.reason}</p>
              {result.context_summary && <p className="mt-1 text-muted-foreground">上下文：{result.context_summary}</p>}
              {result.warnings.map((warning) => <p key={warning} className="mt-1 text-warning-foreground">{warning}</p>)}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function AIAvailabilityNotice({ available, settingsLoaded }: { available: boolean; settingsLoaded: boolean }) {
  if (available || !settingsLoaded) return null;
  return (
    <p className="flex items-start gap-2 rounded-lg border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
      <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      AI 概念插图当前不可用；数据图表与示意图仍由本地渲染。启用外部图像服务后，每次调用依然需要单独确认。
    </p>
  );
}

function EmptyState({ hasAny }: { hasAny: boolean }) {
  return (
    <div className="rounded-xl border border-dashed p-8 text-center">
      <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted"><ImageIcon className="h-6 w-6 text-muted-foreground" /></div>
      <p className="mt-3 font-medium">{hasAny ? '这个视图里没有视觉' : '还没有视觉'}</p>
      <p className="mx-auto mt-1 max-w-lg text-sm text-muted-foreground">在上方描述读者需要理解的内容，系统会根据正文和素材形成第一版预览。</p>
    </div>
  );
}

function matches(group: VisualGroup, filter: FilterId): boolean {
  const visual = group.latest;
  if (filter === 'all') return true;
  if (filter === 'history') return visual.review_status === 'approved' || visual.review_status === 'rejected';
  if (filter === 'attention') return visual.review_status === 'pending';
  if (visual.review_status !== 'pending') return false;
  if (filter === 'ready') return visual.generation_status === 'ready';
  if (filter === 'failed') return visual.generation_status === 'failed';
  if (filter === 'generating') return visual.generation_status === 'queued' || visual.generation_status === 'running';
  return false;
}

function compareAttention(a: VisualGroup, b: VisualGroup): number {
  return priority(a.latest) - priority(b.latest);
}

function priority(visual: VisualAsset): number {
  if (visual.review_status === 'approved') return 4;
  if (visual.review_status === 'rejected') return 5;
  if (visual.generation_status === 'ready') return 0;
  if (visual.generation_status === 'failed') return 1;
  if (visual.generation_status === 'proposed') return 2;
  return 3;
}

function countByFilter(groups: VisualGroup[]): Record<FilterId, number> {
  return FILTERS.reduce((counts, item) => ({ ...counts, [item.id]: groups.filter((group) => matches(group, item.id)).length }), {} as Record<FilterId, number>);
}

function kindLabel(kind: VisualDraft['kind']): string {
  return kind === 'chart' ? '数据图表' : kind === 'diagram' ? '学术示意图' : 'AI 概念插图';
}
