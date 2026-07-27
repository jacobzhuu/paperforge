'use client';

import * as React from 'react';
import { getProject, getWhitelist } from '@/lib/api';
import { describeError } from '@/lib/errors';
import { useJobTracker, type TrackedJob } from '@/lib/useJobTracker';
import { useProjectProgress, type ProjectProgress } from '@/lib/useProjectProgress';
import type { DataSource, Job, JobEvent, PaperType, Project } from '@/lib/types';

interface ProjectContextValue {
  projectId: string;
  project: Project | undefined;
  paperType: PaperType;
  /** R1 写作白名单。library/outline/write 三处此前各拉一遍，这里统一供给。 */
  whitelist: string[];
  loading: boolean;
  error: string | null;
  /** 任一依赖降级时为 'mock'。 */
  source: DataSource;
  note?: string;
  reload: () => void;
  /** 按实际产物推断的进度（不使用永远为 'draft' 的 project.status）。 */
  progress: ProjectProgress;
  /** 项目级任务追踪：跨工作台存活，切页不丢进度与已收集的降级警告。 */
  tracked: TrackedJob | null;
  /** 全部在跑的任务，按 job id 索引。视觉卡片按自己的 job id 取自己的进度。 */
  jobs: Record<string, TrackedJob>;
  busy: boolean;
  jobMessage: string | null;
  /** 返回 job id（后端不可用时为 null），供卡片级 `visual_id → job_id` 映射使用。 */
  startJob: (started: Job | undefined, fallbackMessage: string) => string | null;
  /** 跳过剩余的连贯性润色：已润色的保留，剩下的直接交付初稿。 */
  skipPolish: (jobId: string) => Promise<void>;
  /** 任务结束后想额外刷新自身数据的页面在此登记。 */
  onJobFinished: (handler: (job: Job) => void) => () => void;
  /** 需要读具体事件负载的页面（如导出中心的 render.completed）在此登记。 */
  onJobEvent: (handler: (event: JobEvent) => void) => () => void;
}

const ProjectContext = React.createContext<ProjectContextValue | null>(null);

export function ProjectProvider({
  projectId,
  children,
}: {
  projectId: string;
  children: React.ReactNode;
}) {
  const [project, setProject] = React.useState<Project | undefined>();
  const [whitelist, setWhitelist] = React.useState<string[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [source, setSource] = React.useState<DataSource>('live');
  const [note, setNote] = React.useState<string | undefined>();
  const [token, setToken] = React.useState(0);

  // 订阅者集合：各工作台在任务结束后按需自行 reload，避免 Provider 反向依赖页面。
  const finishHandlers = React.useRef(new Set<(job: Job) => void>());
  const eventHandlers = React.useRef(new Set<(event: JobEvent) => void>());

  const onJobFinished = React.useCallback((handler: (job: Job) => void) => {
    finishHandlers.current.add(handler);
    return () => {
      finishHandlers.current.delete(handler);
    };
  }, []);

  const onJobEvent = React.useCallback((handler: (event: JobEvent) => void) => {
    eventHandlers.current.add(handler);
    return () => {
      eventHandlers.current.delete(handler);
    };
  }, []);

  const handleEvent = React.useCallback((event: JobEvent) => {
    eventHandlers.current.forEach((fn) => fn(event));
  }, []);

  const reload = React.useCallback(() => setToken((t) => t + 1), []);

  React.useEffect(() => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    let alive = true;
    setError(null);
    Promise.all([getProject(projectId), getWhitelist(projectId)])
      .then(([proj, wl]) => {
        if (!alive) return;
        setProject(proj.data);
        setWhitelist(wl.data);
        const degraded = [proj, wl].find((r) => r.source === 'mock');
        setSource(degraded ? 'mock' : 'live');
        setNote(degraded?.note);
        setLoading(false);
      })
      .catch((err) => {
        if (!alive) return;
        setError(describeError(err));
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [projectId, token]);

  const handleFinished = React.useCallback(
    (job: Job) => {
      // 项目自身的计数（library_count / section_count）也会变，一并刷新。
      reload();
      finishHandlers.current.forEach((fn) => fn(job));
    },
    [reload],
  );

  const { tracked, jobs, busy, message, start, skipPolish } = useJobTracker(projectId, {
    onFinished: handleFinished,
    onEvent: handleEvent,
  });

  const paperType = project?.paper_type ?? 'review';
  // token 随 reload 递增，任务结束后进度随之重算。
  const progress = useProjectProgress(projectId, paperType, project?.library_count ?? 0, token);

  const value = React.useMemo<ProjectContextValue>(
    () => ({
      projectId,
      project,
      paperType,
      whitelist,
      loading,
      error,
      source,
      note,
      reload,
      progress,
      tracked,
      jobs,
      busy,
      jobMessage: message,
      startJob: start,
      skipPolish,
      onJobFinished,
      onJobEvent,
    }),
    [
      projectId,
      project,
      paperType,
      whitelist,
      loading,
      error,
      source,
      note,
      reload,
      progress,
      tracked,
      jobs,
      busy,
      message,
      start,
      skipPolish,
      onJobFinished,
      onJobEvent,
    ],
  );

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}

export function useProject(): ProjectContextValue {
  const ctx = React.useContext(ProjectContext);
  if (!ctx) throw new Error('useProject 必须在 <ProjectProvider> 内使用');
  return ctx;
}

/**
 * 在任务结束后重新拉取本页数据。
 *
 * 取代各工作台自建 useJobTracker 的写法——那样每切一次页面就重开一次 EventSource，
 * 计时归零、已收集的降级警告全部丢失。
 */
export function useJobFinished(handler: (job: Job) => void): void {
  const { onJobFinished } = useProject();
  const ref = React.useRef(handler);
  React.useEffect(() => {
    ref.current = handler;
  });
  React.useEffect(() => onJobFinished((job) => ref.current(job)), [onJobFinished]);
}

/** 订阅具体的任务事件（导出中心要从 `render.completed` 的负载里读编译结果）。 */
export function useJobEvent(handler: (event: JobEvent) => void): void {
  const { onJobEvent } = useProject();
  const ref = React.useRef(handler);
  React.useEffect(() => {
    ref.current = handler;
  });
  React.useEffect(() => onJobEvent((event) => ref.current(event)), [onJobEvent]);
}
