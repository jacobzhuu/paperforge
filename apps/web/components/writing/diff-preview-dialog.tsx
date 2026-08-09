'use client';

import { Dialog } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Callout } from '@/components/ui/callout';

/**
 * AI 改写的接受 / 放弃确认。
 *
 * 此前 refine 的结果是**直接替换**用户的文字（`onTextChange(result.data.refined)`），
 * 没有预览也没有撤销。AI 动作绝不该静默改写作者已经写好的句子。
 */
export function DiffPreviewDialog({
  open,
  original,
  refined,
  note,
  onAccept,
  onCancel,
}: {
  open: boolean;
  original: string;
  refined: string;
  note?: string | null;
  onAccept: () => void;
  onCancel: () => void;
}) {
  const unchanged = original.trim() === refined.trim();

  return (
    <Dialog
      open={open}
      onClose={onCancel}
      className="max-w-3xl"
      title="确认改写"
      description={unchanged ? '模型返回的文字与原文一致。' : '接受后才会写回正文。'}
      footer={
        <>
          <Button variant="outline" onClick={onCancel}>
            放弃
          </Button>
          <Button onClick={onAccept} disabled={unchanged}>
            接受改写
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {note && (
          <Callout variant="warning">
            {note}
          </Callout>
        )}
        <div className="grid gap-3 md:grid-cols-2">
          <Panel label="原文" tone="muted" text={original} />
          <Panel label="改写后" tone="success" text={refined} />
        </div>
        <p className="text-xs text-muted-foreground">
          服务端保证改写不新增引用键、不改动任何数字；这里只替换选中的这段文字。
        </p>
      </div>
    </Dialog>
  );
}

function Panel({
  label,
  text,
  tone,
}: {
  label: string;
  text: string;
  tone: 'muted' | 'success';
}) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <div
        className={
          tone === 'success'
            ? 'max-h-64 overflow-y-auto rounded-md border border-success/40 bg-success/5 p-3 text-sm leading-relaxed scrollbar-thin'
            : 'max-h-64 overflow-y-auto rounded-md border bg-muted/30 p-3 text-sm leading-relaxed scrollbar-thin'
        }
      >
        {text || <span className="text-muted-foreground">（空）</span>}
      </div>
    </div>
  );
}
