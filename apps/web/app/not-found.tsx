import Link from 'next/link';
import { Compass } from 'lucide-react';
import { buttonVariants } from '@/components/ui/button';

export default function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center gap-4 rounded-lg border border-dashed px-6 py-20 text-center">
      <Compass className="h-8 w-8 text-muted-foreground" />
      <div>
        <h1 className="text-lg font-semibold">页面不存在</h1>
        <p className="mt-1 text-sm text-muted-foreground">这个地址没有对应的工作台。</p>
      </div>
      <Link href="/projects" className={buttonVariants({ variant: 'outline' })}>
        回到项目列表
      </Link>
    </div>
  );
}
