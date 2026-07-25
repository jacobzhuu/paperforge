import { Suspense } from 'react';
import { SettingsPage } from '@/components/settings/settings-page';

export default function Settings() {
  return (
    <Suspense
      fallback={<div className="py-20 text-center text-sm text-muted-foreground">加载中…</div>}
    >
      <SettingsPage />
    </Suspense>
  );
}
