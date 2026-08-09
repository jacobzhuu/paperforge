'use client';

import * as React from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';

/** 应用级 error boundary。此前 app/ 下一个都没有：任何渲染异常直接白屏。 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  React.useEffect(() => {
    console.error('[PaperForge] 页面渲染失败', error);
  }, [error]);

  return (
    <div
      role="alert"
      className="flex flex-col items-center justify-center gap-4 rounded-lg border border-destructive/40 bg-destructive/5 px-6 py-20 text-center"
    >
      <AlertTriangle className="h-8 w-8 text-destructive-strong" />
      <div>
        <h1 className="text-lg font-semibold">这个页面出错了</h1>
        <p className="mt-1 max-w-md text-sm text-muted-foreground">
          {error.message || '发生了未预期的错误。'}
        </p>
        {error.digest && (
          <p className="mt-1 font-mono text-xs text-muted-foreground">digest: {error.digest}</p>
        )}
      </div>
      <Button onClick={reset}>
        <RefreshCw className="h-4 w-4" /> 重试
      </Button>
    </div>
  );
}
