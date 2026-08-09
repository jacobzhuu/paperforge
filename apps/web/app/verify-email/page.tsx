'use client';

import * as React from 'react';
import Link from 'next/link';
import { AuthPageShell } from '@/components/auth/app-shell';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { verifyEmail } from '@/lib/api';

export default function VerifyEmailPage() {
  const [state, setState] = React.useState<'loading' | 'success' | 'error'>('loading');
  React.useEffect(() => {
    const token = new URLSearchParams(window.location.search).get('token');
    window.history.replaceState(null, '', '/verify-email');
    if (!token) {
      setState('error');
      return;
    }
    verifyEmail(token).then(() => setState('success')).catch(() => setState('error'));
  }, []);
  return (
    <AuthPageShell>
      <Card className="auth-card"><CardHeader><CardTitle className="font-serif text-2xl leading-tight">{state === 'loading' ? '正在验证…' : state === 'success' ? '邮箱已验证' : '链接无效或已过期'}</CardTitle><CardDescription>{state === 'success' ? '现在可以登录 PaperForge' : '验证链接只能使用一次'}</CardDescription></CardHeader><CardContent className="text-center text-sm text-muted-foreground">{state === 'loading' ? '请稍候。' : <Link href={state === 'success' ? '/login' : '/register'} className="text-primary hover:underline">{state === 'success' ? '前往登录' : '重新注册或发送验证邮件'}</Link>}</CardContent></Card>
    </AuthPageShell>
  );
}
