import * as React from 'react';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

export type StatusState =
  | 'idle'
  | 'running'
  | 'done'
  | 'degraded'
  | 'failed'
  | 'paused'
  | 'needs_input';

const statusConfig: Record<
  StatusState,
  { label: string; dot: string; badge: 'muted' | 'warning' | 'success' | 'destructive' | 'secondary' }
> = {
  idle: { label: '等待', dot: 'bg-muted-foreground', badge: 'muted' },
  running: { label: '运行中', dot: 'bg-primary', badge: 'secondary' },
  done: { label: '完成', dot: 'bg-success', badge: 'success' },
  degraded: { label: '降级完成', dot: 'bg-warning', badge: 'warning' },
  failed: { label: '失败', dot: 'bg-destructive', badge: 'destructive' },
  paused: { label: '已暂停', dot: 'bg-warning', badge: 'warning' },
  needs_input: { label: '需补充材料', dot: 'bg-warning', badge: 'warning' },
};

export function StatusDot({
  state,
  label,
  className,
}: {
  state: StatusState;
  label?: React.ReactNode;
  className?: string;
}) {
  const config = statusConfig[state];
  return (
    <span className={cn('inline-flex items-center gap-2 text-meta', className)}>
      <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', config.dot)} aria-hidden="true" />
      <span>{label ?? config.label}</span>
    </span>
  );
}

export function StatusBadge({
  state,
  label,
  className,
}: {
  state: StatusState;
  label?: React.ReactNode;
  className?: string;
}) {
  const config = statusConfig[state];
  return (
    <Badge variant={config.badge} className={cn('text-micro', className)}>
      {label ?? config.label}
    </Badge>
  );
}
