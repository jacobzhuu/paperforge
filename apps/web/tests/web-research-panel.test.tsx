import * as React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  projectId: 'project-a', project: { web_research_enabled: false }, busy: false,
  reload: vi.fn(), startJob: vi.fn(),
}));
const api = vi.hoisted(() => ({
  listWebResearch: vi.fn(), getWebResearch: vi.fn(), refreshWebResearch: vi.fn(),
  updateProject: vi.fn(),
}));
vi.mock('@/components/project/project-context', () => ({
  useProjectData: () => state, useProjectActions: () => state, useJobFinished: () => {},
}));
vi.mock('@/lib/api', () => api);
import { WebResearchPanel } from '@/components/library/web-research-panel';

beforeEach(() => {
  vi.clearAllMocks();
  state.project.web_research_enabled = false;
  state.busy = false;
  api.listWebResearch.mockResolvedValue({ available: true, runs: [] });
  api.updateProject.mockResolvedValue({});
  state.reload.mockResolvedValue(undefined);
});

it('explains disclosure and requires project opt-in before refresh', async () => {
  render(<WebResearchPanel />);
  expect(screen.getByText(/向 Exa 发送/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '刷新网页资料' })).toBeDisabled();
  await userEvent.click(screen.getByRole('checkbox'));
  await waitFor(() => expect(api.updateProject).toHaveBeenCalledWith('project-a', {
    web_research_enabled: true,
  }));
  expect(state.reload).toHaveBeenCalled();
});

it('refreshes through project job control and displays conflict errors', async () => {
  state.project.web_research_enabled = true;
  api.refreshWebResearch.mockRejectedValue(new Error('project already has an active job'));
  render(<WebResearchPanel />);
  await userEvent.click(screen.getByRole('button', { name: '刷新网页资料' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('project already has an active job');
  expect(state.startJob).not.toHaveBeenCalled();
});

it('renders untrusted web text without interpreting HTML and shows partial results', async () => {
  const run = { id: 'run-a', status: 'partial', calls: 2, error: 'tool_timeout',
    created_at: '2026-09-17T00:00:00Z' };
  api.listWebResearch.mockResolvedValue({ available: true, runs: [run] });
  api.getWebResearch.mockResolvedValue({ ...run, sources: [{ id: 'source-a',
    url: 'https://example.org/', title: 'Official docs',
    snippet: '<img src=x onerror=alert(1)>', body: 'documentation', status: 'read',
    fetched_at: '2026-09-17T00:00:01Z', verification: null,
  }] });
  const { container } = render(<WebResearchPanel />);
  expect(await screen.findByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
  expect(container.querySelector('img')).toBeNull();
  expect(screen.getByText(/费用未计价/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Official docs' })).toHaveAttribute(
    'rel', 'noopener noreferrer',
  );
});
