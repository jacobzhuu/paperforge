'use client';

import * as React from 'react';
import { AlertTriangle, Check, ChevronDown, Loader2, RotateCw, SkipForward, WifiOff } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { Button } from '@/components/ui/button';
import { stageLabel } from '@/lib/labels';
import { cn } from '@/lib/utils';
import type { TrackedJob } from '@/lib/useJobTracker';

function useElapsed(startedAt: number): string {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const total = Math.max(0, Math.floor((now - startedAt) / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes > 0 ? `${minutes}分${String(seconds).padStart(2, '0')}秒` : `${seconds}秒`;
}

/**
 * 一键全管线的阶段序列（worker.py::run_full_pipeline）。
 * 单阶段任务不走这条序列，届时只显示当前阶段本身。
 */
const FULL_PIPELINE_STAGES = [
  'scope',
  'search',
  'curate',
  'ingest',
  'cards',
  'outline',
  'write',
  'polish',
  'quality',
  'visual_plan',
  'render',
];

/** 可以单独重跑的阶段——有独立端点的才给按钮，没有的不画假按钮。 */
export type RetryableStage = 'search' | 'ingest' | 'snowball' | 'cards' | 'quality' | 'outline' | 'write' | 'render';

const RETRYABLE = new Set<string>([
  'search',
  'ingest',
  'snowball',
  'cards',
  'quality',
  'outline',
  'write',
  'render',
]);

/**
 * 长任务进度卡。检索/大纲/写作/编译共用一份，挂在项目 shell 上跨页面存活。
 *
 * 这些任务动辄数分钟，所以除了百分比还必须给出：当前阶段的中文名、正在处理的对象、
 * 已用时长、以及 draft-first 下被降级的阶段——降级是静默继续的，不显示等于没发生。
 *
 * 阶段序列可展开：一次「8 个检索源失败但 cards 还在跑」的运行，用户能逐阶段看出
 * 哪些成功、哪些降级，而不是只看到一条前进的百分比。
 */
export function JobProgressCard({
  tracked,
  className,
  onRetryStage,
  onSkipPolish,
}: {
  tracked: TrackedJob;
  className?: string;
  onRetryStage?: (stage: RetryableStage) => void;
  onSkipPolish?: () => void;
}) {
  const [showDetail, setShowDetail] = React.useState(false);
  const elapsed = useElapsed(tracked.startedAt);
  const percent = Math.round((tracked.job.progress ?? 0) * 100);
  const { warnings, reconnecting, degradedToPolling } = tracked;

  const isFull = tracked.job.kind === 'full';
  const currentStage = tracked.job.stage ?? '';
  const failedStages = new Set(warnings.map((w) => w.stage));
  const currentIndex = FULL_PIPELINE_STAGES.indexOf(currentStage);
  // 润色是唯一「稿子已经完整、只是在打磨」的阶段，所以也是唯一可以随时喊停的。
  const polishing = currentStage === 'polish' && Boolean(onSkipPolish);

  return (
    <Card className={className}>
      <CardContent className="py-3">
        <div className="flex items-center gap-3">
          {reconnecting ? (
            <WifiOff className="h-4 w-4 shrink-0 text-warning-strong" />
          ) : (
            <Loader2 className="h-4 w-4 shrink-0 animate-spin text-muted-foreground" />
          )}
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-3 text-xs">
              {/* 阶段变化要被读屏播报——这是长任务里唯一的进度反馈。 */}
              <span className="truncate" role="status" aria-live="polite">
                <span className="font-medium">{tracked.label}</span>
                {tracked.detail && (
                  <span className="ml-1.5 text-muted-foreground">{tracked.detail}</span>
                )}
              </span>
              <span className="flex shrink-0 items-center gap-2">
                {polishing && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 px-2 text-xs"
                    disabled={tracked.polishSkipRequested}
                    onClick={onSkipPolish}
                  >
                    <SkipForward className="h-3 w-3" />
                    {tracked.polishSkipRequested ? '本节后停止' : '跳过润色'}
                  </Button>
                )}
                <span className="tabular-nums text-muted-foreground">
                  {elapsed} · {percent}%
                </span>
              </span>
            </div>
            <Progress
              value={percent}
              className="mt-1.5"
              aria-label={`${tracked.label} ${percent}%`}
            />
          </div>
          {(isFull || warnings.length > 0) && (
            <button
              type="button"
              onClick={() => setShowDetail((v) => !v)}
              aria-expanded={showDetail}
              aria-label="展开阶段详情"
              className="shrink-0 rounded p-1 text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <ChevronDown className={cn('h-4 w-4 transition-transform', showDetail && 'rotate-180')} />
            </button>
          )}
        </div>

        {/* 润色期间所有章节都已「已生成」，不解释一句就会被当成卡死。 */}
        {polishing && !reconnecting && (
          <p className="mt-2 text-xs text-muted-foreground">
            正文各节已生成并保存，正在逐节做连贯性润色
            {tracked.polishSkipRequested
              ? '——已请求跳过，当前这节写完即停。'
              : '；不想等可以跳过，剩余章节直接交付初稿。'}
          </p>
        )}

        {reconnecting && (
          <p className="mt-2 text-xs text-warning-foreground" role="status">
            {degradedToPolling
              ? '进度流已断开，正在改用轮询获取状态——任务仍在服务端运行。'
              : '进度流中断，正在重连……任务仍在服务端运行。'}
          </p>
        )}

        {/* 一句话摘要常驻：降级必须在不展开的情况下也能看见。 */}
        {warnings.length > 0 && !showDetail && (
          <button
            type="button"
            onClick={() => setShowDetail(true)}
            className="mt-2 flex w-full items-center gap-1.5 border-t pt-2 text-xs text-warning-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning-strong" />
            <span>{warnings.length} 个阶段已降级，稿件仍会产出</span>
          </button>
        )}

        {showDetail && (
          <div className="mt-2 space-y-2 border-t pt-2">
            {isFull && currentIndex >= 0 && (
              <ol className="flex flex-wrap gap-1.5">
                {FULL_PIPELINE_STAGES.map((stage, index) => {
                  const failed = failedStages.has(stage);
                  const done = index < currentIndex && !failed;
                  const running = index === currentIndex;
                  return (
                    <li
                      key={stage}
                      className={cn(
                        'flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs',
                        failed && 'border-warning/50 bg-warning/10 text-warning-foreground',
                        done && 'border-success/50 bg-success/10 text-success-strong',
                        running && 'border-primary bg-accent',
                        !failed && !done && !running && 'text-muted-foreground',
                      )}
                    >
                      {failed ? (
                        <AlertTriangle className="h-3 w-3" />
                      ) : done ? (
                        <Check className="h-3 w-3" />
                      ) : running ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : null}
                      {stageLabel(stage)}
                    </li>
                  );
                })}
              </ol>
            )}

            {warnings.length > 0 && (
              <ul className="space-y-1.5 text-xs text-muted-foreground">
                {warnings.map((w) => (
                  <li key={`${w.stage}-${w.reason}`} className="flex items-start gap-2">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning-strong" />
                    <span className="flex-1">
                      <span className="font-medium text-foreground">{stageLabel(w.stage)}</span>：
                      {w.reason}
                      {w.count !== undefined ? `（${w.count} 处）` : ''}
                      {w.message ? ` — ${w.message}` : ''}
                    </span>
                    {onRetryStage && RETRYABLE.has(w.stage) && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => onRetryStage(w.stage as RetryableStage)}
                      >
                        <RotateCw className="h-3 w-3" /> 重跑
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
