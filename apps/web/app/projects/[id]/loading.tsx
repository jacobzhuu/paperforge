import { Skeleton } from '@/components/ui/skeleton';

/** 项目 shell 会保持挂载，仅为切入的工作台提供稳定占位。 */
export default function ProjectWorkbenchLoading() {
  return (
    <div className="grid min-h-80 gap-4 lg:grid-cols-[minmax(0,1fr)_19rem]" aria-label="正在加载工作台">
      <div className="space-y-3">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
      <Skeleton className="hidden h-72 lg:block" />
    </div>
  );
}
