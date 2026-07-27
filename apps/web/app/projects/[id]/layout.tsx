import type { ReactNode } from 'react';
import { ProjectShell } from '@/components/project/project-shell';

/**
 * 项目工作区布局。
 *
 * 项目身份从此进入**路径**而不再是 `?project=` 查询参数——后者要求每一个站内
 * 跳转都手工拼接（旧 `lib/project-nav.ts::withProject`），漏一处就掉回
 * 「尚未选择项目」。App Router 的布局在子路由间不会重挂载，因此项目数据、
 * 白名单与任务订阅都能跨工作台存活。
 */
export default async function ProjectLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <ProjectShell projectId={id}>{children}</ProjectShell>;
}
