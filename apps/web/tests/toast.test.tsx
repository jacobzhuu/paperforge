import * as React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { ToastProvider, useToast } from '@/components/ui/toast';

function ToastHarness() {
  const { toast } = useToast();
  return (
    <div>
      <button
        type="button"
        onClick={() => toast({ title: '已保存', description: '判定已经写入。', variant: 'success' })}
      >
        重复成功提示
      </button>
      <button
        type="button"
        onClick={() => {
          toast({ title: '通知一', variant: 'error' });
          toast({ title: '通知二', variant: 'error' });
          toast({ title: '通知三', variant: 'error' });
          toast({ title: '通知四', variant: 'error' });
        }}
      >
        连续提示
      </button>
    </div>
  );
}

describe('全局通知', () => {
  it('使用不透明卡片和顶部布局，并合并重复消息', async () => {
    render(
      <ToastProvider>
        <ToastHarness />
      </ToastProvider>,
    );

    const trigger = screen.getByRole('button', { name: '重复成功提示' });
    await userEvent.click(trigger);
    await userEvent.click(trigger);

    expect(screen.getAllByText('已保存')).toHaveLength(1);
    expect(screen.getByRole('status')).toHaveClass('bg-card');
    expect(screen.getByRole('region', { name: '通知' })).toHaveClass(
      'top-[calc(4rem+env(safe-area-inset-top))]',
      'sm:top-4',
    );
  });

  it('最多保留最近三条消息', async () => {
    render(
      <ToastProvider>
        <ToastHarness />
      </ToastProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: '连续提示' }));

    expect(screen.queryByText('通知一')).not.toBeInTheDocument();
    expect(screen.getByText('通知二')).toBeInTheDocument();
    expect(screen.getByText('通知三')).toBeInTheDocument();
    expect(screen.getByText('通知四')).toBeInTheDocument();
    expect(screen.getAllByRole('alert')).toHaveLength(3);
  });
});
