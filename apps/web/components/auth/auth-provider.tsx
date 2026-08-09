'use client';

import * as React from 'react';
import { usePathname, useRouter } from 'next/navigation';
import { getCurrentUser, loginAccount, logoutAccount } from '@/lib/api';
import type { AuthUser } from '@/lib/types';

const PUBLIC_PATHS = new Set([
  '/login',
  '/register',
  '/forgot-password',
  '/reset-password',
  '/verify-email',
]);

interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  signIn: (account: string, password: string) => Promise<AuthUser>;
  signOut: () => Promise<void>;
}

const AuthContext = React.createContext<AuthContextValue | null>(null);

export function isPublicAuthPath(pathname: string): boolean {
  return PUBLIC_PATHS.has(pathname);
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? '/';
  const router = useRouter();
  const publicPath = isPublicAuthPath(pathname);
  const [user, setUser] = React.useState<AuthUser | null>(null);
  const [loading, setLoading] = React.useState(!publicPath);

  React.useEffect(() => {
    let alive = true;
    if (publicPath) {
      setLoading(false);
      return () => {
        alive = false;
      };
    }
    setLoading(true);
    getCurrentUser()
      .then((current) => {
        if (alive) setUser(current);
      })
      .catch(() => {
        if (!alive) return;
        setUser(null);
        const next = `${window.location.pathname}${window.location.search}`;
        router.replace(`/login?next=${encodeURIComponent(next)}`);
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    /*
     * 只在「公开页 ↔ 受保护区」的边界上校验，不要跟随每一个 pathname。
     *
     * AuthProvider 位于根 layout，原先把 pathname 放在依赖里会让工作台间的每次
     * 客户端导航都 setLoading(true)。AppShell 随即卸载整个应用树，换成
     * 「正在验证会话…」，项目 Provider、任务订阅和页面缓存也一起丢失；等
     * /auth/me 返回后再从头挂载，看起来就像浏览器整页刷新。
     *
     * 同一受保护区内会话失效仍由 paperforge:auth-expired 事件处理，因此不需要
     * 逐路由重复请求 /auth/me。
     */
  }, [publicPath, router]);

  React.useEffect(() => {
    const onExpired = () => {
      setUser(null);
      if (!isPublicAuthPath(window.location.pathname)) {
        const next = `${window.location.pathname}${window.location.search}`;
        router.replace(`/login?next=${encodeURIComponent(next)}`);
      }
    };
    window.addEventListener('paperforge:auth-expired', onExpired);
    return () => window.removeEventListener('paperforge:auth-expired', onExpired);
  }, [router]);

  const signIn = React.useCallback(async (account: string, password: string) => {
    const current = await loginAccount(account, password);
    setUser(current);
    return current;
  }, []);

  const signOut = React.useCallback(async () => {
    try {
      await logoutAccount();
    } finally {
      setUser(null);
      router.replace('/login');
      router.refresh();
    }
  }, [router]);

  const value = React.useMemo(
    () => ({ user, loading, signIn, signOut }),
    [user, loading, signIn, signOut],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = React.useContext(AuthContext);
  if (!value) throw new Error('useAuth 必须在 AuthProvider 内使用');
  return value;
}
