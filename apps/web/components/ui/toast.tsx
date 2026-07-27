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
  success: 'border-success/40 bg-success/10 text-foreground',
  error: 'border-destructive/40 bg-destructive/10 text-foreground',
  info: 'border-border bg-card text-foreground',
};

const ICON_TONE: Record<ToastVariant, string> = {
  success: 'text-success-strong',
  error: 'text-destructive-strong',
  info: 'text-muted-foreground',
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = React.useState<ToastItem[]>([]);
  const nextId = React.useRef(0);

  const dismiss = React.useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const toast = React.useCallback<ToastContextValue['toast']>(
    ({ title, description, variant = 'info' }) => {
      const id = nextId.current++;
      setItems((prev) => [...prev, { id, title, description, variant }]);
      const ttl = AUTO_DISMISS_MS[variant];
      if (ttl !== null) setTimeout(() => dismiss(id), ttl);
    },
    [dismiss],
  );

  const value = React.useMemo(() => ({ toast }), [toast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2"
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
                <p className="text-sm font-medium leading-snug">{item.title}</p>
                {item.description && (
                  <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                    {item.description}
                  </p>
                )}
              </div>
              <button
                type="button"
                onClick={() => dismiss(item.id)}
                aria-label="关闭通知"
                className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
