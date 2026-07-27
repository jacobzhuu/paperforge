'use client';

import * as React from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { AuthPageShell } from '@/components/auth/app-shell';
import { useAuth } from '@/components/auth/auth-provider';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ApiError } from '@/lib/api';

export default function LoginPage() {
  const router = useRouter();
  const { signIn } = useAuth();
  const [account, setAccount] = React.useState('');
  const [password, setPassword] = React.useState('');
  const [error, setError] = React.useState('');
  const [submitting, setSubmitting] = React.useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError('');
    try {
      await signIn(account, password);
      const requested = new URLSearchParams(window.location.search).get('next') ?? '/';
      const destination = requested.startsWith('/') && !requested.startsWith('//') ? requested : '/';
      router.replace(destination);
      router.refresh();
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === 'email_verification_required') {
        setError('邮箱尚未验证，请先打开验证邮件。');
      } else {
        setError('账号或密码不正确。');
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AuthPageShell>
      <Card>
        <CardHeader>
          <CardTitle>登录</CardTitle>
          <CardDescription>进入你的独立论文工作空间</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={submit}>
            <div className="space-y-1.5">
              <Label htmlFor="account">账号或邮箱</Label>
              <Input id="account" type="text" autoComplete="username" required value={account} onChange={(event) => setAccount(event.target.value)} />
            </div>
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label htmlFor="password">密码</Label>
                <Link href="/forgot-password" className="text-xs text-primary hover:underline">忘记密码？</Link>
              </div>
              <Input id="password" type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />
            </div>
            {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
            <Button className="w-full" disabled={submitting}>{submitting ? '正在登录…' : '登录'}</Button>
          </form>
          {process.env.NODE_ENV === 'development' && (
            <p className="mt-4 rounded-md bg-muted px-3 py-2 text-center text-xs text-muted-foreground">
              开发测试账号：admin · 密码：123456
            </p>
          )}
          <p className="mt-5 text-center text-sm text-muted-foreground">
            还没有账户？ <Link href="/register" className="text-primary hover:underline">注册</Link>
          </p>
        </CardContent>
      </Card>
    </AuthPageShell>
  );
}
