import * as React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const push = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push }),
  usePathname: () => '/projects/p2/write',
}));
vi.mock('@/lib/api', () => ({
  listProjects: () => Promise.resolve({
    source: 'live',
    data: [
      { id: 'p1', title: '项目一' },
      { id: 'p2', title: '项目二' },
      { id: 'p3', title: '项目三' },
    ],
  }),
}));

import { ProjectSwitcher } from '@/components/layout/project-switcher';

describe('项目切换器键盘模型', () => {
  beforeEach(() => push.mockReset());

  it('支持方向键、Home/End、Enter 与 Escape，并保持当前工作台段', async () => {
    render(<ProjectSwitcher />);
    const trigger = await screen.findByRole('button', { name: /项目二/ });
    trigger.focus();
    await userEvent.keyboard('{ArrowDown}');
    const listbox = await screen.findByRole('listbox');
    await waitFor(() => expect(listbox).toHaveAttribute('aria-activedescendant', 'project-option-p1'));
    await userEvent.keyboard('{End}');
    // 活动项由 React 状态驱动，按键之后并不同步落到 DOM 上。上一条断言用了
    // waitFor，这几条却是裸 expect——在快的 Linux runner 上碰巧赢了竞态，
    // 在负载高的 macOS runner 上就输。断言口径必须一致。
    await waitFor(() =>
      expect(listbox).toHaveAttribute('aria-activedescendant', 'project-option-p3'),
    );
    await userEvent.keyboard('{Enter}');
    await waitFor(() => expect(push).toHaveBeenCalledWith('/projects/p3/write'));
    await waitFor(() => expect(trigger).toHaveFocus());

    await userEvent.click(trigger);
    await userEvent.keyboard('{Home}{Escape}');
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});
