'use client';

import * as React from 'react';
import { listJobs, subscribeJobEvents } from './api';
import { stageLabel } from './labels';
import type { Job, JobEvent } from './types';

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
}

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

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
    case 'write.section':
      return str(p, 'title');
    case 'write.coherence': {
      // 连贯性润色是 write 阶段最后几分钟，此前没有任何事件，界面像卡住了。
      const done = num(p, 'done');
      const total = num(p, 'total');
      return done !== undefined && total !== undefined ? `连贯性润色 ${done}/${total}` : '连贯性润色';
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

/**
 * 任务进度订阅。四个工作台共用一份：
 *  - 在 effect 里订阅并在 cleanup 中断开（此前四处都丢弃了取消函数，EventSource 会泄漏）；
 *  - 收集 `*.failed` 与 `job.finished` 的 warnings —— 否则「scope/ingest/cards 全挂」
 *    的一次运行和干净运行长得一模一样；
 *  - 挂载时用 listJobs 恢复仍在跑的任务，刷新页面不再丢进度。
 */
export function useJobTracker(
  projectId: string,
  options: { onFinished?: () => void; onEvent?: (event: JobEvent) => void } = {},
) {
  const { onFinished, onEvent } = options;
  const [tracked, setTracked] = React.useState<TrackedJob | null>(null);
  const [message, setMessage] = React.useState<string | null>(null);

  // 回调放进 ref，避免调用方每次渲染新建函数导致重新订阅（会重开 EventSource）。
  const onFinishedRef = React.useRef(onFinished);
  const onEventRef = React.useRef(onEvent);
  React.useEffect(() => {
    onFinishedRef.current = onFinished;
    onEventRef.current = onEvent;
  });

  const activeId = tracked?.job.id ?? null;

  /** 挂载时恢复进行中的任务。 */
  React.useEffect(() => {
    if (!projectId) return;
    let alive = true;
    void listJobs(projectId)
      .then(({ data }) => {
        if (!alive) return;
        const running = data.find((j) => !TERMINAL.has(j.status));
        if (running) {
          setTracked((prev) =>
            prev
              ? prev
              : {
                  job: running,
                  label: stageLabel(running.stage),
                  warnings: [],
                  startedAt: Date.parse(running.created_at ?? '') || Date.now(),
                  reconnecting: false,
                  degradedToPolling: false,
                },
          );
        }
      })
      .catch(() => {
        /* 恢复失败不影响后续手动触发 */
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  /** 订阅当前任务；activeId 变化或卸载时断开。 */
  React.useEffect(() => {
    if (!projectId || !activeId) return;
    const stop = subscribeJobEvents(projectId, activeId, {
      onEvent: (event) => {
        onEventRef.current?.(event);
        setTracked((prev) => {
          if (!prev) return prev;
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
          };
        });
      },
      onConnectionChange: ({ reconnecting, degradedToPolling }) => {
        setTracked((prev) => (prev ? { ...prev, reconnecting, degradedToPolling } : prev));
      },
      onClose: () => {
        setTracked(null);
        onFinishedRef.current?.();
      },
    });
    return stop;
  }, [projectId, activeId]);

  /** 触发一个任务：started 为空说明后端不可用，给出明确文案而不是静默。 */
  const start = React.useCallback((started: Job | undefined, fallbackMessage: string) => {
    if (!started) {
      setMessage(fallbackMessage);
      return;
    }
    setMessage(null);
    setTracked({
      job: started,
      label: stageLabel(started.stage),
      warnings: [],
      startedAt: Date.now(),
      reconnecting: false,
      degradedToPolling: false,
    });
  }, []);

  return {
    tracked,
    /** 有任务在跑（用于禁用触发按钮）。 */
    busy: tracked !== null,
    message,
    setMessage,
    start,
  };
}
