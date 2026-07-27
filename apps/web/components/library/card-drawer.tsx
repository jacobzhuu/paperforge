'use client';

import * as React from 'react';
import { ExternalLink, ShieldCheck, ShieldAlert, KeyRound, FileText, Quote } from 'lucide-react';
import { Drawer } from '@/components/ui/drawer';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import type { LibraryEntry } from '@/lib/types';
import { ADDED_VIA_LABEL, LIBRARY_ACTION } from '@/lib/labels';

function Section({ title, items }: { title: string; items?: string[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="space-y-1.5">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h4>
      <ul className="list-disc space-y-1 pl-4 text-sm">
        {items.map((it, i) => (
          <li key={i}>{it}</li>
        ))}
      </ul>
    </div>
  );
}

export function CardDrawer({
  entry,
  open,
  citedIn = [],
  onClose,
  onToggleSelect,
}: {
  entry: LibraryEntry | null;
  open: boolean;
  /** 引用这篇文献的章节标题，见 lib/citation-usage.ts。 */
  citedIn?: string[];
  onClose: () => void;
  onToggleSelect: (entry: LibraryEntry) => void | Promise<void>;
}) {
  if (!entry) return null;
  const w = entry.work;
  const verified = !!entry.verified_at && !!entry.bibtex_key;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={w.canonical_title}
      description={`${w.authors.join(', ')} · ${w.publication_year ?? '—'}${w.venue_name ? ` · ${w.venue_name}` : ''}`}
      footer={
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground">
            来源：{ADDED_VIA_LABEL[entry.added_via]}
          </span>
          <Button
            variant={entry.status === 'selected' ? 'secondary' : 'default'}
            onClick={() => void onToggleSelect(entry)}
          >
            {/* 「取消入库」只是把状态退回候选（可逆）；批量栏里的「移出文献库」
                是真删除。此前两处都叫「移出文献库」，同名不同义。 */}
            {entry.status === 'selected' ? LIBRARY_ACTION.deselect : LIBRARY_ACTION.select}
          </Button>
        </div>
      }
    >
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-2">
          {verified ? (
            <Badge variant="success">
              <ShieldCheck className="mr-1 h-3 w-3" /> 已核验入库
            </Badge>
          ) : (
            <Badge variant="warning">
              <ShieldAlert className="mr-1 h-3 w-3" /> 待核验（R1）
            </Badge>
          )}
          {w.is_retracted && <Badge variant="destructive">已撤稿</Badge>}
          {w.oa_status && w.oa_status !== 'closed' && (
            <Badge variant="secondary">OA · {w.oa_status}</Badge>
          )}
          <Badge variant="muted">相关性 {(entry.relevance_score * 100).toFixed(0)}%</Badge>
        </div>

        {entry.bibtex_key && (
          <div className="flex items-center gap-2 rounded-md border bg-muted/40 px-3 py-2 text-sm">
            <KeyRound className="h-4 w-4 text-muted-foreground" />
            <code className="font-mono text-xs">{entry.bibtex_key}</code>
            <span className="text-xs text-muted-foreground">· cite-key 持久化（R3）</span>
          </div>
        )}

        {/*
          「它已经在你的论文里的哪几章」排在排序理由之前：一旦文献真的被引用，
          这就是关于它最有价值的一句话，比检索器当初为什么排上它更重要。
        */}
        {citedIn.length > 0 && (
          <div className="flex items-start gap-2 rounded-md border-l-2 border-success/50 bg-success/10 px-3 py-2 text-sm">
            <Quote className="mt-0.5 h-3.5 w-3.5 shrink-0 text-success-strong" />
            <span>
              已被正文引用于 <span className="font-medium">{citedIn.join('、')}</span>
            </span>
          </div>
        )}

        {entry.rank_reason && (
          <div className="rounded-md border-l-2 border-primary/40 bg-accent/30 px-3 py-2 text-sm text-muted-foreground">
            排序理由：{entry.rank_reason}
          </div>
        )}

        <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
          {w.doi && (
            <a
              href={`https://doi.org/${w.doi}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 hover:text-foreground"
            >
              <ExternalLink className="h-3.5 w-3.5" /> DOI: {w.doi}
            </a>
          )}
          {w.arxiv_id && (
            <a
              href={`https://arxiv.org/abs/${w.arxiv_id}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 hover:text-foreground"
            >
              <ExternalLink className="h-3.5 w-3.5" /> arXiv: {w.arxiv_id}
            </a>
          )}
          {typeof w.citation_count === 'number' && <span>被引 {w.citation_count}</span>}
        </div>

        {w.abstract && (
          <div className="space-y-1.5">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">摘要</h4>
            <p className="text-sm leading-relaxed">{w.abstract}</p>
          </div>
        )}

        <div className="border-t pt-4">
          {entry.card ? (
            <div className="space-y-4">
              <div className="flex items-center gap-2">
                <FileText className="h-4 w-4 text-muted-foreground" />
                <h3 className="text-sm font-semibold">文献卡片</h3>
                {entry.card.fulltext_used ? (
                  <Badge variant="success">全文抽取</Badge>
                ) : (
                  <Badge variant="muted">摘要级</Badge>
                )}
              </div>
              <p className="text-sm leading-relaxed">{entry.card.summary}</p>
              <Section title="贡献" items={entry.card.contributions} />
              <Section title="方法" items={entry.card.methods} />
              <Section title="结果" items={entry.card.results} />
              <Section title="局限" items={entry.card.limitations} />
              <Section title="可引要点" items={entry.card.quotable_points} />
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              尚未抽取文献卡片。入库并运行 CARDS 阶段后，将展示贡献 / 方法 / 结果 / 局限 / 可引要点。
            </p>
          )}
        </div>
      </div>
    </Drawer>
  );
}
