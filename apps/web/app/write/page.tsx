import { Suspense } from 'react';
import { WritingWorkbench } from '@/components/writing/writing-workbench';

export default function WritePage() {
  return (
    <Suspense
      fallback={<div className="py-20 text-center text-sm text-muted-foreground">加载中…</div>}
    >
      <WritingWorkbench />
    </Suspense>
  );
}
