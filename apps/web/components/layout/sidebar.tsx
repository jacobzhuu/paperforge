'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  LayoutGrid,
  Library,
  ListTree,
  PenLine,
  FolderUp,
  FileDown,
  Settings,
  FlaskConical,
} from 'lucide-react';
import { cn } from '@/lib/utils';

const NAV = [
  { href: '/projects', label: '项目', icon: LayoutGrid, match: (p: string) => p === '/' || p.startsWith('/projects') },
  { href: '/library', label: '文献工作台', icon: Library, match: (p: string) => p.startsWith('/library') },
  { href: '/outline', label: '大纲编辑器', icon: ListTree, match: (p: string) => p.startsWith('/outline') },
  { href: '/write', label: '写作工作台', icon: PenLine, match: (p: string) => p.startsWith('/write') },
  { href: '/assets', label: '素材中心', icon: FolderUp, match: (p: string) => p.startsWith('/assets') },
  { href: '/export', label: '导出中心', icon: FileDown, match: (p: string) => p.startsWith('/export') },
  { href: '/settings', label: '设置', icon: Settings, match: (p: string) => p.startsWith('/settings') },
];

export function Sidebar() {
  const pathname = usePathname() ?? '/';
  return (
    <aside className="flex h-screen w-60 shrink-0 flex-col border-r bg-card">
      <div className="flex items-center gap-2 px-5 py-5">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
          <FlaskConical className="h-5 w-5" />
        </div>
        <div className="leading-tight">
          <div className="text-sm font-semibold">PaperForge</div>
          <div className="text-[11px] text-muted-foreground">成稿优先 · 引用真实</div>
        </div>
      </div>
      <nav className="flex-1 space-y-1 px-3">
        {NAV.map((item) => {
          const active = item.match(pathname);
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                active
                  ? 'bg-accent text-accent-foreground'
                  : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground',
              )}
            >
              <Icon className="h-4 w-4" />
              {item.label}
            </Link>
          );
        })}
      </nav>
      <div className="border-t px-5 py-4 text-[11px] text-muted-foreground">
        <p>M2 综述管线</p>
        <p className="mt-0.5">API 501 时自动降级示例数据</p>
      </div>
    </aside>
  );
}
