import * as React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { makeProject, makeToastSpy, mockProjectContext, ok } from './helpers';
import { EMPTY_PROGRESS } from '@/lib/useProjectProgress';
import type { Job } from '@/lib/types';

const projectCtx = { current: mockProjectContext() };
const toastSpy = makeToastSpy();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => '/projects/p1',
}));

vi.mock('@/components/project/project-context', () => ({
  useProject: () => projectCtx.current,
  useJobFinished: () => {},
  useJobEvent: () => {},
}));

vi.mock('@/components/ui/toast', () => ({
  useToast: () => ({ toast: toastSpy.toast }),
}));

const generateAll = vi.fn();

vi.mock('@/lib/api', () => ({
  generateAll: (...args: unknown[]) => generateAll(...args),
  getCitationAudit: () => ok(undefined),
  getCostDetail: () => ok(undefined),
  getNumLint: () => ok(undefined),
  getVersionHistory: () => ok(undefined),
  listExports: () => ok([]),
  listJobs: () => ok([]),
  updateProject: vi.fn(),
  exportDownloadUrl: () => '#',
}));

import { ProjectOverview } from '@/components/project/project-overview';

/** 一个「已经跑完」的项目：有正文也有导出产物。 */
const doneProgress = {
  ...EMPTY_PROGRESS,
  loading: false,
  hasScope: true,
  libraryCount: 12,
  sectionCount: 6,
  wordCount: 4200,
  exportCount: 1,
  hasPdf: true,
};

const freshProgress = { ...EMPTY_PROGRESS, loading: false, hasScope: true, libraryCount: 12 };

function trackedJob(kind: Job['kind'], label: string) {
  return {
    job: { id: 'j1', project_id: 'p1', kind, status: 'running', stage: 'write', progress: 40 },
    label,
    warnings: [],
    startedAt: Date.now(),
    reconnecting: false,
    degradedToPolling: false,
    polishSkipRequested: false,
  };
}

/** render + 冲掉概览自己那次 load()，否则每个用例都刷一屏 act 警告。 */
async function renderOverview() {
  const result = render(<ProjectOverview />);
  await act(async () => {
    await Promise.resolve();
  });
  return result;
}

function runAllButton(): HTMLButtonElement {
  const button = screen
    .getAllByRole('button')
    .find((el) => /全管线|投稿候选稿|正在启动|等待当前任务/.test(el.textContent ?? ''));
  if (!button) throw new Error('找不到全管线按钮');
  return button as HTMLButtonElement;
}

describe('项目概览的「跑通全管线」按钮', () => {
  beforeEach(() => {
    generateAll.mockReset();
    projectCtx.current = mockProjectContext({ project: makeProject(), progress: freshProgress });
  });

  it('没跑过时可点，文案是「跑通全管线」', async () => {
    await renderOverview();
    const button = runAllButton();
    expect(button.textContent).toContain('跑通全管线');
    expect(button.disabled).toBe(false);
  });

  it('全管线在跑时禁用，并且文案换成运行中 + 当前阶段', async () => {
    projectCtx.current = mockProjectContext({
      progress: freshProgress,
      busy: true,
      tracked: trackedJob('full', '正文写作'),
    });
    await renderOverview();
    const button = runAllButton();
    expect(button.textContent).toContain('全管线运行中');
    expect(button.textContent).toContain('正文写作');
    expect(button.disabled).toBe(true);
  });

  it('在跑的是别的任务时不谎称全管线在跑，但同样不可点', async () => {
    projectCtx.current = mockProjectContext({
      progress: freshProgress,
      busy: true,
      tracked: trackedJob('search', '文献检索'),
    });
    await renderOverview();
    const button = runAllButton();
    expect(button.textContent).toContain('等待当前任务结束');
    expect(button.textContent).not.toContain('全管线运行中');
    expect(button.disabled).toBe(true);
  });

  it('跑完之后文案变成「重跑全管线」，不再假装从没跑过', async () => {
    projectCtx.current = mockProjectContext({ progress: doneProgress });
    await renderOverview();
    const button = runAllButton();
    expect(button.textContent).toContain('重跑全管线');
    expect(button.disabled).toBe(false);
  });

  it('质量模式 / 综述方式默认收起，只在「下一步」下方留一行当前取值', async () => {
    await renderOverview();
    // 收起时页面上不该有任何下拉——它们此前一直并排立在主 CTA 旁边。
    expect(screen.queryAllByRole('combobox')).toHaveLength(0);
    expect(screen.getByText('快速草稿 · 叙述性综述')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '调整' }));
    expect(screen.getAllByRole('combobox')).toHaveLength(2);
  });

  it('研究型论文不显示综述方式', async () => {
    projectCtx.current = mockProjectContext({
      paperType: 'original',
      project: makeProject({ paper_type: 'original' }),
      progress: freshProgress,
    });
    await renderOverview();
    expect(screen.getByText('快速草稿')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '调整' }));
    expect(screen.getAllByRole('combobox')).toHaveLength(1);
  });

  it('运行中不能改质量模式——那两个下拉只对下一次运行生效', async () => {
    projectCtx.current = mockProjectContext({
      progress: freshProgress,
      busy: true,
      tracked: trackedJob('full', '正文写作'),
    });
    await renderOverview();
    fireEvent.click(screen.getByRole('button', { name: '调整' }));
    const selects = screen.getAllByRole('combobox') as HTMLSelectElement[];
    expect(selects).toHaveLength(2);
    expect(selects.every((el) => el.disabled)).toBe(true);
  });

  it('主 CTA 那一排只剩「下一步」自己的按钮，全管线挪到分隔线以下', async () => {
    projectCtx.current = mockProjectContext({
      progress: { ...doneProgress, visuals: { ...doneProgress.visuals, ready: 3 } },
    });
    await renderOverview();
    // 「处理视觉建议」是一个链接（CTA），它和全管线按钮不再是同一排的兄弟节点。
    const cta = screen.getByRole('link', { name: /处理视觉建议/ });
    const runRow = runAllButton().parentElement;
    expect(runRow?.contains(cta)).toBe(false);
  });

  it('连点两下只发一次请求：请求在途时按钮已经禁用', async () => {
    let resolve: (value: { data: Job }) => void = () => {};
    generateAll.mockReturnValue(
      new Promise((r) => {
        resolve = r as (value: { data: Job }) => void;
      }),
    );
    await renderOverview();
    const button = runAllButton();

    fireEvent.click(button);
    await waitFor(() => expect(runAllButton().textContent).toContain('正在启动'));
    expect(runAllButton().disabled).toBe(true);
    fireEvent.click(runAllButton());

    expect(generateAll).toHaveBeenCalledTimes(1);
    resolve({ data: { id: 'j1' } as Job });
  });
});
