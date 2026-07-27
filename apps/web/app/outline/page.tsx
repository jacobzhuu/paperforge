import { redirectLegacyRoute } from '@/lib/legacy-redirect';

/** 旧扁平路由 → /projects/[id]/outline。 */
export default async function LegacyoutlinePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  await redirectLegacyRoute(searchParams, 'outline');
}
