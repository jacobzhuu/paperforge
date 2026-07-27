'use client';

import * as React from 'react';
import Link from 'next/link';
import { AuthPageShell } from '@/components/auth/app-shell';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { resetPassword } from '@/lib/api';

export default function ResetPasswordPage() {
  const [token, setToken] = React.useState<string | null>(null);
  const [password, setPassword] = React.useState('');
  const [confirm, setConfirm] = React.useState('');
  const [error, setError] = React.useState('');
  const [done, setDone] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);
  React.useEffect(() => {
    setToken(new URLSearchParams(window.location.search).get('token'));
    window.history.replaceState(null, '', '/reset-password');
  }, []);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (password !== confirm) {
      setError('两次输入的密码不一致。');
      return;
    }
    if (!token) {
      setError('重置链接缺少令牌。');
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      await resetPassword(token, password);
      setDone(true);
    } catch {
      setError('链接无效或已过期，请重新申请。');
    } finally {
      setSubmitting(false);
    }
  };
  return (
    <AuthPageShell>
      <Card><CardHeader><CardTitle>{done ? '密码已更新' : '设置新密码'}</CardTitle><CardDescription>更新后所有旧会话都会失效</CardDescription></CardHeader><CardContent>{done ? <p className="text-center text-sm"><Link href="/login" className="text-primary hover:underline">使用新密码登录</Link></p> : <form className="space-y-4" onSubmit={submit}><div className="space-y-1.5"><Label htmlFor="password">新密码</Label><Input id="password" type="password" autoComplete="new-password" minLength={8} maxLength={128} required value={password} onChange={(event) => setPassword(event.target.value)} /></div><div className="space-y-1.5"><Label htmlFor="confirm">确认新密码</Label><Input id="confirm" type="password" autoComplete="new-password" required value={confirm} onChange={(event) => setConfirm(event.target.value)} /></div>{error && <p role="alert" className="text-sm text-destructive">{error}</p>}<Button className="w-full" disabled={submitting}>{submitting ? '正在更新…' : '更新密码'}</Button></form>}</CardContent></Card>
    </AuthPageShell>
  );
}
