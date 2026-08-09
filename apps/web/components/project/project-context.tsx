'use client';

import * as React from 'react';
import {
  generateCards,
  generateOutline,
  generateQuality,
  generateSections,
  getProject,
  getWhitelist,
  listJobs,
  resumeJob,
  startExport,
  startIngest,
  startSearch,
  startSnowball,
} from '@/lib/api';
import { describeError } from '@/lib/errors';
import { useJobTracker, type TrackedJob } from '@/lib/useJobTracker';
import { useProjectProgress, type ProjectProgress } from '@/lib/useProjectProgress';
import type { DataSource, Job, JobEvent, PaperType, Project } from '@/lib/types';
import type { RetryableStage } from '@/lib/pipeline';

interface ProjectContextValue {
  projectId: string;
  project: Project | undefined;
  paperType: PaperType;
  /** R1 写作白名单。library/outline/write 三处此前各拉一遍，这里统一供给。 */
  whitelist: string[];
  /** 白名单是软依赖；失败时不能把空数组冒充成“已加载且为空”。 */
  whitelistStatus: 'idle' | 'loading' | 'ready' | 'error';
  whitelistError: string | null;
  reloadWhitelist: () => void;
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
  /** 单独重跑有独立端点的管线阶段；运行中与终态任务共用这一入口。 */
  retryStage: (stage: RetryableStage) => Promise<void>;
  /** 跳过剩余的连贯性润色：已润色的保留，剩下的直接交付初稿。 */
  skipPolish: (jobId: string) => Promise<void>;
  /** 取消任务：当前这一步跑完即停，已产出内容保留。 */
  cancelJob: (jobId: string) => Promise<void>;
  /** 暂停任务：停在最近的安全点，可从断点继续。 */
  pauseJob: (jobId: string) => Promise<void>;
  /**
   * 最近一条处于 paused 的任务。
   *
   * 暂停后任务会离开 `tracked`（paused 已进终态集，进度流随之关闭），
   * 「继续」入口因此必须另有来源，否则暂停完就再也找不到它了。
   */
  pausedJob: Job | null;
  /** 从断点继续：新建一个 job，已完成的阶段会被跳过。 */
  resumePausedJob: () => Promise<void>;
  /** 任务结束后想额外刷新自身数据的页面在此登记。 */
  onJobFinished: (handler: (job: Job) => void) => () => void;
  /** 需要读具体事件负载的页面（如导出中心的 render.completed）在此登记。 */
  onJobEvent: (handler: (event: JobEvent) => void) => () => void;
}

const ProjectContext = React.createContext<ProjectContextValue | null>(null);

type ProjectDataContextValue = Pick<
  ProjectContextValue,
  | 'projectId'
  | 'project'
  | 'paperType'
  | 'whitelist'
  | 'whitelistStatus'
  | 'whitelistError'
  | 'reloadWhitelist'
  | 'loading'
  | 'error'
  | 'source'
  | 'note'
  | 'reload'
  | 'progress'
>;

type ProjectActionsContextValue = Pick<
  ProjectContextValue,
  | 'busy'
  | 'startJob'
  | 'retryStage'
  | 'skipPolish'
  | 'cancelJob'
  | 'pauseJob'
  | 'pausedJob'
  | 'resumePausedJob'
  | 'onJobFinished'
  | 'onJobEvent'
>;

type ProjectJobStateContextValue = Pick<
  ProjectContextValue,
  'tracked' | 'jobs' | 'jobMessage'
>;

interface ProjectActivityContextValue {
  busy: boolean;
  runningStage?: string;
  runningKind?: Job['kind'];
}

const ProjectDataContext = React.createContext<ProjectDataContextValue | null>(null);
const ProjectActionsContext = React.createContext<ProjectActionsContextValue | null>(null);
const ProjectJobStateContext = React.createContext<ProjectJobStateContextValue | null>(null);
const ProjectActivityContext = React.createContext<ProjectActivityContextValue | null>(null);

export function ProjectProvider({
  projectId,
  children,
}: {
  projectId: string;
  children: React.ReactNode;
}) {
  const [project, setProject] = React.useState<Project | undefined>();
  const [whitelist, setWhitelist] = React.useState<string[]>([]);
  const [whitelistStatus, setWhitelistStatus] = React.useState<ProjectContextValue['whitelistStatus']>('idle');
  const [whitelistError, setWhitelistError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [source, setSource] = React.useState<DataSource>('live');
  const [note, setNote] = React.useState<string | undefined>();
  const [token, setToken] = React.useState(0);
  const [whitelistToken, setWhitelistToken] = React.useState(0);

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
  const reloadWhitelist = React.useCallback(() => setWhitelistToken((t) => t + 1), []);

  React.useEffect(() => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setError(null);
    setLoading(true);
    getProject(projectId, controller.signal)
      .then((proj) => {
        if (controller.signal.aborted) return;
        setProject(proj.data);
        setSource(proj.source);
        setNote(proj.note);
        setLoading(false);
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setError(describeError(err));
        setLoading(false);
      });
    return () => {
      controller.abort();
    };
  }, [projectId, token]);

  React.useEffect(() => {
    if (!projectId) {
      setWhitelist([]);
      setWhitelistStatus('idle');
      return;
    }
    const controller = new AbortController();
    setWhitelistStatus('loading');
    setWhitelistError(null);
    getWhitelist(projectId, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;
        setWhitelist(result.data);
        setWhitelistStatus('ready');
        if (result.source === 'mock') {
          setSource('mock');
          setNote(result.note);
        }
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setWhitelistStatus('error');
        setWhitelistError(describeError(err));
      });
    return () => controller.abort();
  }, [projectId, token, whitelistToken]);

  const handleFinished = React.useCallback(
    (job: Job) => {
      // 项目自身的计数（library_count / section_count）也会变，一并刷新。
      reload();
      finishHandlers.current.forEach((fn) => fn(job));
    },
    [reload],
  );

  const { tracked, jobs, busy, message, start, skipPolish, cancel, pause } = useJobTracker(
    projectId,
    { onFinished: handleFinished, onEvent: handleEvent },
  );

  const retryStage = React.useCallback(
    async (stage: RetryableStage) => {
      const starters: Record<RetryableStage, () => Promise<{ data: Job | undefined }>> = {
        search: () => startSearch(projectId, {}),
        ingest: () => startIngest(projectId),
        snowball: () => startSnowball(projectId, 'both'),
        cards: () => generateCards(projectId),
        quality: () => generateQuality(projectId),
        outline: () => generateOutline(projectId),
        write: () => generateSections(projectId, true),
        render: () => startExport(projectId),
      };
      const started = await starters[stage]();
      start(started.data, '后端不可用：无法重跑该阶段');
    },
    [projectId, start],
  );

  // 暂停的任务不在 tracked 里（paused 是终态，进度流已关闭），只能自己拉一遍任务列表找。
  //
  // 依赖里放的是**追踪中任务的 id**而不是 `tracked` 对象：后者每来一条 SSE 事件就换一个
  // 引用，直接依赖它等于每个进度事件都发一次 listJobs。id 变成 undefined 的那一刻
  // （任务离开追踪列表 = 刚刚收尾）正是需要重查的时机。
  const trackedJobId = tracked?.job.id;
  const [pausedJob, setPausedJob] = React.useState<Job | null>(null);
  React.useEffect(() => {
    if (!projectId) return;
    let alive = true;
    void listJobs(projectId)
      .then(({ data }) => {
        if (!alive) return;
        // 只认最近的一条：同一个项目连着暂停两次，用户要继续的是最后那次。
        setPausedJob(data.find((job) => job.status === 'paused') ?? null);
      })
      .catch(() => {
        /* 拉不到就不显示「继续」，不影响其它功能 */
      });
    return () => {
      alive = false;
    };
  }, [projectId, token, trackedJobId]);

  const resumePausedJob = React.useCallback(async () => {
    if (!pausedJob) return;
    const resumed = await resumeJob(projectId, pausedJob.id);
    setPausedJob(null);
    start(resumed, '后端不可用：无法继续该任务');
  }, [pausedJob, projectId, start]);

  const paperType = project?.paper_type ?? 'review';
  // token 随 reload 递增，任务结束后进度随之重算。
  const progress = useProjectProgress(projectId, paperType, project?.library_count ?? 0, token);

  const value = React.useMemo<ProjectContextValue>(
    () => ({
      projectId,
      project,
      paperType,
      whitelist,
      whitelistStatus,
      whitelistError,
      reloadWhitelist,
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
      retryStage,
      skipPolish,
      cancelJob: cancel,
      pauseJob: pause,
      pausedJob,
      resumePausedJob,
      onJobFinished,
      onJobEvent,
    }),
    [
      projectId,
      project,
      paperType,
      whitelist,
      whitelistStatus,
      whitelistError,
      reloadWhitelist,
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
      retryStage,
      skipPolish,
      cancel,
      pause,
      pausedJob,
      resumePausedJob,
      onJobFinished,
      onJobEvent,
    ],
  );

  const dataValue = React.useMemo<ProjectDataContextValue>(
    () => ({
      projectId,
      project,
      paperType,
      whitelist,
      whitelistStatus,
      whitelistError,
      reloadWhitelist,
      loading,
      error,
      source,
      note,
      reload,
      progress,
    }),
    [
      projectId,
      project,
      paperType,
      whitelist,
      whitelistStatus,
      whitelistError,
      reloadWhitelist,
      loading,
      error,
      source,
      note,
      reload,
      progress,
    ],
  );
  const actionsValue = React.useMemo<ProjectActionsContextValue>(
    () => ({
      busy,
      startJob: start,
      retryStage,
      skipPolish,
      cancelJob: cancel,
      pauseJob: pause,
      pausedJob,
      resumePausedJob,
      onJobFinished,
      onJobEvent,
    }),
    [
      busy,
      start,
      retryStage,
      skipPolish,
      cancel,
      pause,
      pausedJob,
      resumePausedJob,
      onJobFinished,
      onJobEvent,
    ],
  );
  const jobStateValue = React.useMemo<ProjectJobStateContextValue>(
    () => ({ tracked, jobs, jobMessage: message }),
    [tracked, jobs, message],
  );
  // 只暴露页面真正关心的原始值；百分比、详情和计时变化不会广播到工作台。
  const activityValue = React.useMemo<ProjectActivityContextValue>(
    () => ({
      busy,
      runningStage: tracked?.job.stage ?? undefined,
      runningKind: tracked?.job.kind,
    }),
    [busy, tracked?.job.stage, tracked?.job.kind],
  );

  return (
    <ProjectDataContext.Provider value={dataValue}>
      <ProjectActionsContext.Provider value={actionsValue}>
        <ProjectActivityContext.Provider value={activityValue}>
          <ProjectJobStateContext.Provider value={jobStateValue}>
            <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>
          </ProjectJobStateContext.Provider>
        </ProjectActivityContext.Provider>
      </ProjectActionsContext.Provider>
    </ProjectDataContext.Provider>
  );
}

export function useProject(): ProjectContextValue {
  const ctx = React.useContext(ProjectContext);
  if (!ctx) throw new Error('useProject 必须在 <ProjectProvider> 内使用');
  return ctx;
}

/** 高频页面优先使用细粒度 hooks，避免任务百分比变化重绘整个工作台。 */
export function useProjectData(): ProjectDataContextValue {
  const ctx = React.useContext(ProjectDataContext);
  if (!ctx) throw new Error('useProjectData 必须在 <ProjectProvider> 内使用');
  return ctx;
}

export function useProjectActions(): ProjectActionsContextValue {
  const ctx = React.useContext(ProjectActionsContext);
  if (!ctx) throw new Error('useProjectActions 必须在 <ProjectProvider> 内使用');
  return ctx;
}

export function useProjectJobState(): ProjectJobStateContextValue {
  const ctx = React.useContext(ProjectJobStateContext);
  if (!ctx) throw new Error('useProjectJobState 必须在 <ProjectProvider> 内使用');
  return ctx;
}

export function useProjectActivity(): ProjectActivityContextValue {
  const ctx = React.useContext(ProjectActivityContext);
  if (!ctx) throw new Error('useProjectActivity 必须在 <ProjectProvider> 内使用');
  return ctx;
}

/**
 * 在任务结束后重新拉取本页数据。
 *
 * 取代各工作台自建 useJobTracker 的写法——那样每切一次页面就重开一次 EventSource，
 * 计时归零、已收集的降级警告全部丢失。
 */
export function useJobFinished(handler: (job: Job) => void): void {
  const { onJobFinished } = useProjectActions();
  const ref = React.useRef(handler);
  React.useEffect(() => {
    ref.current = handler;
  });
  React.useEffect(() => onJobFinished((job) => ref.current(job)), [onJobFinished]);
}

/** 订阅具体的任务事件（导出中心要从 `render.completed` 的负载里读编译结果）。 */
export function useJobEvent(handler: (event: JobEvent) => void): void {
  const { onJobEvent } = useProjectActions();
  const ref = React.useRef(handler);
  React.useEffect(() => {
    ref.current = handler;
  });
  React.useEffect(() => onJobEvent((event) => ref.current(event)), [onJobEvent]);
}
