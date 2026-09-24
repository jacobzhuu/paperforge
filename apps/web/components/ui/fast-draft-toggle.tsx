'use client';

import * as React from 'react';
import { Zap } from 'lucide-react';
import type { ExecutionProfile } from '@/lib/types';
import { cn } from '@/lib/utils';

const TOOLTIP = '更快生成初稿，引用仍需完整核验。';

export function FastDraftToggle({
  value,
  onChange,
  disabled = false,
  className,
}: {
  value: ExecutionProfile;
  onChange: (value: ExecutionProfile) => void;
  disabled?: boolean;
  className?: string;
}) {
  const tooltipId = React.useId();
  const hoverTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const animationTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const dismissed = React.useRef(false);
  const [tooltipOpen, setTooltipOpen] = React.useState(false);
  const [activating, setActivating] = React.useState(false);

  React.useEffect(() => () => {
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    if (animationTimer.current) clearTimeout(animationTimer.current);
  }, []);

  const closeTooltip = () => {
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    setTooltipOpen(false);
    dismissed.current = false;
  };

  return (
    <span className={cn('relative inline-flex shrink-0', className)}>
      <button
        type="button"
        aria-label="快速草稿"
        aria-pressed={value === 'fast_draft'}
        aria-describedby={tooltipOpen ? tooltipId : undefined}
        disabled={disabled}
        onPointerEnter={(event) => {
          if (event.pointerType === 'touch' || dismissed.current || disabled) return;
          if (hoverTimer.current) clearTimeout(hoverTimer.current);
          hoverTimer.current = setTimeout(() => setTooltipOpen(true), 250);
        }}
        onPointerLeave={closeTooltip}
        onFocus={() => {
          if (!dismissed.current && !disabled) setTooltipOpen(true);
        }}
        onBlur={closeTooltip}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            if (hoverTimer.current) clearTimeout(hoverTimer.current);
            dismissed.current = true;
            setTooltipOpen(false);
          }
        }}
        onClick={() => {
          const next = value === 'fast_draft' ? 'standard' : 'fast_draft';
          onChange(next);
          if (next === 'fast_draft') {
            if (animationTimer.current) clearTimeout(animationTimer.current);
            setActivating(true);
            animationTimer.current = setTimeout(() => setActivating(false), 420);
          }
        }}
        className={cn(
          'inline-flex h-11 items-center gap-1.5 rounded-lg border px-2.5 text-sm font-medium transition-colors',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1',
          'disabled:cursor-not-allowed disabled:opacity-50',
          value === 'fast_draft'
            ? 'border-primary/30 bg-accent/60 text-foreground hover:bg-accent/80'
            : 'border-border/80 bg-transparent text-muted-foreground hover:border-foreground/25 hover:bg-muted/50 hover:text-foreground',
          activating && 'fast-draft-activating',
        )}
      >
        <Zap aria-hidden="true" className="fast-draft-icon h-3.5 w-3.5 shrink-0" strokeWidth={1.8} />
        <span>快速草稿：{value === 'fast_draft' ? '开' : '关'}</span>
      </button>
      {tooltipOpen && (
        <span
          id={tooltipId}
          role="tooltip"
          className="pointer-events-none absolute right-0 top-full z-40 mt-2 w-64 max-w-[calc(100vw-2rem)] rounded-md border border-border bg-popover px-3 py-2 text-left text-xs leading-relaxed text-popover-foreground shadow-md"
        >
          {TOOLTIP}
        </span>
      )}
    </span>
  );
}
