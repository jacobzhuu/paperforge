'use client';

import * as React from 'react';
import { decideDraftRevision, exportDownloadUrl, exportPreviewUrl, listDraftRevisions, pauseJob, resumeJob, cancelJob } from '@/lib/api';
import { useAsyncModule } from '@/lib/useAsyncModule';
import { describeError } from '@/lib/errors';
import { Button } from '@/components/ui/button';
import { useToast } from '@/components/ui/toast';

const LABELS: Record<string, string> = {
  pending: '首稿可下载 · 等待后台核验', running: '首稿可下载 · 核验进行中',
  ready: '修订候选可用', needs_review: '修订候选可用 · 仍有待核验或修复的问题',
  accepted: '已采纳修订', discarded: '已丢弃修订候选',
};
export function RevisionPanel({ projectId, onAccepted }: { projectId: string; onAccepted: () => void }) {
  const module = useAsyncModule((signal) => listDraftRevisions(projectId, signal).then(r => r.data), [], [projectId]);
  const { toast } = useToast();
  const [action, setAction] = React.useState<string | null>(null);
  const pending = module.data.some(r => ['pending', 'running'].includes(r.status));
  React.useEffect(() => {
    const timer = window.setInterval(module.reload, pending ? 5000 : 30000);
    return () => window.clearInterval(timer);
  }, [pending, module.reload]);
  async function decide(id: string, choice: 'accept' | 'discard') {
    if (action) return;
    setAction(id);
    try {
      await decideDraftRevision(projectId, id, choice);
      module.reload();
      if (choice === 'accept') onAccepted();
    } catch (error) {
      toast({ title: '修订操作未完成', description: describeError(error), variant: 'error' });
    } finally { setAction(null); }
  }
  async function control(jobId: string, choice: 'pause' | 'resume' | 'cancel') {
    if (action) return;
    setAction(jobId);
    try {
      await ({ pause: pauseJob, resume: resumeJob, cancel: cancelJob }[choice])(projectId, jobId);
      module.reload();
    } catch (error) {
      toast({ title: '任务操作未完成', description: describeError(error), variant: 'error' });
    } finally { setAction(null); }
  }
  if (module.error) return <p role="alert" className="text-sm">修订状态暂不可用。<Button variant="ghost" onClick={module.reload}>重试</Button></p>;
  return <div className="space-y-3">{module.data.slice(0, 3).map(revision => {
    const interrupted = ['failed', 'paused', 'canceled', 'cancelled', 'needs_input'].includes(revision.job_status);
    const pdf = revision.artifacts.find(a => a.format === 'pdf');
    const complete = ['ready', 'needs_review'].includes(revision.status);
    return <div key={revision.id} className="rounded-lg border p-4 space-y-2">
      <p role="status" className="font-medium">{interrupted ? '后台核验未完成 · 首稿仍可下载' : LABELS[revision.status] ?? revision.status}</p>
      <p className="text-sm text-muted-foreground">候选独立保存。采纳前会检查正文是否已编辑；任务完成不代表质量通过。</p>
      {revision.review && <p className="text-sm">已评审 {revision.review.sections_reviewed ?? 0} 节；未完成 {revision.review.unassessed?.length ?? 0} 节；待修复 {revision.review.sections_failing?.length ?? 0} 节。</p>}
      {revision.review && <p className="text-sm text-muted-foreground">
        {revision.review.claim_count == null ? '逐条核验覆盖率：未记录' :
          `论断核验 ${revision.review.checked_count ?? 0}/${revision.review.claim_count}；不确定 ${revision.review.uncertain_count ?? 0}；未评估 ${revision.review.unassessed_count ?? 0}`}
      </p>}
      {(revision.recovery_message || revision.resume_block_reason) && <p role="alert" className="text-sm">{revision.recovery_message || revision.resume_block_reason}</p>}
      {revision.job_status === 'queued' && <p className="text-sm">后台任务排队中；前台优先，等待较久的核验会获得执行机会。</p>}
      {!!revision.review?.quality_warnings?.length && <details className="text-sm"><summary>查看 {revision.review.quality_warnings.length} 项质量提示</summary><ul className="list-disc pl-5 mt-2">{revision.review.quality_warnings.map((warning, i) => <li key={i}>{warning.message || warning.code}</li>)}</ul></details>}
      {revision.review?.layout_checks?.passed === false && <p className="text-sm">排版检查尚未通过，请预览 PDF 后处理。</p>}
      <div className="flex flex-wrap gap-3 items-center">
        {pdf && <><a className="text-sm underline" href={exportPreviewUrl(projectId, pdf.id)} target="_blank" rel="noreferrer">预览修订候选</a><a className="text-sm underline" href={exportDownloadUrl(projectId, pdf.id)}>下载修订 PDF</a></>}
        {['queued', 'running'].includes(revision.job_status) && <>{revision.job_status === 'running' && <Button variant="outline" disabled={!!action} onClick={() => control(revision.job_id, 'pause')}>暂停后台核验</Button>}<Button variant="ghost" disabled={!!action} onClick={() => control(revision.job_id, 'cancel')}>取消后台核验</Button></>}
        {['paused', 'failed'].includes(revision.job_status) && revision.can_resume !== false && !complete && <Button variant="outline" disabled={!!action} onClick={() => control(revision.job_id, 'resume')}>继续后台核验</Button>}
        {complete && <><Button disabled={!!action} onClick={() => decide(revision.id, 'accept')}>采纳为新版本</Button><Button variant="outline" disabled={!!action} onClick={() => decide(revision.id, 'discard')}>丢弃候选</Button></>}
      </div>
    </div>;
  })}</div>;
}
