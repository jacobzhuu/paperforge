'use client';

import * as React from 'react';
import { useSearchParams } from 'next/navigation';
import { CheckCircle2, Cpu, History, Wallet, XCircle } from 'lucide-react';
import { PageHeader } from '@/components/layout/page-header';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { DataSourceBanner } from '@/components/data-source-banner';
import { getCostDetail, getRuntimeSettings, getVersionHistory } from '@/lib/api';
import type { CostDetail, DataSource, RuntimeSettings, VersionHistory } from '@/lib/types';
import { formatDate } from '@/lib/utils';

export function SettingsPage() {
  const params = useSearchParams();
  const projectId = params.get('project') ?? '';

  const [settings, setSettings] = React.useState<RuntimeSettings | undefined>();
  const [cost, setCost] = React.useState<CostDetail | undefined>();
  const [versions, setVersions] = React.useState<VersionHistory | undefined>();
  const [source, setSource] = React.useState<DataSource>('live');
  const [note, setNote] = React.useState<string | undefined>();
  const [loading, setLoading] = React.useState(true);

  React.useEffect(() => {
    let alive = true;
    const load = async () => {
      const runtime = await getRuntimeSettings();
      if (!alive) return;
      setSettings(runtime.data);
      setSource(runtime.source);
      setNote(runtime.note);
      if (projectId) {
        const [costResult, versionResult] = await Promise.all([
          getCostDetail(projectId),
          getVersionHistory(projectId),
        ]);
        if (!alive) return;
        setCost(costResult.data);
        setVersions(versionResult.data);
      }
      setLoading(false);
    };
    void load();
    return () => {
      alive = false;
    };
  }, [projectId]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="设置"
        description="模型角色路由、成本面板、版本历史"
      />

      <DataSourceBanner source={source} note={note} />

      {loading ? (
        <div className="h-48 animate-pulse rounded-xl border bg-muted/40" />
      ) : (
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
                <span>
                  base_url：<span className="font-mono">{settings?.llm_base_url ?? '—'}</span>
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

          {projectId && (
            <>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="flex items-center gap-1.5 text-sm">
                    <Wallet className="h-4 w-4" /> 成本面板
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="grid grid-cols-4 gap-3">
                    {[
                      { label: '调用次数', value: cost?.totals.call_count ?? 0 },
                      { label: '输入 tokens', value: cost?.totals.input_tokens ?? 0 },
                      { label: '输出 tokens', value: cost?.totals.output_tokens ?? 0 },
                      {
                        label: '失败调用',
                        value: cost?.totals.failed_call_count ?? 0,
                      },
                    ].map((metric) => (
                      <div key={metric.label} className="rounded-md border p-2 text-center">
                        <p className="text-lg font-semibold tabular-nums">
                          {typeof metric.value === 'number'
                            ? metric.value.toLocaleString()
                            : metric.value}
                        </p>
                        <p className="text-xs text-muted-foreground">{metric.label}</p>
                      </div>
                    ))}
                  </div>
                  {(cost?.by_role ?? []).length > 0 && (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>角色</TableHead>
                          <TableHead>模型</TableHead>
                          <TableHead className="w-20">次数</TableHead>
                          <TableHead className="w-24">输入</TableHead>
                          <TableHead className="w-24">输出</TableHead>
                          <TableHead className="w-24">平均延迟</TableHead>
                          <TableHead className="w-20">失败</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {cost!.by_role.map((row, index) => (
                          <TableRow key={index}>
                            <TableCell>{row.role}</TableCell>
                            <TableCell className="font-mono text-xs">{row.model}</TableCell>
                            <TableCell className="tabular-nums">{row.call_count}</TableCell>
                            <TableCell className="tabular-nums">{row.input_tokens}</TableCell>
                            <TableCell className="tabular-nums">{row.output_tokens}</TableCell>
                            <TableCell className="tabular-nums">{row.avg_latency_ms} ms</TableCell>
                            <TableCell className="tabular-nums">
                              {row.failed_call_count ? (
                                <Badge variant="warning">{row.failed_call_count}</Badge>
                              ) : (
                                0
                              )}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="flex items-center gap-1.5 text-sm">
                    <History className="h-4 w-4" /> 版本历史
                  </CardTitle>
                </CardHeader>
                <CardContent className="grid gap-6 md:grid-cols-2">
                  <div>
                    <p className="mb-2 text-xs font-medium text-muted-foreground">文稿版本</p>
                    <ul className="space-y-1 text-xs">
                      {(versions?.documents ?? []).map((doc) => (
                        <li key={doc.id} className="flex items-center gap-2">
                          <Badge variant={doc.is_current ? 'success' : 'outline'}>
                            v{doc.version}
                          </Badge>
                          <span className="text-muted-foreground">
                            {doc.section_count != null ? `${doc.section_count} 节 · ` : ''}
                            {formatDate(doc.created_at ?? undefined)}
                          </span>
                        </li>
                      ))}
                      {(versions?.documents ?? []).length === 0 && (
                        <li className="text-muted-foreground">暂无文稿版本</li>
                      )}
                    </ul>
                  </div>
                  <div>
                    <p className="mb-2 text-xs font-medium text-muted-foreground">大纲版本</p>
                    <ul className="space-y-1 text-xs">
                      {(versions?.outlines ?? []).map((outline) => (
                        <li key={outline.id} className="flex items-center gap-2">
                          <Badge variant="outline">v{outline.version}</Badge>
                          <span className="text-muted-foreground">
                            {outline.section_count} 节 · {outline.status} ·{' '}
                            {formatDate(outline.created_at ?? undefined)}
                          </span>
                        </li>
                      ))}
                      {(versions?.outlines ?? []).length === 0 && (
                        <li className="text-muted-foreground">暂无大纲版本</li>
                      )}
                    </ul>
                  </div>
                </CardContent>
              </Card>
            </>
          )}
        </div>
      )}
    </div>
  );
}
