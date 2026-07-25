import { Suspense } from 'react';
import { LibraryWorkbench } from '@/components/library/library-workbench';

export default function LibraryPage() {
  return (
    <Suspense fallback={<div className="py-20 text-center text-sm text-muted-foreground">加载中…</div>}>
      <LibraryWorkbench />
    </Suspense>
  );
}
