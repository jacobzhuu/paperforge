import { SettingsPage } from '@/components/settings/settings-page';

/** 设置已去项目化，不再读 `?project=`，因此不需要 Suspense 边界。 */
export default function Settings() {
  return <SettingsPage />;
}
