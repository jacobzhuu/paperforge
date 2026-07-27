import * as React from 'react';
import { cn } from '@/lib/utils';

/**
 * 工作台标题栏。
 *
 * 与 `layout/page-header.tsx` 的区别：项目 shell 已经渲染了项目题目与管线导航，
 * 工作台不该再来一个 2xl 的大标题——那会让「项目叫什么」和「我在哪一步」
 * 抢同一层视觉权重。这里用较小的标题层级。
 */
export function WorkbenchHeader({
  title,
  description,
  actions,
  className,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('flex flex-wrap items-start justify-between gap-3', className)}>
      <div className="min-w-0 space-y-0.5">
        <h2 className="font-serif text-lg font-semibold tracking-tight">{title}</h2>
        {description && <p className="text-sm text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}
