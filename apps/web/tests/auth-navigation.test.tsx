import * as React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

let pathname = '/projects/p1/library';
const replace = vi.fn();
const refresh = vi.fn();
const router = { replace, refresh };

vi.mock('next/navigation', () => ({
  usePathname: () => pathname,
  useRouter: () => router,
}));

vi.mock('@/components/layout/sidebar', () => ({
  Sidebar: () => <aside data-testid="sidebar" />,
}));

vi.mock('@/lib/api', () => ({
  getCurrentUser: vi.fn(),
  loginAccount: vi.fn(),
  logoutAccount: vi.fn(),
}));

import { AppShell } from '@/components/auth/app-shell';
import { AuthProvider } from '@/components/auth/auth-provider';
import { getCurrentUser } from '@/lib/api';

const currentUser = {
  id: 'u1',
  email: 'user@example.com',
  display_name: '测试用户',
  email_verified: true,
};

describe('受保护工作台之间的导航', () => {
  beforeEach(() => {
    pathname = '/projects/p1/library';
    vi.mocked(getCurrentUser).mockResolvedValue(currentUser);
  });

  it('不会重复校验会话或卸载应用内容', async () => {
    const view = render(
      <AuthProvider>
        <AppShell>
          <div>工作台内容</div>
        </AppShell>
      </AuthProvider>,
    );

    await screen.findByText('工作台内容');
    expect(getCurrentUser).toHaveBeenCalledTimes(1);

    pathname = '/projects/p1/write';
    view.rerender(
      <AuthProvider>
        <AppShell>
          <div>工作台内容</div>
        </AppShell>
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('工作台内容')).toBeInTheDocument();
      expect(screen.queryByText('正在验证会话…')).not.toBeInTheDocument();
      expect(getCurrentUser).toHaveBeenCalledTimes(1);
    });
  });
});
