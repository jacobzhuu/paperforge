import { redirectLegacyRoute } from '@/lib/legacy-redirect';

/** 旧扁平路由 → /projects/[id]/library。 */
export default async function LegacylibraryPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  await redirectLegacyRoute(searchParams, 'library');
}
