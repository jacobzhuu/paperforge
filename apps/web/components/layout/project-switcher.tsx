'use client';

import * as React from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { Check, ChevronsUpDown, LayoutGrid } from 'lucide-react';
import { listProjects } from '@/lib/api';
import type { Project } from '@/lib/types';
import { cn } from '@/lib/utils';

/** 从 `/projects/<id>/<segment>` 取出项目 ID 与当前工作台段。 */
function parseProjectPath(pathname: string): { projectId: string; segment: string } {
  const match = /^\/projects\/([^/]+)(?:\/([^/]+))?/.exec(pathname);
  if (!match || match[1] === undefined) return { projectId: '', segment: '' };
  return { projectId: match[1], segment: match[2] ?? '' };
}

/** 侧边栏顶部的项目切换器：让「我现在在哪个项目里」始终可见，并能原地换项目。 */
export function ProjectSwitcher() {
  const router = useRouter();
  const pathname = usePathname() ?? '';
  const { projectId, segment } = parseProjectPath(pathname);
  const [projects, setProjects] = React.useState<Project[]>([]);
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    let alive = true;
    listProjects()
      .then((res) => alive && setProjects(res.data))
      .catch(() => {
        /* 切换器拿不到列表不影响主流程 */
      });
    return () => {
      alive = false;
    };
  }, []);

  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const current = projects.find((p) => p.id === projectId);

  if (!projectId && projects.length === 0) return null;

  return (
    <div ref={ref} className="relative px-3 pb-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="listbox"
        className="flex w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left text-xs transition-colors hover:bg-accent/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <LayoutGrid className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate">
          {current ? current.title : projectId ? '当前项目' : '未选择项目'}
        </span>
        <ChevronsUpDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      </button>

      {open && (
        <ul
          role="listbox"
          className="absolute left-3 right-3 z-50 mt-1 max-h-72 overflow-y-auto rounded-md border bg-popover p-1 shadow-lg scrollbar-thin animate-fade-in"
        >
          {projects.length === 0 && (
            <li className="px-2 py-1.5 text-xs text-muted-foreground">暂无项目</li>
          )}
          {projects.map((p) => (
            <li key={p.id}>
              <button
                type="button"
                role="option"
                aria-selected={p.id === projectId}
                onClick={() => {
                  setOpen(false);
                  // 停在同一个工作台，只换项目——比把用户扔回列表页少两次点击。
                  // 素材中心只存在于研究型论文，切到综述项目时退回项目概览。
                  const keep = segment === 'assets' && p.paper_type !== 'original' ? '' : segment;
                  router.push(`/projects/${p.id}${keep ? `/${keep}` : ''}`);
                }}
                className={cn(
                  'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  p.id === projectId && 'font-medium',
                )}
              >
                <Check
                  className={cn('h-3.5 w-3.5 shrink-0', p.id === projectId ? '' : 'opacity-0')}
                />
                <span className="min-w-0 flex-1 truncate">{p.title}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
