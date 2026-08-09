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
    expect(listbox).toHaveAttribute('aria-activedescendant', 'project-option-p3');
    await userEvent.keyboard('{Enter}');
    expect(push).toHaveBeenCalledWith('/projects/p3/write');
    expect(trigger).toHaveFocus();

    await userEvent.click(trigger);
    await userEvent.keyboard('{Home}{Escape}');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
