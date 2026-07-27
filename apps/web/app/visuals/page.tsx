import { redirectLegacyRoute } from '@/lib/legacy-redirect';

/** 旧扁平路由 → /projects/[id]/visuals。 */
export default async function LegacyVisualsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  await redirectLegacyRoute(searchParams, 'visuals');
}
