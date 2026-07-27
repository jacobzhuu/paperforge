'use client';

import * as React from 'react';
import { AlertTriangle, CheckCircle2, ChevronDown, Loader2, XCircle } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import type { SearchRun } from '@/lib/types';
import { cn } from '@/lib/utils';

/**
 * 检索统计。
 *
 * 两处改动：
 * - 头号数字从「命中」换成「已取回候选」。命中是各源报告的 total hits，
 *   实测一个项目显示 8 246 662——对用户没有任何决策价值，却占着最显眼的位置。
 * - 失败源聚合成一句话 + 可展开明细。draft-first 下部分源失败是常态，
 *   要让用户一眼看出「挂了几个、剩下的够不够用」，而不是在列表里逐条数。
 */
export function SearchStats({ runs }: { runs: SearchRun[] }) {
  const [showFailed, setShowFailed] = React.useState(false);

  const retrieved = runs.reduce((s, r) => s + r.retrieved_count, 0);
  const failed = runs.filter((r) => r.status === 'failed');
  const partial = runs.filter((r) => r.status === 'partial');
  const succeeded = runs.filter((r) => r.status === 'succeeded');
  const running = runs.filter((r) => r.status === 'running');

  if (runs.length === 0) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">检索统计</CardTitle>
        </CardHeader>
        <CardContent className="text-xs text-muted-foreground">还没有跑过检索。</CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm">检索统计</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div>
          <p className="text-2xl font-semibold tabular-nums">{retrieved.toLocaleString()}</p>
          <p className="text-xs text-muted-foreground">条候选文献已取回并去重</p>
        </div>

        <div className="flex flex-wrap gap-x-3 gap-y-1 border-t pt-2 text-xs">
          <span className="inline-flex items-center gap-1 text-success-strong">
            <CheckCircle2 className="h-3.5 w-3.5" /> {succeeded.length} 源成功
          </span>
          {partial.length > 0 && (
            <span className="inline-flex items-center gap-1 text-warning-strong">
              <AlertTriangle className="h-3.5 w-3.5" /> {partial.length} 源部分成功
            </span>
          )}
          {running.length > 0 && (
            <span className="inline-flex items-center gap-1 text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> {running.length} 源进行中
            </span>
          )}
        </div>

        {failed.length > 0 && (
          <div className="border-t pt-2">
            <button
              type="button"
              onClick={() => setShowFailed((v) => !v)}
              aria-expanded={showFailed}
              className="flex w-full items-start gap-1.5 rounded text-left text-xs text-warning-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning-strong" />
              <span className="flex-1">
                {failed.length} 个检索源失败，已用其余源的 {retrieved.toLocaleString()} 条结果继续
              </span>
              <ChevronDown
                className={cn(
                  'mt-0.5 h-3.5 w-3.5 shrink-0 transition-transform',
                  showFailed && 'rotate-180',
                )}
              />
            </button>
            {showFailed && (
              <ul className="mt-1.5 space-y-1 text-xs text-muted-foreground">
                {failed.map((run) => (
                  <li key={run.id} className="flex items-start gap-1.5">
                    <XCircle className="mt-0.5 h-3 w-3 shrink-0 text-destructive-strong" />
                    <span>
                      <span className="font-medium text-foreground">{run.provider}</span>
                      {run.error ? `：${run.error}` : ''}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        <ul className="space-y-1 border-t pt-2 text-xs">
          {succeeded.concat(partial).map((run) => (
            <li key={run.id} className="flex items-center gap-2">
              {run.status === 'succeeded' ? (
                <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success-strong" />
              ) : (
                <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning-strong" />
              )}
              <span className="min-w-0 flex-1 truncate font-medium">{run.provider}</span>
              <span className="shrink-0 tabular-nums text-muted-foreground">
                {run.retrieved_count}
              </span>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
