'use client';

import * as React from 'react';
import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { getAgentTrace, respondToRepair } from '@/lib/api';
import { describeError } from '@/lib/errors';
import { stageLabel } from '@/lib/labels';
import type { AgentTrace } from '@/lib/types';

export default function TaskDetails() {
  const { id, jobId } = useParams<{ id: string; jobId: string }>();
  const router = useRouter();
  const [trace, setTrace] = React.useState<AgentTrace | null>(null);
  const [error, setError] = React.useState('');
  const [pending, setPending] = React.useState(false);
  const [revision, refresh] = React.useReducer(n => n+1, 0);
  React.useEffect(() => {
    const controller = new AbortController();
    setTrace(null); setError('');
    getAgentTrace(id, jobId, controller.signal).then(setTrace).catch(cause => {
      if (!controller.signal.aborted) setError(describeError(cause));
    });
    return () => controller.abort();
  }, [id, jobId, revision]);

  async function respond(choice: 'continue' | 'finish') {
    if (!trace?.interruption) return;
    setPending(true); setError('');
    try {
      const job = await respondToRepair(id, trace.jobs.at(-1)!.id, {
        interrupt_id: trace.interruption.id, choice,
      });
      router.push(`/projects/${id}/jobs/${job.id}`);
    } catch (cause) { setError(describeError(cause)); }
    finally { setPending(false); }
  }

  return <div className="space-y-6">
    <div className="flex items-center justify-between gap-3">
      <h1 className="text-2xl font-semibold">任务执行详情</h1>
      <Button variant="outline" onClick={refresh}>刷新</Button>
    </div>
    <Link href={`/projects/${id}`} className="text-sm underline">返回项目概览</Link>
    {error && <p role="alert" className="text-destructive">{error}</p>}
    {!trace && !error && <p role="status">正在读取执行记录…</p>}
    {trace && <>
      <dl className="grid grid-cols-2 gap-4 border-y py-4 sm:grid-cols-4">
        <div><dt className="text-sm text-muted-foreground">模型调用</dt><dd>{trace.summary.calls} 次</dd></div>
        <div><dt className="text-sm text-muted-foreground">已知费用估计</dt><dd>{trace.summary.known_cost.toFixed(4)} {trace.currency}</dd></div>
        <div><dt className="text-sm text-muted-foreground">Token</dt><dd>{(trace.summary.input_tokens+trace.summary.output_tokens).toLocaleString()}</dd></div>
        <div><dt className="text-sm text-muted-foreground">运行记录</dt><dd>{trace.jobs.length} 次（含续跑）</dd></div>
      </dl>
      {(trace.summary.unpriced_calls > 0 || trace.summary.unknown_usage_calls > 0) &&
        <p className="text-sm text-muted-foreground">费用和用量不完整：{trace.summary.unpriced_calls} 次未定价，{trace.summary.unknown_usage_calls} 次用量未知。</p>}
      {trace.interruption && <section className="space-y-3 rounded-md border p-4" aria-label="修复需要补充材料">
        <h2 className="font-medium">当前证据仍不足以完成修复</h2>
        <p className="text-sm">请在文献或证据页面补充材料后继续核查。结束修复会保留当前稿件，仍需通过原有质量检查才能导出。</p>
        <Link href={`/projects/${id}/evidence`} className="text-sm underline">查看与补充证据</Link>
        <div className="flex flex-wrap gap-2"><Button disabled={pending} onClick={() => respond('continue')}>已补充，继续核查</Button>
          <Button variant="outline" disabled={pending} onClick={() => respond('finish')}>结束本轮修复</Button></div>
      </section>}
      <section className="space-y-3"><h2 className="font-medium">执行过程</h2>
        {trace.events_truncated && <p>仅展示前 1000 条记录。</p>}
        <ol className="space-y-2">{trace.events.filter(e => e.type !== 'agent.call_reserved').map((event, i) =>
          <li key={`${event.at}-${i}`} className="flex flex-wrap gap-x-4 border-b py-2 text-sm">
            <time className="text-muted-foreground">{new Date(event.at).toLocaleTimeString()}</time>
            <span>{stageLabel(event.name || event.action || event.type)}</span>
            {event.type === 'polish.completed' && event.total != null && event.rewritten != null && <span>
              共 {event.total} 节 · 润色 {event.rewritten ?? 0} 节 · 跳过 {event.skipped ?? 0} 节 · 拒绝修改 {event.rejected ?? '未知'} 节
            </span>}
            {event.policy && <span>{{ legacy: '逐节全部润色', full_parallel: '并行全部润色', selective_parallel: '按需并行润色' }[event.policy] || event.policy}</span>}
            {event.concurrency != null && <span>并发上限 {event.concurrency}</span>}
            {event.elapsed_ms != null && <span>{(event.elapsed_ms/1000).toFixed(1)} 秒</span>}
            {event.status && <span>{{ completed: '完成', failed: '失败', interrupted: '中断' }[event.status] || event.status}</span>}
          </li>)}</ol>
      </section>
      <section className="space-y-3"><h2 className="font-medium">调用与等待</h2>
        {trace.calls_truncated && <p>仅展示前 1000 次调用，费用汇总涵盖全部记录。</p>}
        <div className="overflow-x-auto"><table className="w-full text-left text-sm">
          <thead><tr><th>用途</th><th>模型</th><th>调用耗时</th><th>本地排队</th><th>等待配额</th><th>费用估计</th><th>结果</th></tr></thead>
          <tbody>{trace.calls.map((call, i) => <tr key={i} className="border-t">
            <td className="py-2">{call.role}</td><td>{call.model}</td>
            <td>{call.latency_ms == null ? '未知' : `${(call.latency_ms/1000).toFixed(1)} 秒`}</td>
            <td>{call.local_queue_wait_ms == null ? '未记录' : `${(call.local_queue_wait_ms/1000).toFixed(1)} 秒`}</td>
            <td>{call.provider_slot_wait_ms == null && call.rate_limit_wait_ms == null ? '未记录' : `${((call.provider_slot_wait_ms || 0)+(call.rate_limit_wait_ms || 0))/1000} 秒`}</td>
            <td>{call.cost_estimate == null ? '未知' : call.cost_estimate.toFixed(4)}</td>
            <td>{call.error_code || '完成调用'}</td>
          </tr>)}</tbody>
        </table></div>
      </section>
    </>}
  </div>;
}
