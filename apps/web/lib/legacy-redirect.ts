import { redirect } from 'next/navigation';

/**
 * 旧的扁平路由（`/library?project=<id>` 等）重定向到项目子路由。
 *
 * 保留而不是直接删除：这些地址可能已经在用户的书签、终端历史与
 * `docs/` 的示例里。带不上项目 ID 时退回项目列表，而不是渲染一个
 * 「尚未选择项目」的空壳。
 */
export async function redirectLegacyRoute(
  searchParams: Promise<Record<string, string | string[] | undefined>>,
  segment: string,
): Promise<never> {
  const params = await searchParams;
  const raw = params?.project;
  const projectId = Array.isArray(raw) ? raw[0] : raw;
  if (!projectId) redirect('/projects');
  redirect(`/projects/${projectId}${segment ? `/${segment}` : ''}`);
}
