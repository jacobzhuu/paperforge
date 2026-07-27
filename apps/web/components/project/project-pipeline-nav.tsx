'use client';

import * as React from 'react';
import Link from 'next/link';
import { ChevronDown } from 'lucide-react';
import {
  pipelineSteps,
  projectHref,
  type PipelineProgressMap,
  type PipelineStep,
  type PipelineStepId,
} from '@/lib/pipeline';
import type { PaperType, WritingMode } from '@/lib/types';
import { cn } from '@/lib/utils';

/**
 * 项目级管线导航，常驻于项目 shell。
 *
 * **视觉权重按写作模式分叉**（docs/ui-design.md §3.7 / §4.1）。
 *
 * 一种常见的建议是把整条管线弱化成一行「正在写作…」。对全自动模式那是对的，
 * 但对协作模式是错的：协作模式的定义就是「用户圈选入库、逐章确认」
 * （design.md §1.4 原则 4），把导航收起来等于在用户唯一需要介入的模式下
 * 拿走他的方向盘——他没法回文献工作台改入库集合，也没法在写作前修大纲。
 *
 *   - assisted：步骤常驻可见、可点，但降到最低视觉权重（去掉连接线与描边圆点，
 *     完成态只剩一个小圆点）。
 *   - auto：收成一行「第 4 / 6 步 · 写作工作台」，点开才展开完整步骤。
 *
 * 两种模式下都 gate-free：每一步都可点，不设前置校验。
 */
export function ProjectPipelineNav({
  projectId,
  paperType,
  writingMode,
  current,
  completion,
  running,
  badges,
}: {
  projectId: string;
  paperType: PaperType;
  writingMode: WritingMode;
  current: PipelineStepId;
  completion: PipelineProgressMap;
  /** 正在跑的阶段所属步骤，用于脉冲提示。 */
  running?: PipelineStepId | null;
  /** 步骤上的待办角标（视觉：待处理条数）与告警（生成失败）。 */
  badges?: Partial<Record<PipelineStepId, { count?: number; alert?: boolean; title?: string }>>;
}) {
  const steps = pipelineSteps(paperType);
  const [open, setOpen] = React.useState(false);

  const index = steps.findIndex((s) => s.id === current);
  const activeStep = index >= 0 ? steps[index] : steps[0];

  const list = (
    <ol className="scrollbar-thin flex items-center gap-0.5 overflow-x-auto pb-2">
      {steps.map((step) => (
        <li key={step.id} className="shrink-0">
          <StepLink
            step={step}
            projectId={projectId}
            active={step.id === current}
            done={completion[step.id]}
            running={running === step.id}
            badge={badges?.[step.id]}
          />
        </li>
      ))}
    </ol>
  );

  // 全自动模式：用户不介入，管线默认收起，只留一行位置感。
  if (writingMode === 'auto') {
    return (
      <nav aria-label="管线导航" className="border-b pb-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <StepDot done={completion[activeStep.id]} active running={running === activeStep.id} />
          <span className="font-medium text-foreground">{activeStep.label}</span>
          <span className="tabular-nums">
            第 {Math.max(index, 0) + 1} / {steps.length} 步
          </span>
          <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', open && 'rotate-180')} />
        </button>
        {open && list}
      </nav>
    );
  }

  return (
    <nav aria-label="管线导航" className="border-b">
      {list}
    </nav>
  );
}

function StepLink({
  step,
  projectId,
  active,
  done,
  running,
  badge,
}: {
  step: PipelineStep;
  projectId: string;
  active: boolean;
  done: boolean;
  running: boolean;
  badge?: { count?: number; alert?: boolean; title?: string };
}) {
  return (
    <Link
      href={projectHref(projectId, step.segment)}
      aria-current={active ? 'page' : undefined}
      title={badge?.title ?? step.hint}
      className={cn(
        'flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        active
          ? 'font-medium text-foreground'
          : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground',
      )}
    >
      <StepDot done={done} active={active} running={running} />
      <span className="whitespace-nowrap">{step.label}</span>
      {/*
        待办角标只在真有待办时出现。它与完成态的小圆点是两回事：圆点说
        「这一步有产物了」，角标说「这里还有事等你做」。
      */}
      {badge?.count ? (
        <span className="rounded-full bg-muted px-1.5 text-[11px] font-medium tabular-nums text-muted-foreground">
          {badge.count}
        </span>
      ) : null}
      {badge?.alert && (
        <span
          className="h-1.5 w-1.5 shrink-0 rounded-full bg-destructive"
          aria-label="有失败项"
        />
      )}
    </Link>
  );
}

/**
 * 状态点。
 *
 * 完成态此前是一个「浅绿底 + 对勾」的徽章：七个步骤里有五个完成时，导航条上
 * 就是五个绿色色块，比它们标注的步骤名还显眼。现在完成 = 一个实心小点，
 * 当前 = 一个描边圈，运行中 = 脉冲——颜色只在「正在跑」这一个状态上出现。
 */
function StepDot({ done, active, running }: { done: boolean; active: boolean; running: boolean }) {
  if (running) {
    return (
      <span className="relative flex h-2 w-2 shrink-0 items-center justify-center" aria-hidden>
        <span className="absolute h-2 w-2 animate-ping rounded-full bg-primary/40 motion-reduce:animate-none" />
        <span className="h-1.5 w-1.5 rounded-full bg-primary" />
      </span>
    );
  }
  return (
    <span
      className={cn(
        'h-1.5 w-1.5 shrink-0 rounded-full',
        done
          ? active
            ? 'bg-foreground'
            : 'bg-muted-foreground/70'
          : active
            ? 'border border-foreground'
            : 'border border-border',
      )}
      aria-hidden
    />
  );
}
