import * as React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { makeProject, makeToastSpy, mockProjectContext, ok } from './helpers';
import { EMPTY_PROGRESS } from '@/lib/useProjectProgress';
import type { CostDetail, Job } from '@/lib/types';

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
const startPolish = vi.fn();
const skipPolish = vi.fn();
const startQualityRepair = vi.fn();
const skipQualityRepair = vi.fn();
let listedJobs: Job[] = [];
let costDetail: CostDetail | undefined;

vi.mock('@/lib/api', () => ({
  generateAll: (...args: unknown[]) => generateAll(...args),
  startPolish: (...args: unknown[]) => startPolish(...args),
  skipPolish: (...args: unknown[]) => skipPolish(...args),
  startQualityRepair: (...args: unknown[]) => startQualityRepair(...args),
  skipQualityRepair: (...args: unknown[]) => skipQualityRepair(...args),
  getCitationAudit: () => ok(undefined),
  getCostDetail: () => ok(costDetail),
  getNumLint: () => ok(undefined),
  getSubmissionReadiness: () => ok(undefined),
  getVersionHistory: () => ok(undefined),
  restoreDocumentVersion: vi.fn(),
  listExports: () => ok([]),
  listJobs: () => ok(listedJobs),
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
    startPolish.mockReset();
    skipPolish.mockReset();
    listedJobs = [];
    costDetail = undefined;
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
    listedJobs = [
      {
        id: 'full-done',
        project_id: 'p1',
        kind: 'full',
        status: 'succeeded',
        stage: 'done',
        progress: 1,
        checkpoint: { render: { pdf: true } },
      },
    ];
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
    expect(screen.getByText('一次跑到稿（推荐） · 叙述性综述')).toBeInTheDocument();

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
    expect(screen.getByText('一次跑到稿（推荐）')).toBeInTheDocument();
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
    await act(async () => {
      resolve({ data: { id: 'j1' } as Job });
    });
  });

  it('首稿完成后让用户选择开始润色或跳过，不自动启动', async () => {
    const source = {
      id: 'full-1',
      project_id: 'p1',
      kind: 'full',
      status: 'succeeded',
      stage: 'done',
      progress: 1,
      checkpoint: { polish_decision: 'pending' },
    } as Job;
    listedJobs = [source];
    startPolish.mockResolvedValue({
      id: 'polish-1',
      project_id: 'p1',
      kind: 'write',
      status: 'queued',
      progress: 0,
    } as Job);
    await renderOverview();

    expect(screen.getByText('初稿已交付，可选做连贯性润色')).toBeInTheDocument();
    expect(startPolish).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '开始润色' }));
    await waitFor(() => expect(startPolish).toHaveBeenCalledWith('p1', 'full-1'));
    expect(projectCtx.current.startJob).toHaveBeenCalled();
  });

  it('可明确保留首稿并跳过润色', async () => {
    const source = {
      id: 'full-2',
      project_id: 'p1',
      kind: 'full',
      status: 'succeeded',
      stage: 'done',
      progress: 1,
      checkpoint: { polish_decision: 'pending' },
    } as Job;
    listedJobs = [source];
    skipPolish.mockResolvedValue({
      ...source,
      checkpoint: { polish_decision: 'skipped' },
    });
    await renderOverview();

    fireEvent.click(screen.getByRole('button', { name: '保留首稿，跳过润色' }));
    await waitFor(() => expect(skipPolish).toHaveBeenCalledWith('p1', 'full-2'));
    expect(toastSpy.calls.at(-1)?.title).toBe('已保留首稿');
  });

  it('质检发现项交付后邀请修复，不在管线里自动跑', async () => {
    const source = {
      id: 'full-3',
      project_id: 'p1',
      kind: 'full',
      status: 'succeeded',
      stage: 'done',
      progress: 1,
      checkpoint: { quality_repair_decision: 'pending', quality_finding_count: 4 },
    } as Job;
    listedJobs = [source];
    startQualityRepair.mockResolvedValue({
      id: 'repair-1',
      project_id: 'p1',
      kind: 'write',
      status: 'queued',
      progress: 0,
    } as Job);
    await renderOverview();

    expect(screen.getByText('初稿已完成，另有 4 处论断可以再加强')).toBeInTheDocument();
    expect(startQualityRepair).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '开始修复' }));
    await waitFor(() => expect(startQualityRepair).toHaveBeenCalledWith('p1', 'full-3'));
    expect(projectCtx.current.startJob).toHaveBeenCalled();
  });

  it('可以明确放弃这一轮质量修复', async () => {
    const source = {
      id: 'full-4',
      project_id: 'p1',
      kind: 'full',
      status: 'succeeded',
      stage: 'done',
      progress: 1,
      checkpoint: { quality_repair_decision: 'pending', quality_finding_count: 2 },
    } as Job;
    listedJobs = [source];
    skipQualityRepair.mockResolvedValue({
      ...source,
      checkpoint: { quality_repair_decision: 'skipped' },
    });
    await renderOverview();

    fireEvent.click(screen.getByRole('button', { name: '暂不处理' }));
    await waitFor(() => expect(skipQualityRepair).toHaveBeenCalledWith('p1', 'full-4'));
    expect(toastSpy.calls.at(-1)?.title).toBe('已保留当前稿');
  });

  it('未通过质量门时显示具体阻断原因，不冒充全流程完成', async () => {
    listedJobs = [
      {
        id: 'full-blocked',
        project_id: 'p1',
        kind: 'full',
        status: 'needs_input',
        stage: 'done',
        progress: 1,
        checkpoint: {},
        error: {
          readiness_status: 'needs_revision',
          blockers: [
            {
              code: 'core_claim_fulltext_missing',
              message: '2 条核心论断没有可定位且相符的全文证据',
            },
          ],
        },
      },
    ];
    projectCtx.current = mockProjectContext({ progress: doneProgress });
    await renderOverview();

    expect(runAllButton().textContent).toContain('跑通全管线');
    fireEvent.click(screen.getByRole('button', { name: /需补充材料/ }));
    expect(screen.getByText('需补充材料')).toBeInTheDocument();
    expect(
      screen.getByText('2 条核心论断没有可定位且相符的全文证据'),
    ).toBeInTheDocument();
  });

  it('证据门禁在写作前停止时引导到问题证据矩阵', async () => {
    listedJobs = [
      {
        id: 'full-evidence-blocked',
        project_id: 'p1',
        kind: 'full',
        status: 'needs_input',
        stage: 'done',
        progress: 1,
        checkpoint: {},
        error: {
          readiness_status: 'evidence_insufficient',
          blockers: [
            {
              code: 'question_evidence_coverage_low',
              message: '仅 1/5 个子问题具备至少两篇文献的可用全文证据',
            },
          ],
        },
      },
    ];
    projectCtx.current = mockProjectContext({ progress: doneProgress });
    await renderOverview();

    fireEvent.click(screen.getByRole('button', { name: /需补充材料/ }));
    expect(
      screen.getByText('证据就绪门禁未通过，流程已在正文写作前停止。'),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /去查看问题—证据矩阵/ })).toHaveAttribute(
      'href',
      '/projects/p1/questions',
    );
  });

  // ---- 成本面板（P1-4）------------------------------------------------------
  //
  // 修好之前 cost_estimate 永远是 0，一次几百万 token 的运行看起来是免费的。
  // 现在真正的风险换成了「部分定价」：金额非零、看起来权威，却漏掉整个模型。

  function costWith(totals: Partial<CostDetail['totals']>): CostDetail {
    return {
      project_id: 'p1',
      totals: {
        project_id: 'p1',
        call_count: 10,
        input_tokens: 1000,
        output_tokens: 500,
        cost_estimate: 1.25,
        failed_call_count: 0,
        priced_call_count: 10,
        unpriced_call_count: 0,
        cost_complete: true,
        ...totals,
      },
      by_role: [],
    };
  }

  it('全部定价时直接给出金额，不加下界符号', async () => {
    costDetail = costWith({});
    await renderOverview();

    await waitFor(() => expect(screen.getByText('$1.2500')).toBeTruthy());
    expect(screen.queryByText(/无法估价/)).toBeNull();
  });

  it('有未定价调用时金额显示为下界，并说明原因', async () => {
    costDetail = costWith({ priced_call_count: 7, unpriced_call_count: 3, cost_complete: false });
    await renderOverview();

    await waitFor(() => expect(screen.getByText('≥ $1.2500')).toBeTruthy());
    const note = screen.getByText(/无法估价/);
    expect(note.textContent).toContain('3 次调用无法估价');
    expect(note.textContent).toContain('下界');
  });

  it('图片生成没有价格来源时同样把总额降级为下界', async () => {
    costDetail = {
      ...costWith({}),
      images: { call_count: 2, failed_call_count: 0, cost_estimate: 0, unpriced_call_count: 2 },
    };
    await renderOverview();

    await waitFor(() => expect(screen.getByText('≥ $1.2500')).toBeTruthy());
    expect(screen.getByText(/无法估价/).textContent).toContain('图片生成暂无价格来源');
  });

});
