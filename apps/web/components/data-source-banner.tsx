import { Info } from 'lucide-react';
import type { DataSource } from '@/lib/types';
import { cn } from '@/lib/utils';

export function DataSourceBanner({
  source,
  note,
  className,
}: {
  source: DataSource;
  note?: string;
  className?: string;
}) {
  if (source !== 'mock') return null;
  return (
    <div
      role="status"
      className={cn(
        'flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning-foreground',
        className,
      )}
    >
      <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>
        {/* 这是全应用最需要被看见的一句话，此前写成 Markdown 星号，用户字面读到 **不是**。 */}
        示例数据预览：{note ?? '后端不可用'}。当前展示的<strong className="font-semibold">不是</strong>
        你的真实数据，后端恢复后刷新即可。
      </span>
    </div>
  );
}
