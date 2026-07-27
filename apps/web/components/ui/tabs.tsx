'use client';

import * as React from 'react';
import { cn } from '@/lib/utils';
import { useId } from '@/lib/useFocusTrap';

interface TabsContextValue {
  value: string;
  setValue: (v: string) => void;
  baseId: string;
  register: (value: string) => void;
  values: React.MutableRefObject<string[]>;
}

const TabsContext = React.createContext<TabsContextValue | null>(null);

/**
 * 标签组。
 *
 * 此前 `TabsList` 是裸 `<div>`、`TabsTrigger` 是裸 `<button>`：没有
 * role=tablist/tab、没有 aria-selected、没有方向键导航。读屏用户听不出
 * 「编辑 / 预览 / 引用审计 / 质量报告」是一组标签页，只会听到四个孤立按钮。
 */
export function Tabs({
  value,
  onValueChange,
  defaultValue,
  className,
  children,
}: {
  value?: string;
  onValueChange?: (v: string) => void;
  defaultValue?: string;
  className?: string;
  children: React.ReactNode;
}) {
  const [internal, setInternal] = React.useState(defaultValue ?? '');
  const current = value ?? internal;
  const baseId = useId('tabs');
  const values = React.useRef<string[]>([]);

  const setValue = React.useCallback(
    (v: string) => {
      setInternal(v);
      onValueChange?.(v);
    },
    [onValueChange],
  );

  const register = React.useCallback((v: string) => {
    if (!values.current.includes(v)) values.current.push(v);
  }, []);

  const ctx = React.useMemo(
    () => ({ value: current, setValue, baseId, register, values }),
    [current, setValue, baseId, register],
  );

  return (
    <TabsContext.Provider value={ctx}>
      <div className={className}>{children}</div>
    </TabsContext.Provider>
  );
}

export function TabsList({
  className,
  children,
}: {
  className?: string;
  children: React.ReactNode;
}) {
  const ctx = React.useContext(TabsContext);

  /** 左右方向键在标签间移动，Home/End 跳首尾（WAI-ARIA 标签组模式）。 */
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!ctx) return;
    const list = ctx.values.current;
    const index = list.indexOf(ctx.value);
    if (index < 0) return;
    let next = index;
    if (e.key === 'ArrowRight') next = (index + 1) % list.length;
    else if (e.key === 'ArrowLeft') next = (index - 1 + list.length) % list.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = list.length - 1;
    else return;
    e.preventDefault();
    ctx.setValue(list[next]);
    document.getElementById(`${ctx.baseId}-tab-${list[next]}`)?.focus();
  };

  return (
    <div
      role="tablist"
      onKeyDown={onKeyDown}
      className={cn(
        'inline-flex h-9 items-center justify-center rounded-lg bg-muted p-1 text-muted-foreground',
        className,
      )}
    >
      {children}
    </div>
  );
}

export function TabsTrigger({ value, children }: { value: string; children: React.ReactNode }) {
  const ctx = React.useContext(TabsContext);

  React.useEffect(() => {
    ctx?.register(value);
  }, [ctx, value]);

  if (!ctx) return null;
  const active = ctx.value === value;

  return (
    <button
      type="button"
      role="tab"
      id={`${ctx.baseId}-tab-${value}`}
      aria-selected={active}
      aria-controls={`${ctx.baseId}-panel-${value}`}
      // 只有选中项进 Tab 序列，方向键负责组内移动。
      tabIndex={active ? 0 : -1}
      onClick={() => ctx.setValue(value)}
      className={cn(
        'inline-flex items-center justify-center whitespace-nowrap rounded-md px-3 py-1 text-sm font-medium transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        active ? 'bg-background text-foreground shadow-sm' : 'hover:text-foreground',
      )}
    >
      {children}
    </button>
  );
}

export function TabsContent({
  value,
  className,
  children,
}: {
  value: string;
  className?: string;
  children: React.ReactNode;
}) {
  const ctx = React.useContext(TabsContext);
  if (!ctx || ctx.value !== value) return null;
  return (
    <div
      role="tabpanel"
      id={`${ctx.baseId}-panel-${value}`}
      aria-labelledby={`${ctx.baseId}-tab-${value}`}
      tabIndex={0}
      className={cn('mt-4 animate-fade-in', className)}
    >
      {children}
    </div>
  );
}
