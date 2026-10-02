import * as React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';

const mock = vi.hoisted(() => ({ list: vi.fn(), decide: vi.fn(), toast: vi.fn(), pause: vi.fn() }));
vi.mock('@/lib/api', () => ({
  listDraftRevisions: (...args: unknown[]) => mock.list(...args),
  decideDraftRevision: (...args: unknown[]) => mock.decide(...args),
  exportDownloadUrl: () => '/download', exportPreviewUrl: () => '/preview',
  pauseJob: (...args: unknown[]) => mock.pause(...args), resumeJob: vi.fn(), cancelJob: vi.fn(),
}));
vi.mock('@/components/ui/toast', () => ({ useToast: () => ({ toast: mock.toast }) }));
import { RevisionPanel } from '@/components/export/revision-panel';

beforeEach(() => { vi.clearAllMocks(); });
it('reports unresolved findings and adoption conflicts without hiding the candidate', async () => {
  mock.list.mockResolvedValue({ data: [{ id: 'r1', job_id: 'j1', job_status: 'succeeded',
    status: 'needs_review', review: { sections_reviewed: 12, unassessed: ['s1'], sections_failing: ['s2'] },
    artifacts: [{ id: 'pdf1', format: 'pdf' }] }] });
  mock.decide.mockRejectedValue(new Error('正文已编辑'));
  const accepted = vi.fn();
  render(<RevisionPanel projectId="p1" onAccepted={accepted} />);
  expect(await screen.findByText(/仍有待核验或修复的问题/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '采纳为新版本' }));
  await waitFor(() => expect(mock.toast).toHaveBeenCalled());
  expect(accepted).not.toHaveBeenCalled();
  expect(screen.getByRole('link', { name: '下载修订 PDF' })).toHaveAttribute('href', '/download');
});
it('offers pause while review is running and does not offer adoption', async () => {
  mock.list.mockResolvedValue({ data: [{ id: 'r1', job_id: 'j1', job_status: 'running', status: 'running', artifacts: [] }] });
  mock.pause.mockResolvedValue({});
  render(<RevisionPanel projectId="p1" onAccepted={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: '暂停后台核验' }));
  await waitFor(() => expect(mock.pause).toHaveBeenCalledWith('p1', 'j1'));
  expect(screen.queryByRole('button', { name: '采纳为新版本' })).not.toBeInTheDocument();
});

it('shows partial claim coverage and uncertainty while review continues', async () => {
  mock.list.mockResolvedValue({ data: [{ id: 'r1', job_id: 'j1', job_status: 'running',
    status: 'running', review: { sections_reviewed: 2, claim_count: 20,
      checked_count: 12, uncertain_count: 3, unassessed_count: 8 }, artifacts: [] }] });
  render(<RevisionPanel projectId="p1" onAccepted={vi.fn()} />);
  expect(await screen.findByText('论断核验 12/20；不确定 3；未评估 8')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '采纳为新版本' })).not.toBeInTheDocument();
});
