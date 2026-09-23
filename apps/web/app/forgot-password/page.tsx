'use client';

import * as React from 'react';
import Link from 'next/link';
import { AuthPageShell } from '@/components/auth/app-shell';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ApiError, requestPasswordReset } from '@/lib/api';

export default function ForgotPasswordPage() {
  const [email, setEmail] = React.useState('');
  const [sent, setSent] = React.useState(false);
  const [error, setError] = React.useState('');
  const [submitting, setSubmitting] = React.useState(false);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError('');
    try {
      await requestPasswordReset(email);
      setSent(true);
    } catch (cause) {
      setError(cause instanceof ApiError && cause.code === 'email_delivery_disabled' ? '暂未启用邮件找回密码，请联系管理员。' : '暂时无法发送重置邮件，请稍后重试。');
    } finally {
      setSubmitting(false);
    }
  };
  return (
    <AuthPageShell>
      <Card className="auth-card">
        <CardHeader><CardTitle className="font-serif text-2xl leading-tight">找回密码</CardTitle><CardDescription>重置链接将在 30 分钟后失效</CardDescription></CardHeader>
        <CardContent>
          {sent ? <div className="space-y-4 text-sm text-muted-foreground"><p>如果账户存在，我们已经发送了重置邮件。</p>{process.env.NODE_ENV === 'development' && <p className="rounded-md bg-muted px-3 py-2 text-xs leading-relaxed">本地开发不会发送真实邮件；请打开项目 data/auth-outbox 目录中的最新重置文件。</p>}<p className="text-center"><Link href="/login" className="text-primary hover:underline">返回登录</Link></p></div> : <form className="space-y-4" onSubmit={submit}><div className="space-y-1.5"><Label htmlFor="email">邮箱</Label><Input id="email" type="email" autoComplete="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></div>{error && <p role="alert" className="text-sm text-destructive">{error}</p>}<Button className="w-full" disabled={submitting}>{submitting ? '正在发送…' : '发送重置邮件'}</Button><p className="text-center text-sm"><Link href="/login" className="text-primary hover:underline">返回登录</Link></p></form>}
        </CardContent>
      </Card>
    </AuthPageShell>
  );
}
