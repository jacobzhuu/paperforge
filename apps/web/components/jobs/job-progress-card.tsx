'use client';

import * as React from 'react';
import {
  AlertTriangle,
  ChevronDown,
  Loader2,
  Pause,
  RotateCw,
  SkipForward,
  WifiOff,
  X,
} from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { Button } from '@/components/ui/button';
import { StatusDot, type StatusState } from '@/components/ui/status';
import { stageLabel } from '@/lib/labels';
import { RETRYABLE_STAGES, type RetryableStage } from '@/lib/pipeline';
import { cn } from '@/lib/utils';
import type { TrackedJob } from '@/lib/useJobTracker';
import type { Job } from '@/lib/types';

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

/** 把每秒变化的时钟隔离在最小子树里，避免整张任务卡和阶段详情一起重绘。 */
const ElapsedTime = React.memo(function ElapsedTime({ startedAt }: { startedAt: number }) {
  return <>{useElapsed(startedAt)}</>;
});

/**
 * 一键全管线的阶段序列（worker.py::run_full_pipeline）。
 * 单阶段任务不走这条序列，届时只显示当前阶段本身。
 */
const FULL_PIPELINE_STAGES = [
  'scope',
  'qdecomp',
  'search',
  'screen',
  'curate',
  'ingest',
  'cards',
  'evidence',
  'qmatrix',
  'synth',
  'outline',
  'write',
  'quality',
  'visual_plan',
  'render',
];

/**
 * 证据补充与收敛重写只在用户主动选了严谨/投稿档时才留在管线里；默认的
 * 「一次跑到稿」跑完一次质检就交付，修复由用户在概览页显式发起。
 *
 * 这四步必须按档位插入而不是常驻：列表把 currentIndex 之前的一律画成「已完成」，
 * 常驻就等于给每一次草稿运行凭空记上四个从没跑过的阶段。
 */
const QUALITY_REPAIR_STAGES = [
  'repair_search',
  'repair_ingest',
  'quality_repair',
  'quality_recheck',
];

function fullPipelineStages(job: Job): string[] {
  const resume = job.checkpoint?.resume as { kwargs?: { quality_profile?: string } } | undefined;
  // 档位缺失时按 draft 处理：默认入口不再传 scholarly，而旧任务的 checkpoint
  // 里本来就带着自己的档位，两边都不会被这条兜底改写。
  if ((resume?.kwargs?.quality_profile ?? 'draft') === 'draft') return FULL_PIPELINE_STAGES;
  const at = FULL_PIPELINE_STAGES.indexOf('quality');
  return [
    ...FULL_PIPELINE_STAGES.slice(0, at + 1),
    ...QUALITY_REPAIR_STAGES,
    ...FULL_PIPELINE_STAGES.slice(at + 1),
  ];
}

/** 可以单独重跑的阶段——有独立端点的才给按钮，没有的不画假按钮。 */
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
  onPause,
  onCancel,
}: {
  tracked: TrackedJob;
  className?: string;
  onRetryStage?: (stage: RetryableStage) => void;
  onSkipPolish?: () => void;
  onPause?: () => void;
  onCancel?: () => void;
}) {
  const [showDetail, setShowDetail] = React.useState(false);
  const percent = Math.round((tracked.job.progress ?? 0) * 100);
  const { warnings, reconnecting, degradedToPolling } = tracked;

  const isFull = tracked.job.kind === 'full';
  const currentStage = tracked.job.stage ?? '';
  const failedStages = new Set(warnings.map((w) => w.stage));
  const pipelineStages = fullPipelineStages(tracked.job);
  const currentIndex = pipelineStages.indexOf(currentStage);
  // 润色是唯一「稿子已经完整、只是在打磨」的阶段，所以也是唯一可以跳过收尾工序的。
  const polishing = currentStage === 'polish' && Boolean(onSkipPolish);
  // 停止已请求：按钮全部禁用，但进度条继续走——当前这一步还要跑完才真的停。
  // 用 Boolean 而不是 `!== null`：字段缺失（旧快照、测试替身）时不该被当成「正在停止」。
  const stopping = Boolean(tracked.stopRequested);

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
            <div className="flex items-center justify-between gap-3 text-body">
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
                    size="xs"
                    disabled={tracked.polishSkipRequested || stopping}
                    onClick={onSkipPolish}
                  >
                    <SkipForward className="h-3 w-3" />
                    {tracked.polishSkipRequested ? '本节后停止' : '跳过润色'}
                  </Button>
                )}
                {/* 暂停 / 取消常驻：这些任务动辄十几分钟，用户随时可能改主意，
                    而此前唯一的叫停手段只有润色阶段的「跳过润色」。 */}
                {onPause && (
                  <Button
                    variant="ghost"
                    size="xs"
                    disabled={stopping}
                    onClick={onPause}
                  >
                    <Pause className="h-3 w-3" />
                    暂停
                  </Button>
                )}
                {onCancel && (
                  <Button
                    variant="ghost"
                    size="xs"
                    disabled={stopping}
                    onClick={onCancel}
                  >
                    <X className="h-3 w-3" />
                    取消
                  </Button>
                )}
                <span className="text-meta tabular-nums text-muted-foreground">
                  <ElapsedTime startedAt={tracked.startedAt} /> · {percent}%
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
              className="flex h-11 w-11 shrink-0 items-center justify-center rounded text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:h-8 md:w-8"
            >
              <ChevronDown className={cn('h-4 w-4 transition-transform', showDetail && 'rotate-180')} />
            </button>
          )}
        </div>

        {/* 协作式停止要等当前这一步跑完，中间可能还有一两分钟。不说明就会被
            当成「点了没反应」，然后用户去点第二次、第三次。 */}
        {stopping && (
          <p className="mt-2 text-meta text-muted-foreground" role="status">
            {tracked.stopRequested === 'pause'
              ? '已请求暂停，当前这一步完成后停止；已生成的内容会保留，之后可从断点继续。'
              : '已请求取消，当前这一步完成后停止；已生成的内容会保留。'}
          </p>
        )}

        {/* 润色期间所有章节都已「已生成」，不解释一句就会被当成卡死。 */}
        {polishing && !reconnecting && !stopping && (
          <p className="mt-2 text-meta text-muted-foreground">
            正文各节已生成并保存，正在逐节做连贯性润色
            {tracked.polishSkipRequested
              ? '——已请求跳过，当前这节写完即停。'
              : '；不想等可以跳过，剩余章节直接交付初稿。'}
          </p>
        )}

        {reconnecting && (
          <p className="mt-2 text-meta text-warning-foreground" role="status">
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
            className="mt-2 flex min-h-11 w-full items-center gap-2 border-t pt-2 text-meta text-warning-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning-strong" />
            <span>{warnings.length} 个阶段已降级，稿件仍会产出</span>
          </button>
        )}

        {showDetail && (
          <div className="mt-2 space-y-2 border-t pt-2">
            {isFull && currentIndex >= 0 && (
              <ol className="flex flex-wrap gap-1.5">
                {pipelineStages.map((stage, index) => {
                  const failed = failedStages.has(stage);
                  const done = index < currentIndex && !failed;
                  const running = index === currentIndex;
                  const state: StatusState = failed
                    ? 'degraded'
                    : done
                      ? 'done'
                      : running
                        ? 'running'
                        : 'idle';
                  return (
                    <li key={stage}>
                      <StatusDot
                        state={state}
                        label={stageLabel(stage)}
                        className={cn(
                          'text-micro',
                          failed && 'text-warning-foreground',
                          running && 'font-medium text-foreground',
                        )}
                      />
                    </li>
                  );
                })}
              </ol>
            )}

            {warnings.length > 0 && (
              <ul className="space-y-2 text-meta text-muted-foreground">
                {warnings.map((w) => (
                  <li key={`${w.stage}-${w.reason}`} className="flex items-start gap-2">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning-strong" />
                    <span className="flex-1">
                      <span className="font-medium text-foreground">{stageLabel(w.stage)}</span>：
                      {w.reason}
                      {w.count !== undefined ? `（${w.count} 处）` : ''}
                      {w.message ? ` — ${w.message}` : ''}
                    </span>
                    {onRetryStage && RETRYABLE_STAGES.has(w.stage) && (
                      <Button
                        variant="ghost"
                        size="xs"
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
