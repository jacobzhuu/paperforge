import { Skeleton } from '@/components/ui/skeleton';

/**
 * 路由代码或服务端布局尚未就绪时保留页面结构，避免导航后出现空白闪屏。
 * 项目内导航通常由持久 layout 接管，这里也覆盖首页、项目列表和设置页。
 */
export default function AppLoading() {
  return (
    <div className="mx-auto w-full max-w-6xl space-y-5 px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
      <div className="space-y-2">
        <Skeleton className="h-7 w-56" />
        <Skeleton className="h-4 w-80 max-w-full" />
      </div>
      <Skeleton className="h-11 w-full" />
      <div className="grid gap-4 md:grid-cols-2">
        <Skeleton className="h-48" />
        <Skeleton className="h-48" />
      </div>
    </div>
  );
}
