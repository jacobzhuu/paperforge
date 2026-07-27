import * as React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MoreHorizontal } from 'lucide-react';
import { describe, expect, it, vi } from 'vitest';
import { ActionMenu } from '@/components/ui/action-menu';

describe('次要操作菜单', () => {
  it('点击打开后可选择菜单项', async () => {
    const onSelect = vi.fn();
    render(
      <ActionMenu
        items={[{ label: '下载', icon: MoreHorizontal, onSelect }]}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /更多/ }));
    fireEvent.click(await screen.findByRole('menuitem', { name: '下载' }));

    expect(onSelect).toHaveBeenCalledOnce();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('支持方向键打开，并在 Escape 后将焦点还给触发按钮', async () => {
    render(
      <ActionMenu
        items={[{ label: '查看来源', icon: MoreHorizontal, onSelect: vi.fn() }]}
      />,
    );
    const trigger = screen.getByRole('button', { name: /更多/ });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });

    const item = await screen.findByRole('menuitem', { name: '查看来源' });
    await waitFor(() => expect(item).toHaveFocus());
    fireEvent.keyDown(item, { key: 'Escape' });
    expect(trigger).toHaveFocus();
  });
});
