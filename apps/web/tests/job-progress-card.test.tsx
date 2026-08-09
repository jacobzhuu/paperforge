import * as React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { TrackedJob } from '@/lib/useJobTracker';
import { JobProgressCard } from '@/components/jobs/job-progress-card';

function tracked(overrides: Partial<TrackedJob> = {}): TrackedJob {
  return {
    job: {
      id: 'job-1',
      project_id: 'p1',
      kind: 'full',
      status: 'running',
      stage: 'polish',
      progress: 0.86,
    },
    label: '连贯性润色',
    detail: '已润色 4/9 节 · 威胁模型与攻击分类',
    warnings: [],
    startedAt: Date.now() - 60_000,
    reconnecting: false,
    degradedToPolling: false,
    polishSkipRequested: false,
    stopRequested: null,
    ...overrides,
  } as TrackedJob;
}

/**
 * 真实故障：润色阶段所有章节都已「已生成」并落库，进度条却还写着「分节写作」，
 * 十几分钟里既没有细节也没有出口——用户只能理解为系统卡死。
 */
describe('任务进度卡 · 连贯性润色', () => {
  it('润色期间显示阶段名、逐节进度与「稿子已生成」的解释', () => {
    render(<JobProgressCard tracked={tracked()} onSkipPolish={vi.fn()} />);
    expect(screen.getByText('连贯性润色')).toBeTruthy();
    expect(screen.getByText('已润色 4/9 节 · 威胁模型与攻击分类')).toBeTruthy();
    expect(screen.getByText(/正文各节已生成并保存/)).toBeTruthy();
  });

  it('可以中途跳过；点过之后按钮变成「本节后停止」且不可再点', () => {
    const onSkipPolish = vi.fn();
    const { rerender } = render(
      <JobProgressCard tracked={tracked()} onSkipPolish={onSkipPolish} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /跳过润色/ }));
    expect(onSkipPolish).toHaveBeenCalledTimes(1);

    rerender(
      <JobProgressCard
        tracked={tracked({ polishSkipRequested: true })}
        onSkipPolish={onSkipPolish}
      />,
    );
    const button = screen.getByRole('button', { name: /本节后停止/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    // 已经在跑的那一节还要跑完，文案必须说清楚，别让人以为点了没用。
    expect(screen.getByText(/当前这节写完即停/)).toBeTruthy();
  });

  it('其他阶段不显示跳过按钮——只有润色是「稿子已完整、只是在打磨」', () => {
    render(
      <JobProgressCard
        tracked={tracked({ job: { ...tracked().job, stage: 'write' }, label: '分节写作' })}
        onSkipPolish={vi.fn()}
      />,
    );
    expect(screen.queryByRole('button', { name: /跳过润色/ })).toBeNull();
  });
});

/**
 * 一键生成实测 22 分钟起步，此前除了润色阶段的「跳过润色」之外没有任何叫停手段。
 * 停止是协作式的——当前这一步跑完才真的退出——所以「点了还在转」必须被解释清楚，
 * 否则用户只会反复点。
 */
describe('任务进度卡 · 暂停与取消', () => {
  it('运行中给出暂停与取消入口', () => {
    render(
      <JobProgressCard tracked={tracked()} onPause={vi.fn()} onCancel={vi.fn()} />,
    );
    expect(screen.getByRole('button', { name: /暂停/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /取消/ })).toBeTruthy();
  });

  it('点击后回调被调用', () => {
    const onPause = vi.fn();
    const onCancel = vi.fn();
    render(<JobProgressCard tracked={tracked()} onPause={onPause} onCancel={onCancel} />);
    fireEvent.click(screen.getByRole('button', { name: /暂停/ }));
    fireEvent.click(screen.getByRole('button', { name: /取消/ }));
    expect(onPause).toHaveBeenCalledTimes(1);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('已请求暂停后按钮禁用，并说明「当前这一步完成后停止」且可从断点继续', () => {
    render(
      <JobProgressCard
        tracked={tracked({ stopRequested: 'pause' })}
        onPause={vi.fn()}
        onCancel={vi.fn()}
        onSkipPolish={vi.fn()}
      />,
    );
    expect((screen.getByRole('button', { name: /暂停/ }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: /取消/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/已请求暂停/)).toBeTruthy();
    expect(screen.getByText(/从断点继续/)).toBeTruthy();
  });

  it('已请求取消时不承诺「可以继续」——取消就是不再往下跑', () => {
    render(
      <JobProgressCard tracked={tracked({ stopRequested: 'cancel' })} onCancel={vi.fn()} />,
    );
    expect(screen.getByText(/已请求取消/)).toBeTruthy();
    expect(screen.queryByText(/从断点继续/)).toBeNull();
  });

  it('未接线时不画按钮——没有入口好过画一个点了没反应的', () => {
    render(<JobProgressCard tracked={tracked()} />);
    expect(screen.queryByRole('button', { name: /暂停/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /取消/ })).toBeNull();
  });
});

/**
 * 阶段列表把 currentIndex 之前的一律画成「已完成」。证据补充与收敛重写只在
 * 严谨/投稿档才跑，常驻在列表里就等于给每一次草稿运行凭空记上四个从没跑过的阶段。
 */
describe('任务进度卡 · 阶段序列随档位变化', () => {
  function fullJob(qualityProfile?: string) {
    return tracked({
      job: {
        ...tracked().job,
        stage: 'render',
        checkpoint: qualityProfile
          ? { resume: { function: 'run_full_pipeline', kwargs: { quality_profile: qualityProfile } } }
          : undefined,
      },
      label: '编译导出',
    });
  }

  it('默认的一次跑到稿不列出质量修复阶段', () => {
    render(<JobProgressCard tracked={fullJob('draft')} />);
    fireEvent.click(screen.getByRole('button', { name: '展开阶段详情' }));
    expect(screen.getByText('生成质量报告')).toBeTruthy();
    expect(screen.queryByText('重写未达标章节')).toBeNull();
  });

  it('用户主动选了严谨档才列出修复阶段——那一档的收敛确实在管线里跑', () => {
    render(<JobProgressCard tracked={fullJob('scholarly')} />);
    fireEvent.click(screen.getByRole('button', { name: '展开阶段详情' }));
    expect(screen.getByText('重写未达标章节')).toBeTruthy();
  });

  it('档位缺失时按 draft 处理，不凭空多画四个阶段', () => {
    render(<JobProgressCard tracked={fullJob()} />);
    fireEvent.click(screen.getByRole('button', { name: '展开阶段详情' }));
    expect(screen.queryByText('重写未达标章节')).toBeNull();
  });
});
