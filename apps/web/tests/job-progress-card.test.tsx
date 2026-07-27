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
