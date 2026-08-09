import { Info } from 'lucide-react';
import type { DataSource } from '@/lib/types';
import { cn } from '@/lib/utils';
import { Callout } from '@/components/ui/callout';

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
    <Callout
      variant="warning"
      role="status"
      className={cn(
        'flex items-start gap-2',
        className,
      )}
    >
      <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>
        {/* 这是全应用最需要被看见的一句话，此前写成 Markdown 星号，用户字面读到 **不是**。 */}
        示例数据预览：{note ?? '后端不可用'}。当前展示的<strong className="font-semibold">不是</strong>
        你的真实数据，后端恢复后刷新即可。
      </span>
    </Callout>
  );
}
