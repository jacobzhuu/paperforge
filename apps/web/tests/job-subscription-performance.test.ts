import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DEGRADED_JOB_POLL_INTERVAL,
  HEALTHY_JOB_POLL_INTERVAL,
  subscribeJobEvents,
} from '@/lib/api';

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn();

  constructor() {
    FakeEventSource.instances.push(this);
  }
}

function response(status = 'running') {
  return {
    ok: true,
    status: 200,
    json: async () => ({
      id: 'job-1',
      project_id: 'p1',
      kind: 'full',
      stage: 'search',
      progress: 0.2,
      status,
    }),
  } as Response;
}

function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', {
    value: hidden,
    configurable: true,
  });
  document.dispatchEvent(new Event('visibilitychange'));
}

describe('任务订阅的校准轮询', () => {
  const stops: Array<() => void> = [];

  beforeEach(() => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response()));
    Object.defineProperty(document, 'hidden', {
      value: false,
      configurable: true,
    });
  });

  afterEach(() => {
    stops.splice(0).forEach((stop) => stop());
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('SSE 健康时只进行 15 秒低频校准', async () => {
    const stop = subscribeJobEvents('p1', 'job-1', {});
    stops.push(stop);
    FakeEventSource.instances[0].onopen?.();

    await vi.advanceTimersByTimeAsync(HEALTHY_JOB_POLL_INTERVAL - 1);
    expect(fetch).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(fetch).toHaveBeenCalledTimes(1);
    stop();
  });

  it('重连全部失败后才切换为 3 秒轮询', async () => {
    const connection = vi.fn();
    const stop = subscribeJobEvents('p1', 'job-1', { onConnectionChange: connection });
    stops.push(stop);

    for (const delay of [1000, 2000, 4000, 8000]) {
      FakeEventSource.instances.at(-1)?.onerror?.();
      await vi.advanceTimersByTimeAsync(delay);
    }
    FakeEventSource.instances.at(-1)?.onerror?.();

    const callsBeforeDegradedPoll = vi.mocked(fetch).mock.calls.length;
    await vi.advanceTimersByTimeAsync(DEGRADED_JOB_POLL_INTERVAL - 1);
    expect(fetch).toHaveBeenCalledTimes(callsBeforeDegradedPoll);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetch).toHaveBeenCalledTimes(callsBeforeDegradedPoll + 1);
    expect(connection).toHaveBeenLastCalledWith({
      reconnecting: true,
      degradedToPolling: true,
    });
    stop();
  });

  it('页面隐藏时暂停轮询，重新可见后立即校准且清理监听', async () => {
    const stop = subscribeJobEvents('p1', 'job-1', {});
    stops.push(stop);
    FakeEventSource.instances[0].onopen?.();
    setHidden(true);

    await vi.advanceTimersByTimeAsync(HEALTHY_JOB_POLL_INTERVAL * 2);
    expect(fetch).not.toHaveBeenCalled();

    setHidden(false);
    await vi.runAllTicks();
    expect(fetch).toHaveBeenCalledTimes(1);

    stop();
    await vi.advanceTimersByTimeAsync(HEALTHY_JOB_POLL_INTERVAL);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
