'use client';

import * as React from 'react';
import { NodeViewWrapper, type NodeViewProps } from '@tiptap/react';
import { Columns2, Copy, RefreshCw, Square, Trash2 } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import type { FigureNumbering } from '@/lib/figure-numbering';

export interface FigureNodeViewOptions {
  /** asset_ref → 预览图 URL。视觉列表刷新后必须就地更新，不重挂载编辑器。 */
  previewUrls: Record<string, string>;
  aiAssetRefs: Set<string>;
  numbering: FigureNumbering;
  readOnly: boolean;
  /** 用新版本替换这张图（打开视觉工作台的编辑器）。 */
  onReplace?: (assetRef: string) => void;
  /** 在光标处插入指向本图的引用。 */
  onInsertXref?: (label: string) => void;
}

interface FigurePayload {
  asset_ref?: string;
  caption?: string;
  alt_text?: string;
  label?: string | null;
  width?: 'column' | 'full';
}

/**
 * 正文里的图。
 *
 * 从只读原子块升级成可操作的 NodeView：图注、宽度、替换、删除、插入图引用都在
 * 图片旁边完成。此前这些操作要么不存在，要么散在另一个页面里。
 *
 * 关键约束：`previewUrls` 与 `numbering` 走 extension options 并由外部 effect
 * 就地更新——依赖重新挂载整个编辑器来刷新图片会丢光标、丢滚动位置，
 * 每次视觉列表刷新都把正在写字的人打断一次。
 */
export function FigureNodeView({ node, editor, deleteNode, updateAttributes }: NodeViewProps) {
  const options = editor.extensionManager.extensions.find((item) => item.name === node.type.name)
    ?.options as FigureNodeViewOptions | undefined;

  const payload = (node.attrs.payload ?? {}) as FigurePayload;
  const assetRef = payload.asset_ref ?? '';
  const label = payload.label ?? '';
  const src = options?.previewUrls[assetRef];
  const isAI = options?.aiAssetRefs.has(assetRef) ?? false;
  const readOnly = options?.readOnly ?? false;
  const number = options?.numbering.byAssetRef[assetRef] ?? options?.numbering.byLabel[label];
  const width = payload.width === 'full' ? 'full' : 'column';

  const [editingCaption, setEditingCaption] = React.useState(false);
  const [caption, setCaption] = React.useState(payload.caption ?? '');
  React.useEffect(() => setCaption(payload.caption ?? ''), [payload.caption]);

  const patch = (next: Partial<FigurePayload>) =>
    updateAttributes({ payload: { ...payload, ...next } });

  return (
    <NodeViewWrapper
      className={cn(
        'pf-figure-block my-4 rounded-lg border bg-card p-3',
        width === 'full' ? 'w-full' : 'mx-auto max-w-[80%]',
      )}
      data-type="figure"
    >
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element -- authenticated API rendition URL
        <img
          src={src}
          alt={payload.alt_text || payload.caption || ''}
          className="max-h-80 w-full rounded-md bg-white object-contain"
        />
      ) : (
        <div className="flex min-h-24 items-center justify-center rounded-md bg-muted text-xs text-muted-foreground">
          图片预览暂不可用 · {assetRef || '未指定素材'}
        </div>
      )}

      <figcaption className="mt-2 space-y-1 text-center text-sm text-muted-foreground">
        {editingCaption && !readOnly ? (
          <input
            autoFocus
            value={caption}
            onChange={(event) => setCaption(event.target.value)}
            onBlur={() => {
              setEditingCaption(false);
              if (caption !== payload.caption) patch({ caption });
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter') event.currentTarget.blur();
              if (event.key === 'Escape') {
                setCaption(payload.caption ?? '');
                setEditingCaption(false);
              }
            }}
            className="w-full rounded border bg-background px-2 py-1 text-center text-sm text-foreground"
            aria-label="图注"
          />
        ) : (
          <button
            type="button"
            onClick={() => !readOnly && setEditingCaption(true)}
            className={cn('w-full rounded px-1', !readOnly && 'hover:bg-accent/50')}
            title={readOnly ? undefined : '点击修改图注'}
          >
            {number ? <span className="font-medium text-foreground">图 {number}　</span> : null}
            {payload.caption || '未填写图注'}
          </button>
        )}
        {payload.alt_text && (
          <p className="text-xs text-muted-foreground/80">替代文本：{payload.alt_text}</p>
        )}
      </figcaption>

      <div className="mt-2 flex flex-wrap items-center justify-center gap-1 text-xs">
        {isAI && <Badge variant="warning">AI 插图</Badge>}
        {!readOnly && (
          <>
            <IconAction
              label={width === 'full' ? '改为单栏宽' : '改为通栏宽'}
              onClick={() => patch({ width: width === 'full' ? 'column' : 'full' })}
            >
              {width === 'full' ? (
                <Square className="h-3.5 w-3.5" />
              ) : (
                <Columns2 className="h-3.5 w-3.5" />
              )}
              {width === 'full' ? '通栏' : '单栏'}
            </IconAction>
            {label && options?.onInsertXref && (
              <IconAction label="在光标处插入图引用" onClick={() => options.onInsertXref!(label)}>
                <Copy className="h-3.5 w-3.5" /> 插入引用
              </IconAction>
            )}
            {options?.onReplace && (
              <IconAction label="用新版本替换" onClick={() => options.onReplace!(assetRef)}>
                <RefreshCw className="h-3.5 w-3.5" /> 替换
              </IconAction>
            )}
            <IconAction label="从正文删除这张图" onClick={() => deleteNode()} destructive>
              <Trash2 className="h-3.5 w-3.5" /> 删除
            </IconAction>
          </>
        )}
      </div>
    </NodeViewWrapper>
  );
}

function IconAction({
  label,
  onClick,
  destructive,
  children,
}: {
  label: string;
  onClick: () => void;
  destructive?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      contentEditable={false}
      className={cn(
        'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground',
        destructive && 'hover:text-destructive-strong',
      )}
    >
      {children}
    </button>
  );
}
