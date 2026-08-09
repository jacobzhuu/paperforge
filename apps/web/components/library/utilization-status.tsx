'use client';

import * as React from 'react';
import { AlertTriangle, CheckCircle2, Circle, Loader2, XCircle } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import type {
  FulltextStatus,
  LibraryEntry,
  LiteratureUtilization,
} from '@/lib/types';
import { cn } from '@/lib/utils';

export function entryRole(entry: LibraryEntry): 'general' | 'core' {
  return entry.literature_role ?? (entry.user_pinned ? 'core' : 'general');
}

export function entryUtilization(
  entry: LibraryEntry,
  citedIn: string[] = [],
): LiteratureUtilization {
  const current = entry.utilization;
  const citationCount = current?.citation_count ?? citedIn.length;
  return {
    fulltext_status:
      current?.fulltext_status ??
      (entry.card?.fulltext_used ? 'available' : entry.card ? 'abstract_only' : 'unavailable'),
    fulltext_source: current?.fulltext_source ?? 'none',
    evidence_status: current?.evidence_status ?? 'none',
    assignment_status: current?.assignment_status ?? 'unassigned',
    citation_status: current?.citation_status ?? (citationCount > 0 ? 'cited' : 'not_cited'),
    evidence_count: current?.evidence_count ?? 0,
    assignment_count: current?.assignment_count ?? 0,
    citation_count: citationCount,
    usage_evaluated: current?.usage_evaluated ?? false,
    unused_reason: current?.unused_reason ?? null,
  };
}

export function isCoreUnused(entry: LibraryEntry, usage: LiteratureUtilization): boolean {
  return (
    entryRole(entry) === 'core' &&
    usage.usage_evaluated &&
    usage.citation_count === 0
  );
}

const FULLTEXT_LABEL: Record<FulltextStatus, string> = {
  parsing: '解析中',
  available: '全文可用',
  failed: '解析失败',
  abstract_only: '仅摘要',
  unavailable: '缺全文',
};

function FulltextIcon({ status }: { status: FulltextStatus }) {
  if (status === 'available') return <CheckCircle2 className="h-3 w-3" />;
  if (status === 'parsing') {
    return <Loader2 className="h-3 w-3 animate-spin" />;
  }
  if (status === 'failed') return <XCircle className="h-3 w-3" />;
  return <Circle className="h-3 w-3" />;
}

export function UtilizationBadges({
  entry,
  citedIn = [],
  className,
}: {
  entry: LibraryEntry;
  citedIn?: string[];
  className?: string;
}) {
  const usage = entryUtilization(entry, citedIn);
  const cited = usage.citation_count;
  const reason = usage.unused_reason ?? undefined;

  return (
    <div className={cn('flex flex-wrap items-center gap-1', className)}>
      <Badge
        variant={
          usage.fulltext_status === 'available'
            ? 'success'
            : usage.fulltext_status === 'failed'
              ? 'destructive'
              : usage.fulltext_status === 'unavailable' ||
                  usage.fulltext_status === 'abstract_only'
                ? 'warning'
                : 'secondary'
        }
        className="gap-1 px-2 py-0 text-micro"
      >
        <FulltextIcon status={usage.fulltext_status} />
        {FULLTEXT_LABEL[usage.fulltext_status]}
      </Badge>
      <Badge
        variant={
          usage.evidence_status === 'extracted'
            ? 'success'
            : usage.evidence_status === 'pending'
              ? 'secondary'
              : 'muted'
        }
        className="px-2 py-0 text-micro"
      >
        {usage.evidence_status === 'extracted'
          ? `证据 ${usage.evidence_count}`
          : usage.evidence_status === 'pending'
            ? '证据提取中'
            : '待提取证据'}
      </Badge>
      <Badge
        variant={
          usage.assignment_status === 'assigned'
            ? 'success'
            : usage.assignment_status === 'pending'
              ? 'secondary'
              : 'muted'
        }
        className="px-2 py-0 text-micro"
      >
        {usage.assignment_status === 'assigned'
          ? `已分配 ${usage.assignment_count}`
          : usage.assignment_status === 'pending'
            ? '分配中'
            : '待分配'}
      </Badge>
      <Badge
        variant={cited > 0 ? 'success' : usage.usage_evaluated ? 'warning' : 'muted'}
        className="px-2 py-0 text-micro"
        title={reason}
      >
        {cited > 0 ? `正文 ${cited}` : usage.usage_evaluated ? '未进入正文' : '待进入正文'}
      </Badge>
      {isCoreUnused(entry, usage) && (
        <Badge variant="warning" className="gap-1 px-2 py-0 text-micro" title={reason}>
          <AlertTriangle className="h-3 w-3" />
          核心未使用
        </Badge>
      )}
    </div>
  );
}

export function UtilizationSummary({
  entries,
  citedIn = () => [],
}: {
  entries: LibraryEntry[];
  citedIn?: (entry: LibraryEntry) => string[];
}) {
  const included = entries.filter((entry) => entry.status === 'selected');
  const usages = included.map((entry) => entryUtilization(entry, citedIn(entry)));
  const cells = [
    { label: '已纳入', value: included.length },
    {
      label: '全文可用',
      value: usages.filter((usage) => usage.fulltext_status === 'available').length,
    },
    {
      label: '已提取证据',
      value: usages.filter((usage) => usage.evidence_status === 'extracted').length,
    },
    {
      label: '已分配',
      value: usages.filter((usage) => usage.assignment_status === 'assigned').length,
    },
    {
      label: '进入正文',
      value: usages.filter((usage) => usage.citation_status === 'cited').length,
    },
    {
      label: '未使用',
      value: usages.filter((usage) => usage.usage_evaluated && usage.citation_count === 0).length,
    },
  ];
  const coreWarnings = included.filter(
    (entry) =>
      isCoreUnused(entry, entryUtilization(entry, citedIn(entry))),
  ).length;

  return (
    <section aria-labelledby="library-utilization-heading">
      <header className="flex items-center justify-between gap-2 pb-3">
        <h3 id="library-utilization-heading" className="text-body">
          文献利用
        </h3>
        {coreWarnings > 0 && (
          <Badge variant="warning" className="gap-1">
            <AlertTriangle className="h-3 w-3" />
            {coreWarnings} 篇核心未使用
          </Badge>
        )}
      </header>
      <div className="grid grid-cols-2 gap-x-3 gap-y-2">
        {cells.map((cell) => (
          <div key={cell.label} className="border-t pt-2">
            <p className="text-lg font-semibold tabular-nums">{cell.value}</p>
            <p className="text-meta text-muted-foreground">{cell.label}</p>
          </div>
        ))}
      </div>
    </section>
  );
}
