'use client';

import * as React from 'react';
import { X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useFocusTrap, useId } from '@/lib/useFocusTrap';
import { Button } from './button';

interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  description?: React.ReactNode;
  children?: React.ReactNode;
  footer?: React.ReactNode;
  className?: string;
}

export function Drawer({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  className,
}: DrawerProps) {
  const panelRef = React.useRef<HTMLElement>(null);
  const titleId = useId('drawer-title');
  const descId = useId('drawer-desc');

  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  useFocusTrap(open, panelRef);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div
        className="absolute inset-0 bg-black/45 backdrop-blur-[2px] animate-fade-in dark:bg-black/70"
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        className={cn(
          'relative z-10 flex h-[100dvh] w-full max-w-xl flex-col bg-card/95 shadow-xl backdrop-blur-xl animate-slide-in-right sm:border-l',
          className,
        )}
      >
        <header className="flex items-start justify-between gap-4 border-b px-5 pb-4 pt-[max(1.25rem,env(safe-area-inset-top))]">
          <div className="space-y-1">
            {title && (
              <h2 id={titleId} className="text-lg font-semibold leading-tight">
                {title}
              </h2>
            )}
            {description && (
              <p id={descId} className="text-sm text-muted-foreground">
                {description}
              </p>
            )}
          </div>
          <Button variant="ghost" size="icon" className="h-11 w-11" onClick={onClose} aria-label="关闭">
            <X />
          </Button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain scrollbar-thin p-5">{children}</div>
        {footer && (
          <footer className="shrink-0 border-t bg-card/95 px-4 pb-[max(1rem,env(safe-area-inset-bottom))] pt-4 backdrop-blur-xl">
            {footer}
          </footer>
        )}
      </aside>
    </div>
  );
}
