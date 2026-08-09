'use client';

import * as React from 'react';
import Link from 'next/link';
import { AuthPageShell } from '@/components/auth/app-shell';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { resetPassword } from '@/lib/api';
import { PasswordField } from '@/components/auth/password-field';

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
      <Card className="auth-card"><CardHeader><CardTitle className="font-serif text-2xl leading-tight">{done ? '密码已更新' : '设置新密码'}</CardTitle><CardDescription>更新后所有旧会话都会失效</CardDescription></CardHeader><CardContent>{done ? <p className="text-center text-sm"><Link href="/login" className="text-primary hover:underline">使用新密码登录</Link></p> : <form className="space-y-4" onSubmit={submit}><PasswordField id="password" label="新密码" autoComplete="new-password" minLength={15} value={password} onChange={setPassword} hint="至少 15 个字符。" /><PasswordField id="confirm" label="确认新密码" autoComplete="new-password" minLength={15} value={confirm} onChange={setConfirm} />{error && <p role="alert" className="text-sm text-destructive">{error}</p>}<Button className="w-full" disabled={submitting}>{submitting ? '正在更新…' : '更新密码'}</Button></form>}</CardContent></Card>
    </AuthPageShell>
  );
}
