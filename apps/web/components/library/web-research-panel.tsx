'use client';

import * as React from 'react';
import { Button } from '@/components/ui/button';
import { useJobFinished, useProjectActions, useProjectData } from '@/components/project/project-context';
import {
  getWebResearch, listWebResearch, refreshWebResearch, updateProject,
  type WebResearchRun,
} from '@/lib/api';
import { describeError } from '@/lib/errors';

const STATUS: Record<string, string> = {
  running: '正在检索', completed: '已完成', partial: '部分完成',
  failed: '获取失败', disabled: '已停止补充', budget_exhausted: '已达到调用额度',
  paused: '已暂停，可从任务继续', cancelled: '已取消',
};

export function WebResearchPanel() {
  const { projectId, project, reload } = useProjectData();
  const { busy, startJob } = useProjectActions();
  const [runs, setRuns] = React.useState<WebResearchRun[]>([]);
  const [selected, setSelected] = React.useState('');
  const [detail, setDetail] = React.useState<WebResearchRun | null>(null);
  const [available, setAvailable] = React.useState(true);
  const [pending, setPending] = React.useState(false);
  const [error, setError] = React.useState('');
  const [offset, setOffset] = React.useState(0);
  const [revision, refresh] = React.useReducer((n: number) => n + 1, 0);
  useJobFinished(() => refresh());

  React.useEffect(() => {
    if (!busy) return;
    const timer = setInterval(refresh, 4000);
    return () => clearInterval(timer);
  }, [busy]);

  React.useEffect(() => {
    let live = true;
    if (!projectId) return;
    listWebResearch(projectId, offset).then((result) => {
      if (!live) return;
      setAvailable(result.available);
      setRuns(result.runs);
      setSelected((id) => result.runs.some((run) => run.id === id) ? id : result.runs[0]?.id ?? '');
    }).catch((failure) => { if (live) setError(describeError(failure)); });
    return () => { live = false; };
  }, [projectId, offset, revision]);

  React.useEffect(() => {
    let live = true;
    setDetail(null);
    if (!projectId || !selected) return;
    getWebResearch(projectId, selected).then((run) => {
      if (live) setDetail(run);
    }).catch((failure) => { if (live) setError(describeError(failure)); });
    return () => { live = false; };
  }, [projectId, selected, revision]);

  async function toggle(enabled: boolean) {
    if (!projectId) return;
    setPending(true); setError('');
    try {
      await updateProject(projectId, { web_research_enabled: enabled });
      await reload();
    } catch (failure) { setError(describeError(failure)); }
    finally { setPending(false); }
  }

  async function start() {
    if (!projectId) return;
    setPending(true); setError('');
    try {
      const job = await refreshWebResearch(projectId);
      startJob(job, '网页检索已提交');
      setOffset(0); setSelected(''); refresh();
    } catch (failure) { setError(describeError(failure)); }
    finally { setPending(false); }
  }

  return (
    <section aria-label="网页资料" className="space-y-3 rounded-xl border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-medium">网页资料</h2>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={project?.web_research_enabled ?? false}
              disabled={pending || (!available && !project?.web_research_enabled)}
              onChange={(event) => void toggle(event.target.checked)} />
            补充网页检索
          </label>
          <Button size="sm" variant="outline"
            disabled={pending || busy || !available || !project?.web_research_enabled}
            onClick={() => void start()}>刷新网页资料</Button>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        启用后，文献准备时会向 Exa 发送项目主题和检索问题，查找官网、技术文档及数据集说明。
        网页仅作研究辅助；识别到的论文须通过来源核验才会加入候选文献。
      </p>
      {!available && <p className="text-sm">网页检索暂不可用，已有资料仍可查看。</p>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {runs.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-sm">检索记录{' '}
            <select aria-label="检索记录" className="rounded border bg-background p-1"
              value={selected} onChange={(event) => setSelected(event.target.value)}>
              {runs.map((run) => <option key={run.id} value={run.id}>
                {new Date(run.created_at).toLocaleString()} · {STATUS[run.status] ?? run.status}
              </option>)}
            </select>
          </label>
          <Button size="sm" variant="ghost" disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - 10))}>较新记录</Button>
          <Button size="sm" variant="ghost" disabled={runs.length < 10}
            onClick={() => setOffset(offset + 10)}>较早记录</Button>
        </div>
      ) : <div className="flex items-center gap-2">
        <p className="text-sm text-muted-foreground">暂无网页资料。</p>
        {offset > 0 && <Button size="sm" variant="ghost"
          onClick={() => setOffset(Math.max(0, offset - 10))}>较新记录</Button>}
      </div>}
      {detail && <div className="space-y-3">
        <p className="text-xs text-muted-foreground">
          {STATUS[detail.status] ?? detail.status} · {detail.calls} 次工具调用 · 费用未计价
          {detail.error && ' · 部分请求未完成，可稍后刷新。'}
        </p>
        {detail.sources?.map((source) => <article key={source.id} className="space-y-2 border-t pt-3">
          <a href={source.url} target="_blank" rel="noopener noreferrer"
            className="break-words text-sm font-medium underline">{source.title}</a>
          <p className="break-all text-xs text-muted-foreground">{source.url}</p>
          <p className="whitespace-pre-wrap break-words text-sm">{source.snippet}</p>
          {source.body && <details><summary className="cursor-pointer text-xs">查看页面片段</summary>
            <p className="mt-2 whitespace-pre-wrap break-words text-sm">{source.body}</p>
          </details>}
          <p className="text-xs text-muted-foreground">
            {source.fetched_at ? `读取于 ${new Date(source.fetched_at).toLocaleString()}`
              : source.status === 'read_failed' ? '页面读取未完成' : '检索结果，尚未读取页面'}
            {source.verification?.identifiers.map((item, index) => <span key={index}>
              {' · '}{item.doi ?? item.arxiv_id}：{item.verified ? '论文已核验' : '论文核验未通过'}
            </span>)}
          </p>
        </article>)}
      </div>}
    </section>
  );
}
