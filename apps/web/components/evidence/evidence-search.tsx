'use client';

import * as React from 'react';
import { Button } from '@/components/ui/button';
import { indexEvidence, searchEvidence } from '@/lib/api';
import { describeError } from '@/lib/errors';
import type { EvidenceSearch } from '@/lib/types';
import { useProject } from '@/components/project/project-context';

export function EvidenceSearchPanel() {
  const { projectId, startJob, busy } = useProject();
  const [query, setQuery] = React.useState('');
  const [result, setResult] = React.useState<EvidenceSearch | null>(null);
  const [pending, setPending] = React.useState(false);
  const [error, setError] = React.useState('');
  const controller = React.useRef<AbortController | null>(null);
  React.useEffect(() => {
    setResult(null);
    setError('');
    return () => controller.current?.abort();
  }, [projectId]);

  async function search(event: React.FormEvent) {
    event.preventDefault();
    if (query.trim().length < 2) return;
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    setPending(true);
    setError('');
    try {
      const found = await searchEvidence(projectId, query.trim(), current.signal);
      if (!current.signal.aborted) setResult(found);
    } catch (cause) {
      if (!current.signal.aborted) setError(describeError(cause));
    } finally {
      if (!current.signal.aborted) setPending(false);
    }
  }

  return <section className="space-y-3 border-b pb-6" aria-label="查找支持证据">
    <h2 className="font-medium">查找支持证据</h2>
    <p className="text-sm text-muted-foreground">输入问题或论断，在当前项目入选文献中查找原文。检索相关性不代表论断已获支持。</p>
    <form onSubmit={search} className="flex flex-wrap gap-2">
      <input aria-label="研究问题或论断" value={query} maxLength={500} minLength={2}
        onChange={e => setQuery(e.target.value)} placeholder="例如：小样本学习的效果依赖哪些实验条件？"
        className="min-w-0 flex-1 rounded-md border bg-background px-3 py-2 text-sm" />
      <Button type="submit" disabled={pending || query.trim().length < 2}>{pending ? '查找中…' : '查找证据'}</Button>
      <Button type="button" variant="outline" disabled={busy || pending} onClick={async () => {
        setError(''); setPending(true);
        try { startJob(await indexEvidence(projectId), "暂时无法启动索引更新"); }
        catch (cause) { setError(describeError(cause)); }
        finally { setPending(false); }
      }}>更新语义索引</Button>
    </form>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {result && <>
      <p role="status" className="text-sm text-muted-foreground">
        找到 {result.results.length} 条相关证据 · 耗时 {result.elapsed_ms} ms
        {result.indexed_count < result.corpus_count && ` · ${result.corpus_count - result.indexed_count} 条尚未建立语义索引`}
        {result.warnings.length > 0 && ' · 部分检索能力不可用，已展示可用结果'}
        {result.corpus_truncated && ' · 当前结果仅覆盖部分证据'}
      </p>
      <ul className="space-y-3">{result.results.map(item => <li key={item.evidence_id} className="border-l-2 pl-3">
        <p className="text-xs text-muted-foreground">{item.page != null ? `第 ${item.page} 页` : '页码未定位'}
          {item.section_path && ` · ${item.section_path}`} · 尚未核验论断支持关系</p>
        <details><summary className="cursor-pointer text-sm leading-relaxed">{item.text.slice(0, 180)}{item.text.length > 180 && '…'}</summary>
          <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed">{item.text}</p>
        </details>
      </li>)}</ul>
    </>}
  </section>;
}
