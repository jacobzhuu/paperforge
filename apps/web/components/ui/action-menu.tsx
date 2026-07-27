'use client';

import * as React from 'react';
import { ChevronDown } from 'lucide-react';
import { Button } from './button';
import { cn } from '@/lib/utils';

export interface ActionMenuItem {
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  onSelect: () => void;
  description?: string;
  disabled?: boolean;
}

/**
 * 次要操作收纳菜单：页头挤了五六个按钮时，窄屏会换行成一团。
 *
 * 补上方向键导航与焦点管理——此前虽有 role="menu"/"menuitem"，但打开后
 * 焦点仍留在触发按钮上，键盘用户要一路 Tab 才能进到菜单项。
 */
export function ActionMenu({
  label = '更多',
  items,
  disabled,
}: {
  label?: string;
  items: ActionMenuItem[];
  disabled?: boolean;
}) {
  const [open, setOpen] = React.useState(false);
  const [activeIndex, setActiveIndex] = React.useState(0);
  const ref = React.useRef<HTMLDivElement>(null);
  const triggerRef = React.useRef<HTMLButtonElement>(null);
  const itemRefs = React.useRef<(HTMLButtonElement | null)[]>([]);

  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  // 打开时把焦点送到第一项。
  React.useEffect(() => {
    if (!open) return;
    setActiveIndex(0);
    const raf = requestAnimationFrame(() => itemRefs.current[0]?.focus());
    return () => cancelAnimationFrame(raf);
  }, [open]);

  const close = (restoreFocus = true) => {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  };

  const onMenuKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
      return;
    }
    const enabled = items.map((item, i) => (item.disabled ? -1 : i)).filter((i) => i >= 0);
    if (enabled.length === 0) return;
    const position = enabled.indexOf(activeIndex);
    let next = activeIndex;
    if (e.key === 'ArrowDown') next = enabled[(position + 1) % enabled.length];
    else if (e.key === 'ArrowUp') next = enabled[(position - 1 + enabled.length) % enabled.length];
    else if (e.key === 'Home') next = enabled[0];
    else if (e.key === 'End') next = enabled[enabled.length - 1];
    else return;
    e.preventDefault();
    setActiveIndex(next);
    itemRefs.current[next]?.focus();
  };

  return (
    <div ref={ref} className="relative">
      <Button
        ref={triggerRef}
        variant="outline"
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown' && !open) {
            e.preventDefault();
            setOpen(true);
          }
        }}
        disabled={disabled}
        aria-expanded={open}
        aria-haspopup="menu"
      >
        {label} <ChevronDown className={cn('transition-transform', open && 'rotate-180')} />
      </Button>
      {open && (
        <div
          role="menu"
          aria-label={label}
          onKeyDown={onMenuKeyDown}
          className="absolute right-0 z-50 mt-1 w-64 rounded-md border bg-popover p-1 shadow-lg animate-fade-in"
        >
          {items.map((item, index) => {
            const Icon = item.icon;
            return (
              <button
                key={item.label}
                ref={(el) => {
                  itemRefs.current[index] = el;
                }}
                type="button"
                role="menuitem"
                tabIndex={index === activeIndex ? 0 : -1}
                disabled={item.disabled}
                onClick={() => {
                  close(false);
                  item.onSelect();
                }}
                className="flex w-full items-start gap-2.5 rounded px-2 py-2 text-left transition-colors hover:bg-accent disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0">
                  <span className="block text-sm font-medium">{item.label}</span>
                  {item.description && (
                    <span className="mt-0.5 block text-xs leading-snug text-muted-foreground">
                      {item.description}
                    </span>
                  )}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
