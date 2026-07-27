import { redirectLegacyRoute } from '@/lib/legacy-redirect';

/** 旧扁平路由 → /projects/[id]/export。 */
export default async function LegacyexportPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  await redirectLegacyRoute(searchParams, 'export');
}
