'use client';

import * as React from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

/**
 * 单个数据模块的失败条。
 *
 * 与 `LoadState` 的区别是它**不接管整块内容**：调用方在 `AsyncModule` 失败时把它
 * 渲染在自己那一小块位置上，页面其余部分照常显示。素材页的 NUMLINT 挂掉时该
 * 出现的是这一条，而不是整页的「加载失败」。
 */
export function ModuleError({
  label,
  error,
  onRetry,
  className,
}: {
  /** 出问题的模块名，让用户知道**什么**没加载出来，而不只是「失败了」。 */
  label: string;
  error: string | null;
  onRetry?: () => void;
  className?: string;
}) {
  if (!error) return null;
  return (
    <div
      role="alert"
      className={cn(
        'flex flex-wrap items-center justify-between gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs',
        className,
      )}
    >
      <span className="flex min-w-0 items-center gap-2 text-warning-foreground">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
        <span className="min-w-0">
          {label}未能加载：{error}
        </span>
      </span>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RefreshCw className="h-3.5 w-3.5" /> 重试
        </Button>
      )}
    </div>
  );
}
