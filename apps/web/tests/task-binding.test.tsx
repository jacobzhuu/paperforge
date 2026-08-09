import * as React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ProjectTaskProfile, TaskDefinitionSummary } from '@/lib/types';
import { makeToastSpy, ok } from './helpers';

const toastSpy = makeToastSpy();

vi.mock('@/components/ui/toast', () => ({
  useToast: () => ({ toast: toastSpy.toast }),
}));

const api = {
  getProjectTasks: vi.fn(),
  listTaskDefinitions: vi.fn(),
  updateProjectTasks: vi.fn(),
};

vi.mock('@/lib/api', () => ({
  getProjectTasks: (...args: unknown[]) => api.getProjectTasks(...args),
  listTaskDefinitions: (...args: unknown[]) => api.listTaskDefinitions(...args),
  updateProjectTasks: (...args: unknown[]) => api.updateProjectTasks(...args),
}));

const { TaskBinding } = await import('@/components/scope/task-binding');

const CATALOGUE: TaskDefinitionSummary[] = [
  {
    slug: 'recsys.poisoning_attack',
    domain: 'recsys_attack',
    label: '投毒攻击',
    metric_count: 4,
    dataset_count: 6,
    has_vocabulary: true,
  },
  {
    slug: 'bgc.identification',
    domain: 'bgc',
    label: '基因簇识别',
    metric_count: 10,
    dataset_count: 3,
    has_vocabulary: true,
  },
  {
    slug: 'generic.scholarly',
    domain: 'generic',
    label: '通用学术研究',
    metric_count: 0,
    dataset_count: 0,
    has_vocabulary: false,
  },
];

function profile(overrides: Partial<ProjectTaskProfile> = {}): ProjectTaskProfile {
  return {
    project_id: 'p1',
    source: 'fallback',
    bound: false,
    task_ids: [],
    effective_tasks: CATALOGUE,
    fallback_mode: 'all_tasks',
    fallback_note: '该项目未绑定任务，正在继承全部领域的指标与数据集白名单；绑定后抽取只会使用相关领域的术语。',
    ...overrides,
  };
}

describe('研究领域绑定', () => {
  beforeEach(() => {
    toastSpy.calls.length = 0;
    api.getProjectTasks.mockReset();
    api.listTaskDefinitions.mockReset();
    api.updateProjectTasks.mockReset();
    api.listTaskDefinitions.mockReturnValue(ok(CATALOGUE));
  });

  it('未绑定时明确说出后果，而不是静默继承所有领域', async () => {
    api.getProjectTasks.mockReturnValue(ok(profile()));

    render(<TaskBinding projectId="p1" />);

    expect(await screen.findByText('未绑定')).toBeInTheDocument();
    expect(screen.getByText(/继承全部领域的指标与数据集白名单/)).toBeInTheDocument();
  });

  it('已绑定时显示来源，并且不再显示回退警告', async () => {
    api.getProjectTasks.mockReturnValue(
      ok(
        profile({
          source: 'explicit',
          bound: true,
          task_ids: ['generic.scholarly'],
          effective_tasks: [CATALOGUE[2]],
          fallback_note: null,
        }),
      ),
    );

    render(<TaskBinding projectId="p1" />);

    expect(await screen.findByText('已手动绑定')).toBeInTheDocument();
    expect(screen.queryByText(/继承全部领域/)).not.toBeInTheDocument();
  });

  it('区分「系统自动推断」与「用户手动绑定」', async () => {
    api.getProjectTasks.mockReturnValue(
      ok(profile({ source: 'inferred', bound: true, task_ids: ['bgc.identification'] })),
    );

    render(<TaskBinding projectId="p1" />);

    expect(await screen.findByText('系统自动推断')).toBeInTheDocument();
  });

  it('保存按钮在没有改动时不可点，改动后才可用', async () => {
    api.getProjectTasks.mockReturnValue(ok(profile()));
    render(<TaskBinding projectId="p1" />);

    const save = await screen.findByRole('button', { name: '保存绑定' });
    expect(save).toBeDisabled();

    await userEvent.click(screen.getByRole('checkbox', { name: /基因簇识别/ }));
    await waitFor(() => expect(save).toBeEnabled());
  });

  it('提交所选任务并回显服务端返回的绑定', async () => {
    api.getProjectTasks.mockReturnValue(ok(profile()));
    api.updateProjectTasks.mockReturnValue(
      ok(
        profile({
          source: 'explicit',
          bound: true,
          task_ids: ['bgc.identification'],
          effective_tasks: [CATALOGUE[1]],
          fallback_note: null,
        }),
      ),
    );

    render(<TaskBinding projectId="p1" />);
    await userEvent.click(await screen.findByRole('checkbox', { name: /基因簇识别/ }));
    await userEvent.click(screen.getByRole('button', { name: '保存绑定' }));

    await waitFor(() =>
      expect(api.updateProjectTasks).toHaveBeenCalledWith('p1', ['bgc.identification']),
    );
    await waitFor(() => expect(screen.getByText('已手动绑定')).toBeInTheDocument());
    expect(toastSpy.calls.at(-1)?.title).toBe('已绑定研究领域');
  });

  it('通用任务与具体领域互斥——同时选中会让「通用」失去意义', async () => {
    api.getProjectTasks.mockReturnValue(ok(profile()));
    render(<TaskBinding projectId="p1" />);

    await userEvent.click(await screen.findByRole('checkbox', { name: /基因簇识别/ }));
    await userEvent.click(screen.getByRole('checkbox', { name: /通用学术研究/ }));

    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: /通用学术研究/ })).toHaveAttribute(
        'aria-checked',
        'true',
      ),
    );
    expect(screen.getByRole('checkbox', { name: /基因簇识别/ })).toHaveAttribute(
      'aria-checked',
      'false',
    );
  });

  it('选具体领域时会自动取消「通用」', async () => {
    api.getProjectTasks.mockReturnValue(
      ok(profile({ source: 'explicit', bound: true, task_ids: ['generic.scholarly'] })),
    );
    render(<TaskBinding projectId="p1" />);

    await userEvent.click(await screen.findByRole('checkbox', { name: /投毒攻击/ }));

    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: /通用学术研究/ })).toHaveAttribute(
        'aria-checked',
        'false',
      ),
    );
  });

  it('清空即解除绑定，提交空列表', async () => {
    api.getProjectTasks.mockReturnValue(
      ok(profile({ source: 'explicit', bound: true, task_ids: ['bgc.identification'] })),
    );
    api.updateProjectTasks.mockReturnValue(ok(profile()));

    render(<TaskBinding projectId="p1" />);
    await userEvent.click(await screen.findByRole('button', { name: /清空/ }));
    await userEvent.click(screen.getByRole('button', { name: '保存绑定' }));

    await waitFor(() => expect(api.updateProjectTasks).toHaveBeenCalledWith('p1', []));
    expect(toastSpy.calls.at(-1)?.title).toBe('已恢复为自动判断');
  });

  it('保存失败时保留用户的选择，不假装成功', async () => {
    api.getProjectTasks.mockReturnValue(ok(profile()));
    api.updateProjectTasks.mockReturnValue(Promise.resolve({ data: undefined, source: 'live' }));

    render(<TaskBinding projectId="p1" />);
    await userEvent.click(await screen.findByRole('checkbox', { name: /基因簇识别/ }));
    await userEvent.click(screen.getByRole('button', { name: '保存绑定' }));

    await waitFor(() => expect(toastSpy.calls.at(-1)?.title).toBe('保存失败'));
    expect(screen.getByRole('checkbox', { name: /基因簇识别/ })).toHaveAttribute(
      'aria-checked',
      'true',
    );
  });
});
