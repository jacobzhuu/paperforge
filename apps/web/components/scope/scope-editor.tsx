'use client';

import * as React from 'react';
import { useRouter } from 'next/navigation';
import { Info, Loader2, Plus, Save, Search, Sparkles, X } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Callout } from '@/components/ui/callout';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import { WorkbenchHeader } from '@/components/project/workbench-header';
import { WorkbenchFooterNav } from '@/components/project/workbench-footer-nav';
import { useJobFinished, useProject } from '@/components/project/project-context';
import { TaskBinding } from '@/components/scope/task-binding';
import { generateScope, getScope, startSearch, updateScope } from '@/lib/api';
import { describeError } from '@/lib/errors';
import { projectHref } from '@/lib/pipeline';
import type { ScopeKeywordGroup, ScopePayload } from '@/lib/types';

/**
 * 研究范围编辑器。
 *
 * 后端三个端点（GET/PUT /scope、POST /scope/generate）在 M1 就已实现，
 * `lib/api.ts` 里的客户端函数也一直在，但**没有任何界面调用它们**——
 * 检索质量的唯一控制点对用户完全不可见。结果是
 * `worker.py::scope_needs_regeneration` 注释描述的死循环：
 * 「关键词很差 → 检索跑题 → 再点一次还是同样的关键词」。
 *
 * 后端在 PUT 时会打上 `generator='user'`（routers/projects.py），使这份 scope
 * 豁免检索前的自动重生成。这个语义必须在界面上说清楚，否则用户不知道
 * 自己的修改会不会被下一次检索覆盖。
 */
export function ScopeEditor() {
  const { projectId, project, busy, startJob, reload } = useProject();
  const { toast } = useToast();
  const router = useRouter();

  const [scope, setScope] = React.useState<ScopePayload>({});
  const [loading, setLoading] = React.useState(true);
  const [saving, setSaving] = React.useState(false);
  const [generating, setGenerating] = React.useState(false);
  const [dirty, setDirty] = React.useState(false);

  const load = React.useCallback(() => {
    if (!projectId) return;
    getScope(projectId)
      .then((res) => {
        setScope(res.data ?? {});
        setDirty(false);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [projectId]);

  React.useEffect(load, [load]);
  useJobFinished(load);

  const patch = (p: Partial<ScopePayload>) => {
    setScope((s) => ({ ...s, ...p }));
    setDirty(true);
  };

  const save = async (): Promise<boolean> => {
    setSaving(true);
    try {
      const res = await updateScope(projectId, scope);
      if (res.data) {
        setScope(res.data);
        setDirty(false);
        toast({ title: '研究范围已保存', description: '后续检索将固定使用你的版本。', variant: 'success' });
        reload();
        return true;
      }
      toast({ title: '未保存', description: '后端不可用。', variant: 'error' });
      return false;
    } catch (err) {
      toast({ title: '研究范围未能保存', description: describeError(err), variant: 'error' });
      return false;
    } finally {
      setSaving(false);
    }
  };

  const regenerate = async () => {
    setGenerating(true);
    try {
      const res = await generateScope(projectId, scope.topic ?? project?.topic ?? undefined);
      if (res.data) {
        setScope(res.data);
        setDirty(false);
        toast({ title: '已重新生成研究范围', variant: 'success' });
      } else {
        toast({ title: '未能生成', description: '后端不可用。', variant: 'error' });
      }
    } catch (err) {
      toast({ title: '生成失败', description: describeError(err), variant: 'error' });
    } finally {
      setGenerating(false);
    }
  };

  const saveAndSearch = async () => {
    if (dirty && !(await save())) return;
    try {
      const started = await startSearch(projectId, {});
      startJob(started.data, '后端不可用：无法触发检索');
      router.push(projectHref(projectId, 'library'));
    } catch (err) {
      toast({ title: '检索未能启动', description: describeError(err), variant: 'error' });
    }
  };

  const generator = String(scope.generator ?? '');
  const isFallback = generator.startsWith('deterministic');
  const isUserOwned = generator === 'user';
  const empty = !loading && Object.keys(scope).length === 0;

  return (
    <div className="space-y-6">
      <WorkbenchHeader
        title="研究范围"
        description="关键词组与研究问题——检索矩阵的直接输入"
        actions={
          <>
            <Button variant="outline" onClick={regenerate} disabled={generating || busy}>
              {generating ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Sparkles className="h-4 w-4" />
              )}
              重新生成
            </Button>
            <Button onClick={save} disabled={saving || !dirty}>
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              保存{dirty ? ' *' : ''}
            </Button>
          </>
        }
      />

      {loading ? (
        <Skeleton className="h-64" />
      ) : (
        <div className="grid gap-6 lg:grid-cols-[1fr,18rem]">
          <div className="space-y-4">
            {empty && (
              <div className="rounded-lg border border-dashed py-12 text-center text-body text-muted-foreground">
                还没有研究范围。点「重新生成」由 LLM 起草，或直接在下面手工填写。
                <br />
                这一步可以跳过——直接去文献工作台检索也能跑通。
              </div>
            )}

            <TaskBinding projectId={projectId} />

            <section>
              <header className="pb-3">
                <h3 className="text-body">研究问题</h3>
              </header>
              <div className="space-y-3">
                <div className="space-y-1.5">
                  <Label htmlFor="topic">主题</Label>
                  <Input
                    id="topic"
                    value={scope.topic ?? ''}
                    onChange={(e) => patch({ topic: e.target.value })}
                    placeholder="例如：扩散模型 医学影像分割"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="rq">研究问题</Label>
                  <Textarea
                    id="rq"
                    value={scope.research_question ?? ''}
                    onChange={(e) => patch({ research_question: e.target.value })}
                    placeholder="这篇综述要回答什么"
                    className="min-h-[72px]"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="summary">范围说明</Label>
                  <Textarea
                    id="summary"
                    value={scope.scope_summary ?? ''}
                    onChange={(e) => patch({ scope_summary: e.target.value })}
                    placeholder="覆盖哪些子方向、不覆盖哪些"
                    className="min-h-[72px]"
                  />
                </div>
              </div>
            </section>

            <KeywordGroups
              groups={scope.keyword_groups ?? []}
              onChange={(keyword_groups) => patch({ keyword_groups })}
            />

            <section>
              <header className="pb-3">
                <h3 className="text-body">时间范围与纳入排除</h3>
              </header>
              <div className="space-y-3">
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label htmlFor="from">起始年份</Label>
                    <Input
                      id="from"
                      type="number"
                      value={scope.time_range?.start_year ?? ''}
                      onChange={(e) =>
                        patch({
                          time_range: {
                            ...scope.time_range,
                            start_year: e.target.value ? Number(e.target.value) : undefined,
                          },
                        })
                      }
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="to">截止年份</Label>
                    <Input
                      id="to"
                      type="number"
                      value={scope.time_range?.end_year ?? ''}
                      onChange={(e) =>
                        patch({
                          time_range: {
                            ...scope.time_range,
                            end_year: e.target.value ? Number(e.target.value) : undefined,
                          },
                        })
                      }
                    />
                  </div>
                </div>
                <LineList
                  label="纳入标准（每行一条）"
                  value={scope.inclusion_notes ?? []}
                  onChange={(inclusion_notes) => patch({ inclusion_notes })}
                />
                <LineList
                  label="排除标准（每行一条）"
                  value={scope.exclusion_notes ?? []}
                  onChange={(exclusion_notes) => patch({ exclusion_notes })}
                />
              </div>
            </section>
          </div>

          <aside className="space-y-4 lg:sticky lg:top-4 lg:self-start">
            {isFallback && (
              <Callout variant="warning" className="flex items-start gap-2">
                <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>
                  这份范围是 LLM 当时不可用留下的<strong className="font-semibold">确定性回退</strong>，
                  检索前会被自动重新生成。手工改一处并保存后，就会固定使用你的版本。
                </span>
              </Callout>
            )}
            {isUserOwned && (
              <div className="rounded-md border border-success/40 bg-success/10 px-3 py-2 text-meta">
                已固定为你的版本，后续检索不会覆盖它。
              </div>
            )}

            <section>
              <header className="pb-2">
                <h3 className="text-body">这一步的作用</h3>
              </header>
              <div className="space-y-1.5 text-meta text-muted-foreground">
                <p>关键词组会被展开成检索矩阵，投给五个学术源。</p>
                <p>只索引英文的检索源打不中中文关键词——中文主题建议补一组英文同义词。</p>
                <p className="border-t pt-1.5">
                  生成来源：<span className="font-mono">{generator || '未记录'}</span>
                </p>
              </div>
            </section>

            <Button className="w-full" onClick={saveAndSearch} disabled={busy || saving}>
              <Search className="h-4 w-4" /> 保存并触发检索
            </Button>
          </aside>
        </div>
      )}

      <WorkbenchFooterNav current="scope" />
    </div>
  );
}

function KeywordGroups({
  groups,
  onChange,
}: {
  groups: ScopeKeywordGroup[];
  onChange: (groups: ScopeKeywordGroup[]) => void;
}) {
  const update = (index: number, patch: Partial<ScopeKeywordGroup>) =>
    onChange(groups.map((g, i) => (i === index ? { ...g, ...patch } : g)));

  return (
    <section>
      <header className="pb-3">
        <h3 className="text-body">关键词组（{groups.length}）</h3>
      </header>
      <div className="space-y-3">
        {groups.length === 0 && (
          <p className="text-meta text-muted-foreground">
            还没有关键词组。检索会退回用主题原文查询，命中率通常明显更低。
          </p>
        )}
        {groups.map((group, index) => (
          <div key={index} className="space-y-2 rounded-lg border p-3">
            <div className="flex items-center gap-2">
              <Input
                value={group.name}
                onChange={(e) => update(index, { name: e.target.value })}
                aria-label="关键词组名称"
                className="font-medium md:h-9"
                placeholder="组名，如「攻击方法」"
              />
              <button
                type="button"
                aria-label="删除关键词组"
                onClick={() => onChange(groups.filter((_, i) => i !== index))}
                className="rounded p-1 text-muted-foreground transition-colors hover:text-destructive-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <TagInput
              values={group.keywords ?? []}
              onChange={(keywords) => update(index, { keywords })}
            />
          </div>
        ))}
        <Button
          variant="outline"
          size="sm"
          onClick={() => onChange([...groups, { name: '', keywords: [] }])}
        >
          <Plus className="h-4 w-4" /> 新增关键词组
        </Button>
      </div>
    </section>
  );
}

function TagInput({
  values,
  onChange,
}: {
  values: string[];
  onChange: (values: string[]) => void;
}) {
  const [draft, setDraft] = React.useState('');

  const commit = () => {
    const value = draft.trim();
    if (!value || values.includes(value)) {
      setDraft('');
      return;
    }
    onChange([...values, value]);
    setDraft('');
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1">
        {values.map((value) => (
          <Badge key={value} variant="secondary" className="gap-1">
            {value}
            <button
              type="button"
              aria-label={`移除关键词 ${value}`}
              onClick={() => onChange(values.filter((v) => v !== value))}
              className="rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="h-3 w-3" />
            </button>
          </Badge>
        ))}
        {values.length === 0 && <span className="text-meta text-muted-foreground">暂无关键词</span>}
      </div>
      <Input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault();
            commit();
          }
        }}
        placeholder="输入关键词后回车"
        aria-label="新增关键词"
        className="md:h-9"
      />
    </div>
  );
}

function LineList({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string[];
  onChange: (value: string[]) => void;
}) {
  const [text, setText] = React.useState(value.join('\n'));
  React.useEffect(() => setText(value.join('\n')), [value]);

  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      <Textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={() =>
          onChange(
            text
              .split('\n')
              .map((line) => line.trim())
              .filter(Boolean),
          )
        }
        className="min-h-[60px] text-meta"
      />
    </div>
  );
}
