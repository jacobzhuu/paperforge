import * as React from 'react';
import { cn } from '@/lib/utils';

/**
 * 页面容器。
 *
 * 根布局此前把**每一个**页面都锁在 max-w-6xl；宽度现在按内容类型选择：
 * - `reading`：列表、设置、表单等以阅读为主的页面
 * - `wide`：需要三栏与大量并排信息的工作台
 */
export function PageContainer({
  width = 'reading',
  className,
  children,
}: {
  width?: 'reading' | 'wide';
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        'mx-auto w-full px-4 py-6 sm:px-6 lg:px-8 lg:py-8',
        width === 'wide' ? 'max-w-[1600px]' : 'max-w-6xl',
        className,
      )}
    >
      {children}
    </div>
  );
}
