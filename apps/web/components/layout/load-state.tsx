'use client';

import * as React from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';

interface LoadStateProps {
  loading: boolean;
  error?: string | null;
  onRetry?: () => void;
  /** 加载骨架的高度类，如 'h-48'。 */
  skeletonClassName?: string;
  children: React.ReactNode;
}

/**
 * 加载 / 失败 / 内容 三态包装。
 *
 * 存在的理由：此前六个页面的加载失败都没有 catch，一次 500 就让骨架屏永久 pulse，
 * 用户既看不到原因也没有重试入口，只能刷新整页。
 */
export function LoadState({
  loading,
  error,
  onRetry,
  skeletonClassName = 'h-48',
  children,
}: LoadStateProps) {
  if (error) {
    return (
      <div
        role="alert"
        className="flex flex-col items-center justify-center gap-3 rounded-lg border border-destructive/40 bg-destructive/5 px-6 py-12 text-center"
      >
        <AlertTriangle className="h-6 w-6 text-destructive-strong" />
        <div>
          <p className="text-sm font-medium">加载失败</p>
          <p className="mt-1 max-w-md text-sm text-muted-foreground">{error}</p>
        </div>
        {onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RefreshCw className="h-4 w-4" /> 重试
          </Button>
        )}
      </div>
    );
  }
  if (loading) return <Skeleton className={skeletonClassName} />;
  return <>{children}</>;
}
