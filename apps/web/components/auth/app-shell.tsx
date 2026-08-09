'use client';

import { Suspense } from 'react';
import { Check, FlaskConical, LoaderCircle } from 'lucide-react';
import { usePathname } from 'next/navigation';
import { Sidebar } from '@/components/layout/sidebar';
import { isPublicAuthPath, useAuth } from './auth-provider';

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? '/';
  const { user, loading } = useAuth();
  if (isPublicAuthPath(pathname)) return <>{children}</>;
  if (loading || !user) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-muted-foreground">
        <div className="flex items-center gap-2 text-sm">
          <LoaderCircle className="h-4 w-4 animate-spin" /> 正在验证会话…
        </div>
      </div>
    );
  }
  return (
    <div className="flex min-h-screen">
      <Suspense fallback={<div className="hidden w-56 shrink-0 border-r bg-card md:block" />}>
        <Sidebar />
      </Suspense>
      <main className="min-w-0 flex-1 overflow-x-hidden pt-14 md:pt-0">{children}</main>
    </div>
  );
}

export function AuthPageShell({ children }: { children: React.ReactNode }) {
  return (
    <main className="relative isolate flex min-h-screen items-center justify-center overflow-hidden bg-background px-4 py-8 sm:px-6 lg:px-10 lg:py-12">
      <div className="pointer-events-none absolute inset-0" aria-hidden="true">
        <div className="auth-grid absolute inset-0 opacity-45 dark:opacity-20" />
        <div className="auth-glow auth-glow-one" />
        <div className="auth-glow auth-glow-two" />
        <div className="absolute inset-x-0 bottom-0 h-40 bg-gradient-to-t from-background to-transparent" />
      </div>

      <div className="relative grid w-full max-w-6xl items-center gap-12 lg:grid-cols-[minmax(0,28rem),minmax(0,1fr)] lg:gap-20">
        <section className="auth-enter space-y-6" aria-label="账户访问">
          <div className="flex items-center justify-center gap-2.5 lg:justify-start">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-primary/10 bg-primary text-primary-foreground shadow-sm">
              <FlaskConical className="h-5 w-5" aria-hidden="true" />
            </div>
            <div>
              <div className="font-serif text-xl font-semibold tracking-tight">PaperForge</div>
              <div className="text-xs tracking-wide text-muted-foreground">成稿优先 · 引用真实</div>
            </div>
          </div>

          <div className="mx-auto max-w-sm text-center lg:hidden">
            <h1 className="font-serif text-xl font-semibold leading-snug">让研究从材料出发，在证据中成稿。</h1>
            <p className="mt-2 text-sm leading-relaxed text-muted-foreground">可信材料进入，能被审阅的论文交付。</p>
          </div>

          {children}
        </section>

        <aside className="hidden lg:block" aria-label="PaperForge 产品理念">
          <div className="auth-enter auth-enter-delay">
            <p className="text-xs font-semibold uppercase tracking-[0.22em] text-muted-foreground">
              Paper first · Evidence always
            </p>
            <h1 className="mt-4 max-w-xl font-serif text-[2.6rem] font-semibold leading-[1.14] tracking-[-0.025em]">
              让研究从材料出发，<br />在证据中成稿。
            </h1>
            <p className="mt-5 max-w-xl text-[15px] leading-7 text-muted-foreground">
              PaperForge 将研究问题、可信文献与实验材料组织成一篇可追溯、可编辑、可交付的论文。
            </p>

            <div className="relative mt-8 max-w-lg" aria-hidden="true">
              <div className="auth-manuscript rounded-xl border border-border/80 bg-card/90 p-6 shadow-[0_24px_70px_-34px_hsl(var(--foreground)/0.35)] backdrop-blur-sm">
                <div className="flex items-center justify-between border-b pb-3 text-[11px] tracking-wide text-muted-foreground">
                  <span>研究稿件 · 工作副本</span>
                  <span className="inline-flex items-center gap-1 text-success-strong">
                    <Check className="h-3 w-3" /> 证据已同步
                  </span>
                </div>
                <p className="mt-5 max-w-sm font-serif text-xl font-semibold leading-snug">
                  可信证据如何支撑可审阅的学术表达
                </p>
                <div className="mt-5 space-y-2.5">
                  <div className="h-1.5 w-full rounded-full bg-muted" />
                  <div className="h-1.5 w-[92%] rounded-full bg-muted" />
                  <div className="h-1.5 w-[76%] rounded-full bg-muted" />
                </div>
                <div className="mt-5 flex items-center gap-2 border-t pt-3 text-[11px] text-muted-foreground">
                  <span className="rounded-full bg-accent px-2 py-0.5 text-accent-foreground">12 篇文献</span>
                  <span>引用与原始材料保持关联</span>
                </div>
              </div>
              <span className="auth-citation auth-citation-one">[01]</span>
              <span className="auth-citation auth-citation-two">[02]</span>
            </div>

            <ol className="mt-9 grid max-w-xl grid-cols-3 border-t pt-5 text-sm">
              <li className="auth-stage pr-4">
                <span className="text-xs text-muted-foreground">01</span>
                <strong className="mt-1 block font-medium">汇入材料</strong>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">问题、文献、数据与代码</span>
              </li>
              <li className="auth-stage border-l px-4">
                <span className="text-xs text-muted-foreground">02</span>
                <strong className="mt-1 block font-medium">循证写作</strong>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">引用有出处，结论可回溯</span>
              </li>
              <li className="auth-stage border-l pl-4">
                <span className="text-xs text-muted-foreground">03</span>
                <strong className="mt-1 block font-medium">灵活交付</strong>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">导出 PDF、Word 与 LaTeX</span>
              </li>
            </ol>
          </div>
        </aside>
      </div>
    </main>
  );
}
