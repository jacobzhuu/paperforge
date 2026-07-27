import { redirectLegacyRoute } from '@/lib/legacy-redirect';

/** 旧扁平路由 → /projects/[id]/write。 */
export default async function LegacywritePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  await redirectLegacyRoute(searchParams, 'write');
}
