'use client';

import * as React from 'react';
import { AlertTriangle, CheckCircle2, Info, X } from 'lucide-react';
import { cn } from '@/lib/utils';

type ToastVariant = 'success' | 'error' | 'info';

interface ToastItem {
  id: number;
  title: string;
  description?: string;
  variant: ToastVariant;
}

interface ToastContextValue {
  toast: (input: { title: string; description?: string; variant?: ToastVariant }) => void;
}

const ToastContext = React.createContext<ToastContextValue | null>(null);

/** 成功类提示自动消散；错误常驻，直到用户自己关掉——失败的操作值得被看见。 */
const AUTO_DISMISS_MS: Record<ToastVariant, number | null> = {
  success: 4000,
  info: 5000,
  error: null,
};

const ICON: Record<ToastVariant, typeof Info> = {
  success: CheckCircle2,
  error: AlertTriangle,
  info: Info,
};

const TONE: Record<ToastVariant, string> = {
  success: 'border-success/40 border-l-4 border-l-success bg-card text-foreground',
  error: 'border-destructive/40 border-l-4 border-l-destructive bg-card text-foreground',
  info: 'border-border border-l-4 border-l-primary bg-card text-foreground',
};

const ICON_TONE: Record<ToastVariant, string> = {
  success: 'text-success-strong',
  error: 'text-destructive-strong',
  info: 'text-muted-foreground',
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = React.useState<ToastItem[]>([]);
  const itemsRef = React.useRef<ToastItem[]>([]);
  const timers = React.useRef(new Map<number, ReturnType<typeof setTimeout>>());
  const nextId = React.useRef(1);

  const commit = React.useCallback((next: ToastItem[]) => {
    itemsRef.current = next;
    setItems(next);
  }, []);

  const dismiss = React.useCallback((id: number) => {
    const timer = timers.current.get(id);
    if (timer) clearTimeout(timer);
    timers.current.delete(id);
    commit(itemsRef.current.filter((item) => item.id !== id));
  }, [commit]);

  const scheduleDismiss = React.useCallback(
    (id: number, variant: ToastVariant) => {
      const existingTimer = timers.current.get(id);
      if (existingTimer) clearTimeout(existingTimer);
      timers.current.delete(id);

      const ttl = AUTO_DISMISS_MS[variant];
      if (ttl === null) return;
      const timer = setTimeout(() => {
        timers.current.delete(id);
        commit(itemsRef.current.filter((item) => item.id !== id));
      }, ttl);
      timers.current.set(id, timer);
    },
    [commit],
  );

  React.useEffect(
    () => () => {
      timers.current.forEach((timer) => clearTimeout(timer));
      timers.current.clear();
    },
    [],
  );

  const toast = React.useCallback<ToastContextValue['toast']>(
    ({ title, description, variant = 'info' }) => {
      const duplicate = itemsRef.current.find(
        (item) =>
          item.title === title &&
          item.description === description &&
          item.variant === variant,
      );
      const item: ToastItem = duplicate
        ? { ...duplicate, title, description, variant }
        : { id: nextId.current++, title, description, variant };

      // 相同通知只保留一条并续期；全局最多展示最近三条，避免遮住页面内容。
      const withNewestLast = [
        ...itemsRef.current.filter((current) => current.id !== item.id),
        item,
      ];
      const removed = withNewestLast.slice(0, Math.max(0, withNewestLast.length - 3));
      removed.forEach((oldItem) => {
        const timer = timers.current.get(oldItem.id);
        if (timer) clearTimeout(timer);
        timers.current.delete(oldItem.id);
      });
      commit(withNewestLast.slice(-3));
      scheduleDismiss(item.id, variant);
    },
    [commit, scheduleDismiss],
  );

  const value = React.useMemo(() => ({ toast }), [toast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        className="pointer-events-none fixed left-4 right-4 top-[calc(4rem+env(safe-area-inset-top))] z-[60] flex flex-col gap-2 sm:left-auto sm:right-4 sm:top-4 sm:w-[min(24rem,calc(100vw-2rem))]"
        // 读屏用户此前收不到任何操作反馈：全应用零 aria-live。
        role="region"
        aria-label="通知"
      >
        {items.map((item) => {
          const Icon = ICON[item.variant];
          return (
            <div
              key={item.id}
              role={item.variant === 'error' ? 'alert' : 'status'}
              aria-live={item.variant === 'error' ? 'assertive' : 'polite'}
              className={cn(
                'pointer-events-auto flex items-start gap-2.5 rounded-lg border p-3 shadow-lg animate-fade-in',
                TONE[item.variant],
              )}
            >
              <Icon className={cn('mt-0.5 h-4 w-4 shrink-0', ICON_TONE[item.variant])} />
              <div className="min-w-0 flex-1">
                <p className="break-words text-sm font-medium leading-snug">{item.title}</p>
                {item.description && (
                  <p className="mt-0.5 break-words text-xs leading-relaxed text-muted-foreground">
                    {item.description}
                  </p>
                )}
              </div>
              <button
                type="button"
                onClick={() => dismiss(item.id)}
                aria-label="关闭通知"
                className="-m-2 flex h-11 w-11 shrink-0 items-center justify-center rounded text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = React.useContext(ToastContext);
  if (!ctx) throw new Error('useToast 必须在 <ToastProvider> 内使用');
  return ctx;
}
