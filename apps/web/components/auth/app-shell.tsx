'use client';

import { Suspense } from 'react';
import { FlaskConical, LoaderCircle } from 'lucide-react';
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
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background px-4 py-12">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,hsl(var(--primary)/0.12),transparent_40%)]" />
      <div className="relative w-full max-w-md space-y-6">
        <div className="flex items-center justify-center gap-2">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <FlaskConical className="h-5 w-5" />
          </div>
          <div>
            <div className="font-serif text-xl font-semibold">PaperForge</div>
            <div className="text-xs text-muted-foreground">成稿优先 · 引用真实</div>
          </div>
        </div>
        {children}
      </div>
    </main>
  );
}
