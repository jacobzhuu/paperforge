import { Suspense } from 'react';
import { ExportCenter } from '@/components/export/export-center';

export default function ExportPage() {
  return (
    <Suspense
      fallback={<div className="py-20 text-center text-sm text-muted-foreground">加载中…</div>}
    >
      <ExportCenter />
    </Suspense>
  );
}
