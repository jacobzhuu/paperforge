import { Suspense } from 'react';
import { AssetsCenter } from '@/components/assets/assets-center';

export default function AssetsPage() {
  return (
    <Suspense
      fallback={<div className="py-20 text-center text-sm text-muted-foreground">加载中…</div>}
    >
      <AssetsCenter />
    </Suspense>
  );
}
