'use client';

import * as React from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  FileSearch,
  FileText,
  Loader2,
  RotateCcw,
  Trash2,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import type {
  LibraryPdfUpload,
  LiteratureRole,
  PdfUploadStatus,
} from '@/lib/types';

const STATUS_LABEL: Record<PdfUploadStatus, string> = {
  matching: '待匹配',
  needs_confirmation: '待确认',
  parsing: '解析中',
  extracting: '提取卡片与证据',
  ready: '全文可用',
  match_failed: '匹配失败',
  parse_failed: '解析失败',
  rejected: '已拒绝',
};

function statusVariant(
  status: PdfUploadStatus,
): 'muted' | 'secondary' | 'warning' | 'success' | 'destructive' {
  if (status === 'ready') return 'success';
  if (status === 'needs_confirmation') return 'warning';
  if (status === 'match_failed' || status === 'parse_failed') return 'destructive';
  if (status === 'rejected') return 'muted';
  return 'secondary';
}

function StatusIcon({ status }: { status: PdfUploadStatus }) {
  if (status === 'ready') return <CheckCircle2 className="h-3.5 w-3.5" />;
  if (status === 'match_failed' || status === 'parse_failed') {
    return <AlertTriangle className="h-3.5 w-3.5" />;
  }
  if (status === 'matching' || status === 'parsing' || status === 'extracting') {
    return <Loader2 className="h-3.5 w-3.5 animate-spin" />;
  }
  return <FileSearch className="h-3.5 w-3.5" />;
}

export function PdfUploadQueue({
  uploads,
  busyId,
  onConfirm,
  onRetry,
  onReject,
}: {
  uploads: LibraryPdfUpload[];
  busyId?: string | null;
  onConfirm: (upload: LibraryPdfUpload) => void;
  onRetry: (upload: LibraryPdfUpload) => void;
  onReject: (upload: LibraryPdfUpload) => void;
}) {
  const visible = uploads.filter((upload) => upload.status !== 'ready' && upload.status !== 'rejected');
  if (visible.length === 0) return null;
  const awaiting = visible.filter((upload) => upload.status === 'needs_confirmation').length;

  return (
    <section className="rounded-lg border bg-card" aria-labelledby="pdf-queue-heading">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3">
        <div>
          <h3 id="pdf-queue-heading" className="text-body font-medium">
            PDF 处理队列
          </h3>
          <p className="text-meta text-muted-foreground">
            匹配确认前不会创建或覆盖文献条目。
          </p>
        </div>
        {awaiting > 0 && <Badge variant="warning">{awaiting} 份待确认</Badge>}
      </header>
      <ul className="divide-y">
        {visible.map((upload) => {
          const title = upload.extracted_metadata?.title;
          const error =
            typeof upload.error?.message === 'string'
              ? upload.error.message
              : upload.error
                ? JSON.stringify(upload.error)
                : null;
          const canReject =
            upload.status !== 'parsing' &&
            upload.status !== 'extracting' &&
            upload.status !== 'parse_failed' &&
            busyId !== upload.id;
          return (
            <li key={upload.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
              <FileText className="h-5 w-5 shrink-0 text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-body font-medium">
                  {title || upload.filename}
                </p>
                <p className="truncate text-meta text-muted-foreground">
                  {title ? upload.filename : '正在提取题名、作者、年份和 DOI'}
                  {error ? ` · ${error}` : ''}
                </p>
              </div>
              <Badge variant={statusVariant(upload.status)} className="gap-1">
                <StatusIcon status={upload.status} />
                {STATUS_LABEL[upload.status]}
              </Badge>
              {upload.status === 'needs_confirmation' && (
                <Button size="sm" onClick={() => onConfirm(upload)} disabled={busyId === upload.id}>
                  确认匹配
                </Button>
              )}
              {upload.status === 'match_failed' && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => onRetry(upload)}
                  disabled={busyId === upload.id}
                >
                  {busyId === upload.id ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <RotateCcw />
                  )}
                  重新匹配
                </Button>
              )}
              {upload.status === 'parse_failed' && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => onRetry(upload)}
                  disabled={busyId === upload.id}
                >
                  {busyId === upload.id ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <RotateCcw />
                  )}
                  重新解析
                </Button>
              )}
              {canReject && (
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label={`拒绝并移除 ${upload.filename}`}
                  onClick={() => onReject(upload)}
                >
                  <Trash2 />
                </Button>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function MatchedWork({ upload }: { upload: LibraryPdfUpload }) {
  const work = upload.matched_work;
  if (!work) return null;
  const meta = [
    work.authors.slice(0, 3).join(', '),
    work.publication_year,
    work.doi ? `DOI ${work.doi}` : undefined,
  ]
    .filter(Boolean)
    .join(' · ');
  return (
    <div className="w-full rounded-lg border border-primary bg-accent/50 px-3 py-3">
      <span className="block text-body font-medium">{work.canonical_title}</span>
      <span className="mt-1 block text-meta text-muted-foreground">{meta || '元数据不完整'}</span>
      <span className="mt-1 block text-meta text-muted-foreground">
        {upload.match_method || '元数据匹配'}
        {typeof upload.match_confidence === 'number'
          ? ` · 匹配度 ${Math.round(upload.match_confidence * 100)}%`
          : ''}
      </span>
    </div>
  );
}

export function PdfMatchDialog({
  upload,
  initialRole = 'general',
  busy,
  onClose,
  onConfirm,
  onReject,
}: {
  upload: LibraryPdfUpload | null;
  initialRole?: LiteratureRole;
  busy: boolean;
  onClose: () => void;
  onConfirm: (upload: LibraryPdfUpload, role: LiteratureRole) => Promise<void>;
  onReject: (upload: LibraryPdfUpload) => Promise<void>;
}) {
  const [role, setRole] = React.useState<LiteratureRole>('general');

  React.useEffect(() => {
    setRole(initialRole);
  }, [initialRole, upload]);

  if (!upload) return null;
  const metadata = upload.extracted_metadata;
  const extracted = [
    metadata?.authors?.join(', '),
    metadata?.publication_year,
    metadata?.doi ? `DOI ${metadata.doi}` : undefined,
  ]
    .filter(Boolean)
    .join(' · ');
  const canConfirm = upload.status === 'needs_confirmation' && Boolean(upload.matched_work);

  return (
    <Dialog
      open
      onClose={() => {
        if (!busy) onClose();
      }}
      title="确认 PDF 匹配"
      description="确认后会将私有原文绑定到该文献，并启动全文解析、卡片与证据处理。"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            暂不处理
          </Button>
          <Button
            variant="destructive"
            onClick={() => void onReject(upload)}
            disabled={busy}
          >
            拒绝此 PDF
          </Button>
          <Button
            onClick={() => void onConfirm(upload, role)}
            disabled={!canConfirm || busy}
          >
            {busy && <Loader2 className="h-4 w-4 animate-spin" />}
            确认并解析
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        <div className="rounded-lg bg-muted/40 px-3 py-3">
          <p className="text-meta text-muted-foreground">从 PDF 提取</p>
          <p className="mt-1 text-body font-medium">
            {metadata?.title || upload.filename}
          </p>
          <p className="mt-1 text-meta text-muted-foreground">
            {extracted || '未能提取到完整作者、年份或 DOI'}
          </p>
        </div>

        {upload.matched_work ? (
          <div className="space-y-3">
            <p className="text-body font-medium">核验后的唯一匹配结果</p>
            <MatchedWork upload={upload} />
            <label className="flex min-h-11 items-center gap-3 rounded-lg border px-3 py-2 text-body">
              <input
                type="checkbox"
                checked={role === 'core'}
                onChange={(event) => setRole(event.target.checked ? 'core' : 'general')}
                className="h-4 w-4 rounded border-input accent-primary"
              />
              标记为核心文献
            </label>
          </div>
        ) : (
          <div className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-3 text-body">
            没有可确认的核验结果。请拒绝本次上传，并通过 DOI 或更清晰的 PDF 重新添加。
          </div>
        )}
      </div>
    </Dialog>
  );
}
