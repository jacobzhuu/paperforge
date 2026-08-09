'use client';

import * as React from 'react';
import { Check, Info, Loader2, Tags } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Callout } from '@/components/ui/callout';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import { getProjectTasks, listTaskDefinitions, updateProjectTasks } from '@/lib/api';
import { describeError } from '@/lib/errors';
import type { ProjectTaskProfile, TaskDefinitionSummary } from '@/lib/types';

const GENERIC_SLUG = 'generic.scholarly';

/**
 * 研究领域绑定。
 *
 * 这个控件存在的理由：未绑定任务的项目会继承**全部**领域的指标与数据集白名单，
 * 于是一篇化学论文会被按推荐系统和生物合成基因簇的术语去抽取证据。此前界面上
 * 完全看不到这件事，也没有任何办法纠正它。
 */
export function TaskBinding({ projectId }: { projectId: string }) {
  const { toast } = useToast();
  const [profile, setProfile] = React.useState<ProjectTaskProfile | null>(null);
  const [catalogue, setCatalogue] = React.useState<TaskDefinitionSummary[]>([]);
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [loading, setLoading] = React.useState(true);
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => {
    const controller = new AbortController();
    let active = true;
    (async () => {
      const [current, all] = await Promise.all([
        getProjectTasks(projectId, controller.signal),
        listTaskDefinitions(controller.signal),
      ]);
      if (!active) return;
      setCatalogue(all.data ?? []);
      if (current.data) {
        setProfile(current.data);
        setSelected(new Set(current.data.task_ids));
      }
      setLoading(false);
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [projectId]);

  const byDomain = React.useMemo(() => {
    const groups = new Map<string, TaskDefinitionSummary[]>();
    for (const task of catalogue) {
      const bucket = groups.get(task.domain) ?? [];
      bucket.push(task);
      groups.set(task.domain, bucket);
    }
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [catalogue]);

  const dirty = React.useMemo(() => {
    const current = new Set(profile?.task_ids ?? []);
    if (current.size !== selected.size) return true;
    for (const slug of selected) if (!current.has(slug)) return true;
    return false;
  }, [profile, selected]);

  function toggle(slug: string) {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(slug)) {
        next.delete(slug);
      } else {
        // 通用任务与具体领域任务互斥：同时选中会让"通用"失去意义。
        if (slug === GENERIC_SLUG) next.clear();
        else next.delete(GENERIC_SLUG);
        next.add(slug);
      }
      return next;
    });
  }

  async function save() {
    setSaving(true);
    const result = await updateProjectTasks(projectId, [...selected]);
    setSaving(false);
    if (!result.data) {
      toast({ title: '保存失败', description: describeError(result), variant: 'error' });
      return;
    }
    setProfile(result.data);
    setSelected(new Set(result.data.task_ids));
    toast({
      title: result.data.bound ? '已绑定研究领域' : '已恢复为自动判断',
      description: result.data.bound
        ? '证据抽取只会使用所选领域的术语表。'
        : '未绑定时会按系统默认回退处理。',
    });
  }

  if (loading) return <Skeleton className="h-32 w-full" />;

  return (
    <section className="space-y-3" aria-labelledby="task-binding-heading">
      <div className="flex items-center gap-2">
        <Tags className="size-4 text-muted-foreground" aria-hidden />
        <h3 id="task-binding-heading" className="text-sm font-medium">
          研究领域
        </h3>
        {profile ? <SourceBadge source={profile.source} /> : null}
      </div>

      <p className="text-caption text-muted-foreground">
        决定证据抽取使用哪一套指标、数据集与方法术语。选错或不选都会让系统用不相关的
        领域词汇去读你的论文。
      </p>

      {profile?.fallback_note ? (
        <Callout variant="warning">
          <span className="flex items-start gap-2">
            <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
            <span>{profile.fallback_note}</span>
          </span>
        </Callout>
      ) : null}

      <div className="space-y-3">
        {byDomain.map(([domain, tasks]) => (
          <div key={domain}>
            <p className="mb-1 text-caption font-medium text-muted-foreground">{domain}</p>
            <div className="flex flex-wrap gap-1.5">
              {tasks.map((task) => {
                const active = selected.has(task.slug);
                return (
                  <button
                    key={task.slug}
                    type="button"
                    role="checkbox"
                    aria-checked={active}
                    onClick={() => toggle(task.slug)}
                    className={`inline-flex min-h-11 items-center gap-1.5 rounded-full border px-3 py-1 text-caption transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                      active
                        ? 'border-primary bg-primary/10 text-primary'
                        : 'border-border hover:bg-accent'
                    }`}
                  >
                    {active ? <Check className="size-3.5" aria-hidden /> : null}
                    {task.label}
                    {task.slug !== GENERIC_SLUG ? (
                      <span className="text-muted-foreground">
                        {task.metric_count}指标
                      </span>
                    ) : null}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-2">
        <Button onClick={save} disabled={!dirty || saving} size="sm">
          {saving ? <Loader2 className="mr-1.5 size-3.5 animate-spin" aria-hidden /> : null}
          保存绑定
        </Button>
        {selected.size > 0 ? (
          <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
            清空（恢复自动判断）
          </Button>
        ) : null}
      </div>
    </section>
  );
}

function SourceBadge({ source }: { source: ProjectTaskProfile['source'] }) {
  if (source === 'explicit') return <Badge variant="outline">已手动绑定</Badge>;
  if (source === 'inferred') return <Badge variant="outline">系统自动推断</Badge>;
  return <Badge variant="warning">未绑定</Badge>;
}
