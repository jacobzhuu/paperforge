import * as React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EMPTY_PROGRESS } from '@/lib/useProjectProgress';

const api = vi.hoisted(() => ({
  getProject: vi.fn(),
  getWhitelist: vi.fn(),
  listJobs: vi.fn(),
}));

vi.mock('@/lib/api', () => ({
  getProject: (...args: unknown[]) => api.getProject(...args),
  getWhitelist: (...args: unknown[]) => api.getWhitelist(...args),
  listJobs: (...args: unknown[]) => api.listJobs(...args),
  generateCards: vi.fn(),
  generateOutline: vi.fn(),
  generateQuality: vi.fn(),
  generateSections: vi.fn(),
  resumeJob: vi.fn(),
  startExport: vi.fn(),
  startIngest: vi.fn(),
  startSearch: vi.fn(),
  startSnowball: vi.fn(),
}));

vi.mock('@/lib/useJobTracker', () => ({
  useJobTracker: () => ({
    tracked: null,
    jobs: {},
    busy: false,
    message: null,
    start: vi.fn(),
    skipPolish: vi.fn(),
    cancel: vi.fn(),
    pause: vi.fn(),
  }),
}));

vi.mock('@/lib/useProjectProgress', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/useProjectProgress')>();
  return { ...actual, useProjectProgress: () => ({ ...EMPTY_PROGRESS, loading: false }) };
});

import { ProjectProvider, useProject } from '@/components/project/project-context';

function Probe() {
  const context = useProject();
  return (
    <div>
      <p>{context.project?.title}</p>
      <p>白名单：{context.whitelistStatus}</p>
      {context.whitelistError && <p>{context.whitelistError}</p>}
      <button type="button" onClick={context.reloadWhitelist}>重试白名单</button>
    </div>
  );
}

describe('ProjectContext 软依赖隔离', () => {
  beforeEach(() => {
    api.getProject.mockResolvedValue({
      data: { id: 'p1', title: '仍可工作的项目', paper_type: 'review' },
      source: 'live',
    });
    api.getWhitelist.mockReset();
    api.listJobs.mockResolvedValue({ data: [], source: 'live' });
  });

  it('白名单失败时项目子页面仍渲染，并可独立重试', async () => {
    api.getWhitelist
      .mockRejectedValueOnce(new Error('whitelist 500'))
      .mockResolvedValueOnce({ data: ['smith2025'], source: 'live' });
    render(<ProjectProvider projectId="p1"><Probe /></ProjectProvider>);

    expect(await screen.findByText('仍可工作的项目')).toBeInTheDocument();
    expect(await screen.findByText('白名单：error')).toBeInTheDocument();
    expect(screen.getByText(/whitelist 500/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '重试白名单' }));
    await waitFor(() => expect(screen.getByText('白名单：ready')).toBeInTheDocument());
    expect(api.getProject).toHaveBeenCalledTimes(1);
    expect(api.getWhitelist).toHaveBeenCalledTimes(2);
  });
});
