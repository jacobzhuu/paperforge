'use client';

import * as React from 'react';
import { CheckCircle2, Cpu, UserRound, XCircle } from 'lucide-react';
import { PageContainer } from '@/components/layout/page-container';
import { PageHeader } from '@/components/layout/page-header';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/toast';
import { DataSourceBanner } from '@/components/data-source-banner';
import { LoadState } from '@/components/layout/load-state';
import { getAcademicProfile, getRuntimeSettings, updateAcademicProfile } from '@/lib/api';
import type { AuthorDetail, DataSource, RuntimeSettings } from '@/lib/types';
import { describeError } from '@/lib/errors';

/**
 * 全局设置。
 *
 * 成本面板与版本历史已移到项目概览（`/projects/[id]`）——它们是**项目级**数据，
 * 放在全局设置里既要求页面自己去猜当前项目，又让用户在两处找同一件事。
 * 这里只留真正全局的运行时配置。
 */
export function SettingsPage() {
  const { toast } = useToast();
  const [settings, setSettings] = React.useState<RuntimeSettings | undefined>();
  const [source, setSource] = React.useState<DataSource>('live');
  const [note, setNote] = React.useState<string | undefined>();
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [reloadToken, setReloadToken] = React.useState(0);

  React.useEffect(() => {
    let alive = true;
    getRuntimeSettings()
      .then((runtime) => {
        if (!alive) return;
        setSettings(runtime.data);
        setSource(runtime.source);
        setNote(runtime.note);
        setLoadError(null);
        setLoading(false);
      })
      .catch((err) => {
        if (!alive) return;
        setLoadError(describeError(err));
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [reloadToken]);

  // settings 为 undefined 时整页会渲染一卡片破折号和空表头，
  // 与「后端真的没配」无法区分——当成加载失败处理。
  const effectiveError =
    loadError ?? (!loading && !settings ? '后端未返回运行时配置，无法确认 LLM 是否可用。' : null);

  return (
    <PageContainer>
      <div className="space-y-6">
        <PageHeader title="设置" description="LLM 运行时与模型角色路由" />

        <DataSourceBanner source={source} note={note} />

        <AcademicProfileCard toast={toast} />

        <LoadState
          loading={loading}
          error={effectiveError}
          onRetry={() => {
            setLoading(true);
            setReloadToken((t) => t + 1);
          }}
        >
          <div className="space-y-6">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center gap-1.5 text-sm">
                  <Cpu className="h-4 w-4" /> LLM 运行时
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-xs">
                  <span>
                    provider：<span className="font-mono">{settings?.llm_provider ?? '—'}</span>
                  </span>
                  <span className="inline-flex items-center gap-1">
                    API Key：
                    {settings?.llm_api_key_configured ? (
                      <Badge variant="success">已配置</Badge>
                    ) : (
                      <Badge variant="muted">未配置</Badge>
                    )}
                  </span>
                  <span className="inline-flex items-center gap-1">
                    状态：
                    {settings?.llm_enabled ? (
                      <Badge variant="success">
                        <CheckCircle2 className="mr-1 h-3 w-3" /> 已启用
                      </Badge>
                    ) : (
                      <Badge variant="warning">
                        <XCircle className="mr-1 h-3 w-3" /> 未启用（全部走确定性回退）
                      </Badge>
                    )}
                  </span>
                </div>
                <p className="rounded-md bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
                  配置方式：编辑仓库根目录的 <code>.env</code>（<code>LLM_DEFAULT_PROVIDER</code>、
                  <code>LLM_OPENAI_BASE_URL</code>、<code>LLM_OPENAI_API_KEY</code>、
                  <code>LLM_ROLE_MODELS</code>），然后重启 <code>./scripts/dev up</code>。
                  密钥只存在于服务端环境，界面永不回传。
                </p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">图片与图表生成</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-xs text-muted-foreground">
                <Row label="确定性视觉" value={settings?.visuals_enabled ? '已启用' : '已关闭'} />
                <Row
                  label="AI 插图"
                  value={settings?.ai_images_enabled ? '已启用（需人工触发）' : '已关闭'}
                />
                <Row
                  label="图像模型"
                  value={`${settings?.image_provider ?? '—'} / ${settings?.image_model ?? '—'}`}
                />
                <Row
                  label="远程 API 密钥"
                  value={settings?.image_api_key_configured ? '已配置' : '未配置'}
                />
                <Row
                  label="生图配置"
                  value={settings?.image_provider_configured ? '完整' : '待补全'}
                />
                <p className="border-t pt-2">
                  图像密钥与文本模型密钥相互独立；未配置时，数据图表和学术示意图仍可正常使用。
                </p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">模型角色路由</CardTitle>
              </CardHeader>
              <CardContent className="px-0 pb-0">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-24">角色</TableHead>
                      <TableHead className="w-44">当前模型</TableHead>
                      <TableHead>用途</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {(settings?.roles ?? []).map((role) => (
                      <TableRow key={role.role}>
                        <TableCell className="font-medium">{role.role}</TableCell>
                        <TableCell className="font-mono text-xs">{role.model}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {role.description}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">其他运行时</CardTitle>
              </CardHeader>
              <CardContent className="space-y-1.5 text-xs text-muted-foreground">
                <Row label="对象存储" value={settings?.storage_backend ?? '—'} />
                <Row
                  label="Semantic Scholar Key"
                  value={settings?.semantic_scholar_key_configured ? '已配置' : '未配置（走匿名限额）'}
                />
                <Row
                  label="检索联系邮箱"
                  value={settings?.scholar_contact_email_configured ? '已配置' : '未配置'}
                />
                <p className="border-t pt-2">
                  项目的成本面板与版本历史已移到各项目的<span className="font-medium text-foreground">概览</span>页。
                </p>
              </CardContent>
            </Card>
          </div>
        </LoadState>
      </div>
    </PageContainer>
  );
}

function AcademicProfileCard({
  toast,
}: {
  toast: (input: { title: string; description?: string; variant?: 'success' | 'error' | 'info' }) => void;
}) {
  const [profile, setProfile] = React.useState<AuthorDetail>({
    id: 'my-academic-profile',
    name: '',
    affiliations: [],
    email: null,
    orcid: null,
    corresponding: false,
  });
  const [affiliations, setAffiliations] = React.useState('');
  const [loading, setLoading] = React.useState(true);
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => {
    let alive = true;
    getAcademicProfile()
      .then(({ profile: stored }) => {
        if (!alive || !stored) return;
        setProfile(stored);
        setAffiliations(stored.affiliations.join('\n'));
      })
      .catch(() => {
        // 身份档案是独立能力；运行时设置仍应继续显示。
      })
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, []);

  const save = async () => {
    if (!profile.name.trim()) {
      toast({ title: '请填写用于论文署名的姓名', variant: 'error' });
      return;
    }
    if (profile.corresponding && !profile.email) {
      toast({ title: '通讯作者需要填写邮箱', variant: 'error' });
      return;
    }
    setSaving(true);
    try {
      const result = await updateAcademicProfile({
        ...profile,
        name: profile.name.trim(),
        affiliations: affiliations.split('\n').map((item) => item.trim()).filter(Boolean),
      });
      if (result.profile) {
        setProfile(result.profile);
        setAffiliations(result.profile.affiliations.join('\n'));
      }
      toast({ title: '学术身份已保存', description: '以后可在项目署名中一键添加本人。', variant: 'success' });
    } catch (error) {
      toast({ title: '学术身份未保存', description: describeError(error), variant: 'error' });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-1.5 text-sm">
          <UserRound className="h-4 w-4" /> 我的学术身份
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-xs text-muted-foreground">
          填写一次即可在每个项目中复用；项目会保存独立快照，之后修改这里不会改写旧论文署名。
        </p>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="profile-name">姓名</Label>
            <Input
              id="profile-name"
              value={profile.name}
              onChange={(event) => setProfile((current) => ({ ...current, name: event.target.value }))}
              disabled={loading}
              className="h-11"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="profile-email">邮箱</Label>
            <Input
              id="profile-email"
              type="email"
              value={profile.email ?? ''}
              onChange={(event) => setProfile((current) => ({ ...current, email: event.target.value || null }))}
              disabled={loading}
              className="h-11"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="profile-affiliations">单位</Label>
            <Textarea
              id="profile-affiliations"
              value={affiliations}
              onChange={(event) => setAffiliations(event.target.value)}
              placeholder="每行一个单位"
              disabled={loading}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="profile-orcid">ORCID</Label>
            <Input
              id="profile-orcid"
              value={profile.orcid ?? ''}
              onChange={(event) => setProfile((current) => ({ ...current, orcid: event.target.value || null }))}
              placeholder="0000-0002-1825-0097"
              disabled={loading}
              className="h-11"
            />
          </div>
        </div>
        <div className="flex flex-col gap-3 border-t pt-4 sm:flex-row sm:items-center sm:justify-between">
          <label className="flex min-h-11 items-center gap-3 text-sm">
            <Checkbox
              checked={profile.corresponding}
              onCheckedChange={(corresponding) => setProfile((current) => ({ ...current, corresponding }))}
              disabled={loading}
              aria-label="默认作为通讯作者"
            />
            默认作为通讯作者
          </label>
          <Button className="h-11" onClick={() => void save()} disabled={loading || saving}>
            {saving ? '正在保存…' : '保存学术身份'}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span>{label}</span>
      <span className="truncate font-mono text-foreground">{value}</span>
    </div>
  );
}
