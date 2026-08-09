import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import type { JobStreamHandlers } from '@/lib/api';
import type { Job } from '@/lib/types';
import { ok } from './helpers';

let streamHandlers: JobStreamHandlers | undefined;
const subscribeJobEvents = vi.fn((_projectId: string, _jobId: string, handlers: JobStreamHandlers) => {
  streamHandlers = handlers;
  return vi.fn();
});

vi.mock('@/lib/api', () => ({
  listJobs: vi.fn(() => ok([])),
  subscribeJobEvents: (...args: Parameters<typeof subscribeJobEvents>) =>
    subscribeJobEvents(...args),
  skipPolish: vi.fn(),
  cancelJob: vi.fn(),
  pauseJob: vi.fn(),
}));

import { useJobTracker } from '@/lib/useJobTracker';

const job = {
  id: 'quality-job',
  project_id: 'p1',
  kind: 'write',
  status: 'queued',
  progress: 0,
  stage: 'quality',
  checkpoint: {},
  created_at: '2026-08-02T00:00:00Z',
} as Job;

beforeEach(() => {
  streamHandlers = undefined;
  subscribeJobEvents.mockClear();
});

it('任务流关闭后必定调用 onFinished，让质量报告重新拉取', async () => {
  const onFinished = vi.fn();
  const { result } = renderHook(() => useJobTracker('p1', { onFinished }));

  act(() => {
    result.current.start(job, '启动失败');
  });
  await waitFor(() => expect(streamHandlers?.onClose).toBeTypeOf('function'));

  act(() => {
    streamHandlers?.onClose?.();
  });

  expect(onFinished).toHaveBeenCalledWith(expect.objectContaining({ id: 'quality-job' }));
  await waitFor(() => expect(result.current.busy).toBe(false));
});
