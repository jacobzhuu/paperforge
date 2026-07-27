'use client';

import * as React from 'react';
import Link from 'next/link';
import { AuthPageShell } from '@/components/auth/app-shell';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ApiError, registerAccount } from '@/lib/api';

export default function RegisterPage() {
  const [form, setForm] = React.useState({ displayName: '', email: '', password: '', confirm: '' });
  const [error, setError] = React.useState('');
  const [sent, setSent] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (form.password !== form.confirm) {
      setError('两次输入的密码不一致。');
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      await registerAccount({
        email: form.email,
        password: form.password,
        display_name: form.displayName || undefined,
      });
      setSent(true);
    } catch (cause) {
      setError(cause instanceof ApiError && cause.code === 'weak_password' ? '密码至少需要 8 个字符，且不能使用常见弱密码。' : '暂时无法注册，请稍后重试。');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AuthPageShell>
      <Card>
        <CardHeader>
          <CardTitle>{sent ? '检查邮箱' : '创建账户'}</CardTitle>
          <CardDescription>{sent ? '验证后即可进入独立工作空间' : '每个账户的项目与文件相互隔离'}</CardDescription>
        </CardHeader>
        <CardContent>
          {sent ? (
            <div className="space-y-4 text-sm text-muted-foreground">
              <p>如果该邮箱可以注册，我们已经发送了验证链接。链接将在 24 小时后失效。</p>
              {process.env.NODE_ENV === 'development' && (
                <p className="rounded-md bg-muted px-3 py-2 text-xs leading-relaxed">
                  本地开发不会发送真实邮件；请打开项目 data/auth-outbox 目录中的最新验证文件。
                </p>
              )}
              <Button variant="outline" className="w-full" onClick={() => setSent(false)}>更换邮箱</Button>
              <p className="text-center"><Link href="/login" className="text-primary hover:underline">返回登录</Link></p>
            </div>
          ) : (
            <form className="space-y-4" onSubmit={submit}>
              <div className="space-y-1.5"><Label htmlFor="name">显示名称</Label><Input id="name" autoComplete="name" value={form.displayName} onChange={(event) => setForm((value) => ({ ...value, displayName: event.target.value }))} /></div>
              <div className="space-y-1.5"><Label htmlFor="email">邮箱</Label><Input id="email" type="email" autoComplete="email" required value={form.email} onChange={(event) => setForm((value) => ({ ...value, email: event.target.value }))} /></div>
              <div className="space-y-1.5"><Label htmlFor="password">密码</Label><Input id="password" type="password" autoComplete="new-password" minLength={8} maxLength={128} required value={form.password} onChange={(event) => setForm((value) => ({ ...value, password: event.target.value }))} /><p className="text-xs text-muted-foreground">至少 8 个字符，可使用空格和中文；不要求固定字符组合。</p></div>
              <div className="space-y-1.5"><Label htmlFor="confirm">确认密码</Label><Input id="confirm" type="password" autoComplete="new-password" required value={form.confirm} onChange={(event) => setForm((value) => ({ ...value, confirm: event.target.value }))} /></div>
              {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
              <Button className="w-full" disabled={submitting}>{submitting ? '正在创建…' : '创建账户'}</Button>
              <p className="text-center text-sm text-muted-foreground">已有账户？ <Link href="/login" className="text-primary hover:underline">登录</Link></p>
            </form>
          )}
        </CardContent>
      </Card>
    </AuthPageShell>
  );
}
