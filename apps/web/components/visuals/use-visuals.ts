'use client';

import * as React from 'react';
import { useToast } from '@/components/ui/toast';
import { useJobFinished, useProject } from '@/components/project/project-context';
import {
  approveVisual,
  createVisual,
  generateVisual,
  getRuntimeSettings,
  listVisuals,
  regenerateVisual,
  rejectVisual,
  updateVisual,
} from '@/lib/api';
import { describeError } from '@/lib/errors';
import type {
  CreateVisualRequest,
  ImageProviderCapabilities,
  Job,
  RegenerateVisualRequest,
  VisualAsset,
} from '@/lib/types';
import { useAsyncModule } from '@/lib/useAsyncModule';
import type { TrackedJob } from '@/lib/useJobTracker';

/**
 * 视觉能力的单一实现。
 *
 * 此前素材中心与写作台各有一套视觉组件，行为并不一致：素材中心能改规格、能重
 * 生成版本，却没有「批准并插入」；写作台能批准插入，却连「调整」入口都没有。
 * 用户必须在两个页面之间来回切换，还得记住哪个操作在哪边。
 *
 * 这里把数据、能力声明与全部动作收敛成一份，视觉工作台与写作台共用。
 */
export interface VisualsController {
  visuals: VisualAsset[];
  loading: boolean;
  error: string | null;
  ready: boolean;
  reload: () => void;

  /** AI 生图是否可用（已启用 + provider 已配置）。 */
  aiGenerationAvailable: boolean;
  /** 提供商真实能力；为空时界面不得对尺寸做任何承诺。 */
  capabilities: ImageProviderCapabilities | null;
  settingsError: string | null;
  reloadSettings: () => void;

  /** 该视觉当前正在跑的任务（卡片级）。没有则为 null。 */
  jobFor: (visualId: string) => TrackedJob | null;
  /** 是否已有 AI 生图在跑——同一项目只允许一个，防误触重复计费。 */
  aiJobRunning: boolean;
  /** 确定性渲染已达并发上限（2 个）。它不计费，但也不该无限压 visuald。 */
  deterministicSlotsFull: boolean;

  generate: (visual: VisualAsset) => Promise<void>;
  createRevision: (
    visual: VisualAsset,
    payload?: RegenerateVisualRequest,
    generateNow?: boolean,
  ) => Promise<void>;
  edit: (visual: VisualAsset, payload: Partial<CreateVisualRequest>) => Promise<void>;
  create: (payload: CreateVisualRequest, generateNow: boolean) => Promise<void>;
  approve: (
    visual: VisualAsset,
    sectionKey: string,
    blockIndex: number,
    expectedSectionUpdatedAt?: string | null,
  ) => Promise<VisualAsset | undefined>;
  reject: (visual: VisualAsset) => Promise<void>;
}

export function useVisuals(projectId: string): VisualsController {
  const { jobs, startJob } = useProject();
  const { toast } = useToast();

  const listModule = useAsyncModule<VisualAsset[]>(
    () => listVisuals(projectId).then((res) => res.data),
    [],
    [projectId],
  );

  /*
   * 运行时设置独立成一个模块：它挂掉时只该禁用 AI 按钮，确定性图表仍然可用。
   * 此前它和视觉列表挤在同一个 Promise.all 里，一次 500 让整页都打不开。
   */
  const settingsModule = useAsyncModule<{
    available: boolean;
    capabilities: ImageProviderCapabilities | null;
  }>(
    () =>
      getRuntimeSettings().then((res) => ({
        available: Boolean(res.data?.ai_images_enabled && res.data?.image_provider_configured),
        capabilities: res.data?.image_capabilities ?? null,
      })),
    { available: false, capabilities: null },
    [],
  );

  /**
   * `visual_id → job_id`。
   *
   * 卡片各自显示自己的排队/生成状态，互不覆盖，也不再占用项目级任务槽位。
   * 任务结束后由 useJobFinished 清理对应条目。
   */
  const [jobByVisual, setJobByVisual] = React.useState<Record<string, string>>({});

  const reloadRef = React.useRef(listModule.reload);
  React.useEffect(() => {
    reloadRef.current = listModule.reload;
  });

  useJobFinished(
    React.useCallback((job: Job) => {
      if (job.kind !== 'visual') return;
      setJobByVisual((prev) => {
        const next = Object.fromEntries(
          Object.entries(prev).filter(([, jobId]) => jobId !== job.id),
        );
        return Object.keys(next).length === Object.keys(prev).length ? prev : next;
      });
      reloadRef.current();
    }, []),
  );

  const jobFor = React.useCallback(
    (visualId: string) => {
      const jobId = jobByVisual[visualId];
      return jobId ? (jobs[jobId] ?? null) : null;
    },
    [jobByVisual, jobs],
  );

  const visuals = listModule.data;

  /*
   * 并发上限按「会不会花钱」分两档：
   *   - AI 生图最多 1 个。误触两次「生成预览」不该产生两次外部计费。
   *   - 确定性图表 / 示意图最多 2 个。它们不计费，但一次点开十张卡片全部开跑
   *     只会把 visuald 压垮，然后十张一起超时失败。
   */
  const { aiJobRunning, deterministicSlotsFull } = React.useMemo(() => {
    const kindById = new Map(visuals.map((item) => [item.id, item.kind]));
    let ai = 0;
    let deterministic = 0;
    for (const [visualId, jobId] of Object.entries(jobByVisual)) {
      if (!jobs[jobId]) continue;
      if (kindById.get(visualId) === 'ai_image') ai += 1;
      else deterministic += 1;
    }
    return { aiJobRunning: ai > 0, deterministicSlotsFull: deterministic >= 2 };
  }, [visuals, jobByVisual, jobs]);

  const track = React.useCallback(
    (visualId: string, job: Job | undefined, fallback: string) => {
      const jobId = startJob(job, fallback);
      if (jobId) setJobByVisual((prev) => ({ ...prev, [visualId]: jobId }));
    },
    [startJob],
  );

  const generate = React.useCallback(
    async (visual: VisualAsset) => {
      try {
        const started = await generateVisual(projectId, visual.id);
        track(visual.id, started.data, '后端不可用：无法生成预览');
        listModule.reload();
      } catch (err) {
        toast({ title: '预览未能生成', description: describeError(err), variant: 'error' });
      }
    },
    [projectId, track, listModule, toast],
  );

  const createRevision = React.useCallback(
    async (
      visual: VisualAsset,
      payload?: RegenerateVisualRequest,
      generateNow = visual.kind !== 'ai_image',
    ) => {
      try {
        const revision = await regenerateVisual(projectId, visual.id, payload);
        if (!revision.data) {
          toast({ title: '新版本未能创建', description: '后端不可用。', variant: 'error' });
          return;
        }
        if (generateNow) {
          const started = await generateVisual(projectId, revision.data.id);
          track(revision.data.id, started.data, '后端不可用：无法生成新版本预览');
        }
        listModule.reload();
      } catch (err) {
        toast({ title: '重新生成失败', description: describeError(err), variant: 'error' });
      }
    },
    [projectId, track, listModule, toast],
  );

  const edit = React.useCallback(
    async (visual: VisualAsset, payload: Partial<CreateVisualRequest>) => {
      try {
        // 已有预览的资产不能原地改规格（后端 409）：那会让屏幕上的图与它的
        // 规格对不上。这种情况走新版本，旧版本仍留在 lineage 里可比较。
        if (
          visual.review_status === 'pending' &&
          (visual.generation_status === 'proposed' || visual.generation_status === 'failed')
        ) {
          await updateVisual(projectId, visual.id, payload);
          listModule.reload();
        } else {
          await createRevision(visual, payload);
        }
      } catch (err) {
        toast({ title: '修改未保存', description: describeError(err), variant: 'error' });
      }
    },
    [projectId, listModule, createRevision, toast],
  );

  const create = React.useCallback(
    async (payload: CreateVisualRequest, generateNow: boolean) => {
      try {
        const created = await createVisual(projectId, payload);
        if (!created.data) {
          toast({ title: '视觉未能创建', description: '后端不可用。', variant: 'error' });
          return;
        }
        if (generateNow) {
          const started = await generateVisual(projectId, created.data.id);
          track(created.data.id, started.data, '后端不可用：无法生成预览');
        }
        listModule.reload();
      } catch (err) {
        toast({ title: '视觉未能创建', description: describeError(err), variant: 'error' });
      }
    },
    [projectId, track, listModule, toast],
  );

  const approve = React.useCallback(
    async (
      visual: VisualAsset,
      sectionKey: string,
      blockIndex: number,
      expectedSectionUpdatedAt?: string | null,
    ) => {
      const result = await approveVisual(
        projectId,
        visual.id,
        sectionKey,
        blockIndex,
        expectedSectionUpdatedAt,
      );
      listModule.reload();
      return result.data;
    },
    [projectId, listModule],
  );

  const reject = React.useCallback(
    async (visual: VisualAsset) => {
      try {
        await rejectVisual(projectId, visual.id);
        listModule.reload();
      } catch (err) {
        toast({ title: '建议未能拒绝', description: describeError(err), variant: 'error' });
      }
    },
    [projectId, listModule, toast],
  );

  return {
    visuals,
    loading: listModule.loading,
    error: listModule.error,
    ready: listModule.ready,
    reload: listModule.reload,
    aiGenerationAvailable: settingsModule.data.available,
    capabilities: settingsModule.data.capabilities,
    settingsError: settingsModule.error,
    reloadSettings: settingsModule.reload,
    jobFor,
    aiJobRunning,
    deterministicSlotsFull,
    generate,
    createRevision,
    edit,
    create,
    approve,
    reject,
  };
}

/**
 * 按 lineage 折叠版本。
 *
 * `regenerate` 会建一条 `supersedes_id` 指向旧版的新行，于是同一张图的 v1、v2、v3
 * 此前在列表里平铺，三五张图就把页面占满。这里沿 `supersedes_id` 链把它们归组，
 * 默认只展示最新版与当前已批准版。
 */
export interface VisualGroup {
  /** lineage 根的 id，用作稳定 key。 */
  rootId: string;
  /** 版本从新到旧。 */
  versions: VisualAsset[];
  latest: VisualAsset;
  approved: VisualAsset | undefined;
}

export function groupVisualsByLineage(visuals: VisualAsset[]): VisualGroup[] {
  const byId = new Map(visuals.map((visual) => [visual.id, visual]));

  const rootOf = (visual: VisualAsset): string => {
    const seen = new Set<string>();
    let current = visual;
    while (current.supersedes_id && byId.has(current.supersedes_id)) {
      // 环在正常数据里不可能出现，但一条脏数据不该让整个页面挂掉。
      if (seen.has(current.id)) break;
      seen.add(current.id);
      current = byId.get(current.supersedes_id)!;
    }
    return current.id;
  };

  const groups = new Map<string, VisualAsset[]>();
  for (const visual of visuals) {
    const root = rootOf(visual);
    const bucket = groups.get(root);
    if (bucket) bucket.push(visual);
    else groups.set(root, [visual]);
  }

  return Array.from(groups.entries()).map(([rootId, members]) => {
    const versions = [...members].sort((a, b) => b.version - a.version);
    return {
      rootId,
      versions,
      latest: versions[0],
      approved: versions.find((item) => item.review_status === 'approved'),
    };
  });
}
