'use client';

import * as React from 'react';
import {
  cancelJob as apiCancelJob,
  listJobs,
  pauseJob as apiPauseJob,
  skipPolish as apiSkipPolish,
  subscribeJobEvents,
} from './api';
import { stageLabel } from './labels';
import type { Job, JobEvent, JobKind } from './types';

/** 一次运行中被降级的阶段。draft-first 下阶段失败不阻断，但用户必须看得见。 */
export interface JobWarning {
  stage: string;
  reason: string;
  message?: string;
  count?: number;
}

export interface TrackedJob {
  job: Job;
  /** 已翻成中文的当前阶段，附带 cards.progress / write.section 之类的细节。 */
  label: string;
  detail?: string;
  warnings: JobWarning[];
  startedAt: number;
  reconnecting: boolean;
  degradedToPolling: boolean;
  /** 用户已请求跳过润色（或后端已确认跳过）：按钮据此变成不可再点。 */
  polishSkipRequested: boolean;
  /**
   * 用户已请求停止（'cancel' | 'pause'）。协作式停止要等当前这一步跑完，
   * 中间可能还有一两分钟，按钮不能在这期间看着像没反应。
   */
  stopRequested: 'cancel' | 'pause' | null;
}

// paused 也算终态：这一轮停了，进度条要收；「继续」会另起一个 job。
const TERMINAL = new Set(['paused', 'succeeded', 'failed', 'cancelled', 'needs_input']);

/**
 * 卡片级任务（视觉生成）与项目级任务（全文管线）的区分。
 *
 * 视觉任务是**每张图一个**的短任务，全文管线是每项目一个的长任务。此前两者
 * 共用一个 `tracked` 槽位：点一次生图就把正在跑的全文任务从进度条上挤掉，
 * 而全文任务其实还在服务端跑着。
 */
function isCardScoped(kind: JobKind): boolean {
  return kind === 'visual';
}

function str(payload: Record<string, unknown>, key: string): string | undefined {
  const value = payload[key];
  return typeof value === 'string' ? value : undefined;
}

function num(payload: Record<string, unknown>, key: string): number | undefined {
  const value = payload[key];
  return typeof value === 'number' ? value : undefined;
}

/**
 * 按 阶段+原因 去重。
 * 同一个降级会被报两次：先是 `{stage}.failed`，收尾时 job.finished 又汇总一遍；
 * 断线重连重放事件时还会再来一轮。
 */
function dedupe(warnings: JobWarning[]): JobWarning[] {
  const seen = new Map<string, JobWarning>();
  for (const w of warnings) {
    const key = `${w.stage}::${w.reason}`;
    // 后到的通常带更全的信息（count 来自收尾汇总），合并而不是丢弃。
    const prev = seen.get(key);
    seen.set(key, prev ? { ...prev, ...w, message: w.message ?? prev.message } : w);
  }
  return Array.from(seen.values());
}

/** 从一条事件里提炼「现在到底在干什么」的细节文案。 */
function detailOf(event: JobEvent): string | undefined {
  const p = event.payload ?? {};
  switch (event.type) {
    case 'cards.progress': {
      const done = num(p, 'done');
      const total = num(p, 'total');
      return done !== undefined && total !== undefined ? `${done}/${total}` : undefined;
    }
    case 'write.section': {
      // 只给标题不够：用户还要知道「还剩几节」，否则十几分钟里无从判断进展。
      const title = str(p, 'title');
      const index = num(p, 'index');
      const total = num(p, 'total');
      if (index !== undefined && total !== undefined) {
        return `已写完 ${index}/${total} 节${title ? ` · ${title}` : ''}`;
      }
      return title;
    }
    case 'polish.started': {
      const total = num(p, 'total');
      return total !== undefined ? `共 ${total} 节待润色` : undefined;
    }
    case 'polish.section': {
      // 润色阶段所有章节都已「已生成」，不报进度就会被当成卡死。
      const title = str(p, 'title');
      const done = num(p, 'done');
      const total = num(p, 'total');
      if (done !== undefined && total !== undefined) {
        return `已润色 ${done}/${total} 节${title ? ` · ${title}` : ''}`;
      }
      return title;
    }
    case 'polish.skipped': {
      const remaining = num(p, 'remaining');
      return remaining !== undefined ? `已跳过剩余 ${remaining} 节，直接交付初稿` : '已跳过润色';
    }
    case 'polish.completed': {
      const done = num(p, 'done');
      const total = num(p, 'total');
      return done !== undefined && total !== undefined ? `润色完成 ${done}/${total}` : undefined;
    }
    case 'search.deduped': {
      const kept = num(p, 'kept') ?? num(p, 'count');
      return kept !== undefined ? `去重后 ${kept} 篇` : undefined;
    }
    case 'import.verified':
      return str(p, 'title') ?? '已核验 1 条';
    case 'import.rejected':
      return `未通过核验：${str(p, 'reason') ?? '未知原因'}`;
    case 'ingest.fulltext':
      return str(p, 'title');
    default:
      return undefined;
  }
}

/** 把一条事件并进已追踪的任务状态。 */
function applyEvent(prev: TrackedJob, event: JobEvent): TrackedJob {
  let warnings = prev.warnings;
  if (event.type.endsWith('.failed')) {
    // 阶段失败事件：{error, message}（worker.py::_run_stage）
    warnings = dedupe([
      ...warnings,
      {
        stage: event.type.replace(/\.failed$/, ''),
        reason: str(event.payload ?? {}, 'error') ?? '阶段失败',
        message: str(event.payload ?? {}, 'message'),
      },
    ]);
  }
  if (event.type === 'job.interrupted') {
    // 任务被掐断（arq 超时 / worker 重启）：已落库的章节还在，但这轮没跑完。
    warnings = dedupe([
      ...warnings,
      {
        stage: 'job',
        reason: '任务被中断',
        message: `${str(event.payload ?? {}, 'reason') ?? '未知原因'}：已生成的内容已保留，可重新触发继续`,
      },
    ]);
  }
  if (event.type === 'job.finished') {
    // 收尾汇总：{stage, reason, count}（worker.py::_finish 的 context.warnings）
    const raw = (event.payload ?? {}).warnings;
    if (Array.isArray(raw)) {
      warnings = dedupe([
        ...warnings,
        ...(raw as Record<string, unknown>[]).map((w) => ({
          stage: str(w, 'stage') ?? '未知阶段',
          reason: str(w, 'reason') ?? str(w, 'error') ?? '降级',
          message: str(w, 'message'),
          count: num(w, 'count'),
        })),
      ]);
    }
  }
  const detail = detailOf(event);
  return {
    ...prev,
    job: {
      ...prev.job,
      progress: event.progress ?? prev.job.progress,
      status: event.status ?? prev.job.status,
      stage: event.stage ?? prev.job.stage,
    },
    label: stageLabel(event.stage ?? prev.job.stage),
    detail: detail ?? (event.stage !== prev.job.stage ? undefined : prev.detail),
    warnings,
    reconnecting: false,
    polishSkipRequested: prev.polishSkipRequested || event.type === 'polish.skipped',
    // 后端确认停止后按钮保持禁用态直到任务离开追踪列表，避免「已请求」闪回可点。
    stopRequested:
      event.type === 'job.cancelled'
        ? 'cancel'
        : event.type === 'job.paused'
          ? 'pause'
          : prev.stopRequested,
  };
}

function newTracked(job: Job): TrackedJob {
  return {
    job,
    label: stageLabel(job.stage),
    warnings: [],
    startedAt: Date.parse(job.created_at ?? '') || Date.now(),
    reconnecting: false,
    degradedToPolling: false,
    // 刷新页面后按钮不该「复活」：跳过标记本身就存在 job.checkpoint 上。
    polishSkipRequested: Boolean(job.checkpoint?.[POLISH_SKIP_KEY]),
    stopRequested: readStopMode(job),
  };
}

/** 与 db.repositories.jobs::POLISH_SKIP_KEY 同名。 */
const POLISH_SKIP_KEY = 'polish_skip';
/** 与 db.repositories.jobs::JOB_CONTROL_KEY 同名。 */
const JOB_CONTROL_KEY = 'control';

/** 停止标记同样存在 job.checkpoint 上，刷新页面后按钮不该「复活」。 */
function readStopMode(job: Job): 'cancel' | 'pause' | null {
  const mode = job.checkpoint?.[JOB_CONTROL_KEY];
  if (mode === 'cancel') return 'cancel';
  if (mode === 'pause') return 'pause';
  return null;
}

export interface JobTracker {
  /** 项目级主任务（全文管线）。视觉任务不会占用这个槽位。 */
  tracked: TrackedJob | null;
  /** 全部在跑的任务，按 job id 索引——视觉卡片按自己的 job id 取。 */
  jobs: Record<string, TrackedJob>;
  /** 项目级任务在跑（用于禁用「重新生成全文」这类触发按钮）。 */
  busy: boolean;
  message: string | null;
  setMessage: (value: string | null) => void;
  /**
   * 触发一个任务。返回 job id（后端不可用时返回 null），调用方据此建立
   * `visual_id → job_id` 之类的卡片级映射。
   */
  start: (started: Job | undefined, fallbackMessage: string) => string | null;
  /**
   * 跳过剩余的连贯性润色。正在跑的那一节会写完，之后的章节直接交付初稿。
   * 是否润色本就该由用户说了算——默认开着，但不该只能干等。
   */
  skipPolish: (jobId: string) => Promise<void>;
  /**
   * 取消任务。协作式停止：当前这一步（阶段 / 章节 / 条目）跑完才真的退出，
   * 已产出的内容全部保留。
   */
  cancel: (jobId: string) => Promise<void>;
  /** 暂停任务。停在最近的安全点，之后可从断点继续。 */
  pause: (jobId: string) => Promise<void>;
}

/**
 * 任务进度订阅。四个工作台共用一份：
 *  - 在 effect 里订阅并在 cleanup 中断开（此前四处都丢弃了取消函数，EventSource 会泄漏）；
 *  - 收集 `*.failed` 与 `job.finished` 的 warnings —— 否则「scope/ingest/cards 全挂」
 *    的一次运行和干净运行长得一模一样；
 *  - 挂载时用 listJobs 恢复**所有**仍在跑的任务，刷新页面不再丢进度。
 *
 * 多任务：此前只有一个 `tracked` 槽位，且挂载恢复时取「第一个非终态任务」——
 * 同时存在全文任务与视觉任务时，界面追踪哪一个是不确定的，后启动的会把前一个
 * 挤掉。现在按 job id 并行追踪，项目级任务与卡片级任务互不覆盖。
 */
export function useJobTracker(
  projectId: string,
  options: { onFinished?: (job: Job) => void; onEvent?: (event: JobEvent) => void } = {},
): JobTracker {
  const { onFinished, onEvent } = options;
  const [jobs, setJobs] = React.useState<Record<string, TrackedJob>>({});
  const jobsRef = React.useRef(jobs);
  jobsRef.current = jobs;
  const [message, setMessage] = React.useState<string | null>(null);

  // 回调放进 ref，避免调用方每次渲染新建函数导致重新订阅（会重开 EventSource）。
  const onFinishedRef = React.useRef(onFinished);
  const onEventRef = React.useRef(onEvent);
  React.useEffect(() => {
    onFinishedRef.current = onFinished;
    onEventRef.current = onEvent;
  });

  // 切项目时清空：上一个项目的任务不该出现在新项目的进度条上。
  React.useEffect(() => {
    setJobs({});
  }, [projectId]);

  /** 挂载时恢复所有进行中的任务。 */
  React.useEffect(() => {
    if (!projectId) return;
    let alive = true;
    void listJobs(projectId)
      .then(({ data }) => {
        if (!alive) return;
        const running = data.filter((job) => !TERMINAL.has(job.status));
        if (running.length === 0) return;
        setJobs((prev) => {
          const next = { ...prev };
          for (const job of running) if (!next[job.id]) next[job.id] = newTracked(job);
          return next;
        });
      })
      .catch(() => {
        /* 恢复失败不影响后续手动触发 */
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  /**
   * 每个任务一条订阅。
   *
   * 依赖是**排序后的 id 串**而不是 `jobs` 对象：事件到达会替换 jobs 引用，
   * 若直接依赖对象，每来一条事件就会断开重连一次 EventSource。
   */
  const jobIds = Object.keys(jobs).sort().join(',');
  React.useEffect(() => {
    if (!projectId || !jobIds) return;
    const stops = jobIds.split(',').map((jobId) =>
      subscribeJobEvents(projectId, jobId, {
        onEvent: (event) => {
          onEventRef.current?.(event);
          setJobs((prev) => {
            const current = prev[jobId];
            if (!current) return prev;
            return { ...prev, [jobId]: applyEvent(current, event) };
          });
        },
        onConnectionChange: ({ reconnecting, degradedToPolling }) => {
          setJobs((prev) => {
            const current = prev[jobId];
            if (!current) return prev;
            return { ...prev, [jobId]: { ...current, reconnecting, degradedToPolling } };
          });
        },
        onClose: () => {
          // React does not guarantee that a functional state updater executes
          // before the next statement.  Reading `finished` from inside that
          // updater made onFinished race with rendering; in production the
          // quality job could finish without the writing page ever reloading.
          const finished = jobsRef.current[jobId]?.job;
          setJobs((prev) => {
            if (!prev[jobId]) return prev;
            const next = { ...prev };
            delete next[jobId];
            return next;
          });
          if (finished) onFinishedRef.current?.(finished);
        },
      }),
    );
    return () => stops.forEach((stop) => stop());
  }, [projectId, jobIds]);

  /** 触发一个任务：started 为空说明后端不可用，给出明确文案而不是静默。 */
  const start = React.useCallback((started: Job | undefined, fallbackMessage: string) => {
    if (!started) {
      setMessage(fallbackMessage);
      return null;
    }
    setMessage(null);
    setJobs((prev) => ({ ...prev, [started.id]: newTracked(started) }));
    return started.id;
  }, []);

  const skipPolish = React.useCallback(
    async (jobId: string) => {
      // 先乐观置位：这一节可能还要跑一两分钟，按钮不能在这期间看着像没反应。
      setJobs((prev) => {
        const current = prev[jobId];
        if (!current) return prev;
        return { ...prev, [jobId]: { ...current, polishSkipRequested: true } };
      });
      try {
        await apiSkipPolish(projectId, jobId);
      } catch (error) {
        setJobs((prev) => {
          const current = prev[jobId];
          if (!current) return prev;
          return { ...prev, [jobId]: { ...current, polishSkipRequested: false } };
        });
        setMessage(error instanceof Error ? error.message : '跳过润色失败，请重试');
      }
    },
    [projectId],
  );

  /**
   * 取消 / 暂停共用一份：两者的交互形状完全一样——乐观置位、失败回滚、
   * 真正的退出发生在 worker 的下一个安全点。
   */
  const requestStop = React.useCallback(
    async (jobId: string, mode: 'cancel' | 'pause') => {
      const call = mode === 'cancel' ? apiCancelJob : apiPauseJob;
      const label = mode === 'cancel' ? '取消' : '暂停';
      setJobs((prev) => {
        const current = prev[jobId];
        if (!current) return prev;
        return { ...prev, [jobId]: { ...current, stopRequested: mode } };
      });
      try {
        await call(projectId, jobId);
      } catch (error) {
        setJobs((prev) => {
          const current = prev[jobId];
          if (!current) return prev;
          return { ...prev, [jobId]: { ...current, stopRequested: null } };
        });
        setMessage(error instanceof Error ? error.message : `${label}任务失败，请重试`);
      }
    },
    [projectId],
  );

  const cancel = React.useCallback(
    (jobId: string) => requestStop(jobId, 'cancel'),
    [requestStop],
  );
  const pause = React.useCallback((jobId: string) => requestStop(jobId, 'pause'), [requestStop]);

  // 项目级主任务：视觉任务不占这个槽位，否则点一次生图就把正在跑的全文任务
  // 从进度条上挤掉。同时有多个项目级任务时取最早启动的那个（通常就是唯一的那个）。
  const projectJobs = Object.values(jobs).filter((item) => !isCardScoped(item.job.kind));
  projectJobs.sort((a, b) => a.startedAt - b.startedAt);
  const tracked = projectJobs[0] ?? null;

  return {
    tracked,
    jobs,
    busy: projectJobs.length > 0,
    message,
    setMessage,
    start,
    skipPolish,
    cancel,
    pause,
  };
}
