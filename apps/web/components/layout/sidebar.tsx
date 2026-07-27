'use client';

import * as React from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  LayoutGrid,
  PenLine,
  Settings,
  FlaskConical,
  LogOut,
  Menu,
  UserRound,
  X,
} from 'lucide-react';
import { useAuth } from '@/components/auth/auth-provider';
import { ProjectSwitcher } from './project-switcher';
import { ThemeToggle } from './theme-toggle';
import { cn } from '@/lib/utils';

/**
 * 应用级导航。
 *
 * 此前这里放了七个工作台入口，每一个都必须手工带上 `?project=`，漏一处
 * 用户就掉回「尚未选择项目」；而且综述项目也会看到只对研究型论文有意义的
 * 「素材中心」。工作台导航现在归项目 shell 管
 * （`components/project/project-pipeline-nav.tsx`），那里能按 paper_type
 * 决定步骤集合与顺序。这里只留应用级的两项。
 */
const NAV = [
  {
    // 首页现在是 Prompt Canvas（意图输入），不再 redirect 到项目列表，
    // 所以它值一个自己的入口——而且是第一个（ui-design.md 原则 02）。
    href: '/',
    label: '新论文',
    icon: PenLine,
    match: (p: string) => p === '/',
  },
  {
    href: '/projects',
    label: '项目',
    icon: LayoutGrid,
    match: (p: string) => p.startsWith('/projects'),
  },
  {
    href: '/settings',
    label: '设置',
    icon: Settings,
    match: (p: string) => p.startsWith('/settings'),
  },
];

function NavList({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname() ?? '/';

  return (
    <nav className="flex-1 space-y-1 px-3" aria-label="主导航">
      {NAV.map((item) => {
        const active = item.match(pathname);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            aria-current={active ? 'page' : undefined}
            className={cn(
              'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              active
                ? 'bg-accent text-accent-foreground'
                : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground',
            )}
          >
            <Icon className="h-4 w-4 shrink-0" />
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

function Brand() {
  return (
    <div className="flex items-center gap-2 px-5 py-5">
      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
        <FlaskConical className="h-5 w-5" />
      </div>
      <div className="leading-tight">
        {/* 品牌字用 serif（18px，刚好过阈值）——它是 logotype，不是界面文字。 */}
        <div className="font-serif text-lg font-semibold tracking-tight">PaperForge</div>
        <div className="text-xs text-muted-foreground">成稿优先 · 引用真实</div>
      </div>
    </div>
  );
}

function SidebarBody({ onNavigate }: { onNavigate?: () => void }) {
  const { user, signOut } = useAuth();

  return (
    <>
      <Brand />
      <ProjectSwitcher />
      <NavList onNavigate={onNavigate} />
      <div className="flex items-center justify-between border-t px-4 py-3">
        <span className="text-xs text-muted-foreground">主题</span>
        <ThemeToggle />
      </div>
      <div className="border-t px-3 py-3">
        <div className="flex items-center gap-2 px-2 pb-2">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent text-accent-foreground">
            <UserRound className="h-4 w-4" />
          </div>
          <div className="min-w-0 flex-1 leading-tight">
            <div className="truncate text-sm font-medium">{user?.display_name || 'PaperForge 用户'}</div>
            <div className="truncate text-xs text-muted-foreground">{user?.email}</div>
          </div>
        </div>
        <button
          type="button"
          onClick={() => {
            onNavigate?.();
            void signOut();
          }}
          className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <LogOut className="h-4 w-4" />
          退出登录
        </button>
      </div>
    </>
  );
}

export function Sidebar() {
  const pathname = usePathname();
  const [mobileOpen, setMobileOpen] = React.useState(false);

  // 移动端抽屉在路由变化后自动收起。
  React.useEffect(() => {
    setMobileOpen(false);
  }, [pathname]);

  React.useEffect(() => {
    if (!mobileOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setMobileOpen(false);
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [mobileOpen]);

  return (
    <>
      {/* 宽屏：固定侧栏 */}
      <aside className="hidden h-screen w-56 shrink-0 flex-col border-r bg-card md:sticky md:top-0 md:flex">
        <SidebarBody />
      </aside>

      {/* 窄屏：顶栏 + 抽屉 */}
      <header className="fixed inset-x-0 top-0 z-40 flex h-14 items-center gap-2 border-b bg-card px-4 md:hidden">
        <button
          type="button"
          onClick={() => setMobileOpen(true)}
          aria-label="打开导航"
          aria-expanded={mobileOpen}
          className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Menu className="h-5 w-5" />
        </button>
        <div className="flex items-center gap-1.5">
          <FlaskConical className="h-4 w-4 text-primary" />
          <span className="font-serif text-base font-semibold">PaperForge</span>
        </div>
      </header>

      {mobileOpen && (
        <div className="fixed inset-0 z-50 md:hidden">
          <div
            className="absolute inset-0 bg-black/50 animate-fade-in dark:bg-black/70"
            onClick={() => setMobileOpen(false)}
          />
          <aside className="relative flex h-full w-64 flex-col border-r bg-card shadow-xl animate-fade-in">
            <button
              type="button"
              onClick={() => setMobileOpen(false)}
              aria-label="关闭导航"
              className="absolute right-3 top-4 rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="h-4 w-4" />
            </button>
            <SidebarBody onNavigate={() => setMobileOpen(false)} />
          </aside>
        </div>
      )}
    </>
  );
}
