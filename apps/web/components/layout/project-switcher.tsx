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
  const [activeIndex, setActiveIndex] = React.useState(0);
  const ref = React.useRef<HTMLDivElement>(null);
  const triggerRef = React.useRef<HTMLButtonElement>(null);
  const listRef = React.useRef<HTMLUListElement>(null);

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

  const openList = React.useCallback((index?: number) => {
    const selected = projects.findIndex((project) => project.id === projectId);
    setActiveIndex(index ?? Math.max(0, selected));
    setOpen(true);
    requestAnimationFrame(() => listRef.current?.focus());
  }, [projectId, projects]);

  const choose = React.useCallback((index: number) => {
    const project = projects[index];
    if (!project) return;
    setOpen(false);
    triggerRef.current?.focus();
    router.push(`/projects/${project.id}${segment ? `/${segment}` : ''}`);
  }, [projects, router, segment]);

  if (!projectId && projects.length === 0) return null;

  return (
    <div ref={ref} className="relative px-3 pb-2">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => open ? setOpen(false) : openList()}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            openList(event.key === 'ArrowDown' ? 0 : Math.max(0, projects.length - 1));
          }
        }}
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
          ref={listRef}
          role="listbox"
          tabIndex={0}
          aria-activedescendant={projects[activeIndex] ? `project-option-${projects[activeIndex].id}` : undefined}
          onKeyDown={(event) => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
              event.preventDefault();
              if (projects.length === 0) return;
              const delta = event.key === 'ArrowDown' ? 1 : -1;
              setActiveIndex((index) => (index + delta + projects.length) % projects.length);
            } else if (event.key === 'Home' || event.key === 'End') {
              event.preventDefault();
              setActiveIndex(event.key === 'Home' ? 0 : Math.max(0, projects.length - 1));
            } else if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              choose(activeIndex);
            } else if (event.key === 'Escape') {
              event.preventDefault();
              setOpen(false);
              triggerRef.current?.focus();
            }
          }}
          className="absolute left-3 right-3 z-50 mt-1 max-h-72 overflow-y-auto rounded-md border bg-popover p-1 shadow-lg scrollbar-thin animate-fade-in"
        >
          {projects.length === 0 && (
            <li className="px-2 py-1.5 text-xs text-muted-foreground">暂无项目</li>
          )}
          {projects.map((p) => (
            <li key={p.id}>
              <button
                id={`project-option-${p.id}`}
                type="button"
                role="option"
                aria-selected={p.id === projectId}
                tabIndex={-1}
                onMouseMove={() => setActiveIndex(projects.indexOf(p))}
                onClick={() => choose(projects.indexOf(p))}
                className={cn(
                  'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  p.id === projectId && 'font-medium',
                  projects[activeIndex]?.id === p.id && 'bg-accent',
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
