import * as React from 'react';
import { ActionMenu, type ActionMenuItem } from '@/components/ui/action-menu';
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
  overflowActions,
  className,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  /** 次要动作在窄屏统一收进菜单，避免页头换成多行按钮堆。 */
  overflowActions?: ActionMenuItem[];
  className?: string;
}) {
  return (
    <div className={cn('flex flex-wrap items-start justify-between gap-3', className)}>
      <div className="min-w-0 space-y-0.5">
        <h2 className="font-serif text-lg font-semibold tracking-tight">{title}</h2>
        {description && <p className="text-sm text-muted-foreground">{description}</p>}
      </div>
      {(actions || overflowActions?.length) && (
        <div className="flex flex-wrap items-center gap-2">
          {actions}
          {overflowActions && overflowActions.length > 0 && (
            <>
              <div className="hidden items-center gap-2 md:flex">
                {overflowActions.map((item) => {
                  const Icon = item.icon;
                  return (
                    <button
                      key={item.label}
                      type="button"
                      onClick={item.onSelect}
                      disabled={item.disabled}
                      className="inline-flex h-9 items-center gap-2 rounded-md px-3 text-body font-medium transition-colors hover:bg-accent disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <Icon className="h-4 w-4" />
                      {item.label}
                    </button>
                  );
                })}
              </div>
              <div className="md:hidden">
                <ActionMenu label="更多操作" items={overflowActions} />
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
