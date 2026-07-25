import { Suspense } from 'react';
import { OutlineEditor } from '@/components/outline/outline-editor';

export default function OutlinePage() {
  return (
    <Suspense
      fallback={<div className="py-20 text-center text-sm text-muted-foreground">加载中…</div>}
    >
      <OutlineEditor />
    </Suspense>
  );
}
