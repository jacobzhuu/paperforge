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
      className={cn(
        'flex items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning-foreground',
        className,
      )}
    >
      <Info className="h-3.5 w-3.5 shrink-0" />
      <span>示例数据预览：{note ?? '对应后端端点尚未实现'}。端点落地后将自动切换为真实数据。</span>
    </div>
  );
}
