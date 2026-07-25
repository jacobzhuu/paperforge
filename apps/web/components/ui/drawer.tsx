'use client';

import * as React from 'react';
import { X } from 'lucide-react';
import { cn } from '@/lib/utils';
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

export function Drawer({ open, onClose, title, description, children, footer, className }: DrawerProps) {
  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/40 animate-fade-in" onClick={onClose} />
      <aside
        className={cn(
          'relative z-10 flex h-full w-full max-w-xl flex-col bg-card shadow-xl animate-slide-in-right',
          className,
        )}
      >
        <header className="flex items-start justify-between gap-4 border-b p-5">
          <div className="space-y-1">
            {title && <h2 className="text-lg font-semibold leading-tight">{title}</h2>}
            {description && <p className="text-sm text-muted-foreground">{description}</p>}
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="关闭">
            <X />
          </Button>
        </header>
        <div className="flex-1 overflow-y-auto scrollbar-thin p-5">{children}</div>
        {footer && <footer className="border-t p-4">{footer}</footer>}
      </aside>
    </div>
  );
}
