import * as React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { makeProject } from './helpers';

const updateProject = vi.fn();
vi.mock('@/lib/api', () => ({
  getSubmissionReadiness: () => Promise.resolve({ data: undefined }),
  updateProject: (...args: unknown[]) => updateProject(...args),
}));
vi.mock('@/components/ui/toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock('@/components/project/project-title', () => ({
  ProjectTitle: ({ title }: { title: string }) => <h1>{title}</h1>,
}));

import { ProjectHeader } from '@/components/project/project-header';

it('shows the saved project profile and patches it for future tasks', async () => {
  updateProject.mockReset().mockResolvedValue({ data: {} });
  const reload = vi.fn();
  const project = makeProject({ execution_profile: 'standard' });
  const { rerender } = render(<ProjectHeader project={project} onProfileChanged={reload} />);
  expect(screen.queryByRole('button', { name: '快速草稿' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '执行设置' }));
  const toggle = screen.getByRole('button', { name: '快速草稿' });
  expect(toggle).toHaveAttribute('aria-pressed', 'false');
  fireEvent.click(toggle);
  await waitFor(() => expect(updateProject).toHaveBeenCalledWith(project.id, {
    execution_profile: 'fast_draft',
  }));
  await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  rerender(<ProjectHeader project={{ ...project, execution_profile: 'fast_draft' }} />);
  expect(toggle).toHaveAttribute('aria-pressed', 'true');
});

it('distinguishes the active task snapshot from the saved setting', async () => {
  const project = makeProject({ execution_profile: 'fast_draft' });
  render(<ProjectHeader project={project} activeJob={{
    id: 'running-task', project_id: project.id, kind: 'full', status: 'running', progress: 0.3,
    checkpoint: { execution_profile: 'standard' },
  }} />);
  await waitFor(() => expect(screen.getByText('执行中 · 标准生成')).toBeInTheDocument());
  expect(screen.getByLabelText('执行设置与状态')).toHaveTextContent('快速草稿');
  expect(screen.queryByRole('button', { name: '快速草稿' })).not.toBeInTheDocument();
});
