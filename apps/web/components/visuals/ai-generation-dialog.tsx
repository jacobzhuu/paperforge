'use client';

import * as React from 'react';
import { AlertTriangle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import type { ImageProviderCapabilities, VisualAsset } from '@/lib/types';

const STEPS: Record<string, number> = { low: 4, medium: 6, high: 8 };

/**
 * AI 生图前的显式确认。
 *
 * 这是唯一会产生外部费用、且会把内容发到第三方的操作，因此必须由用户逐次确认：
 * 自动建议与「重新分析全文」都不得绕过这里（它们只产出 proposal，不生图）。
 *
 * 措辞注意：规划阶段**确实**会把章节摘要发给文本模型，所以这里只能承诺
 * 「不会发给**图像**服务商」，不能笼统写「不会发送论文原文」。
 */
export function AIGenerationDialog({
  visual,
  capabilities,
  onCancel,
  onConfirm,
}: {
  /** null 时对话框关闭。 */
  visual: VisualAsset | null;
  capabilities: ImageProviderCapabilities | null;
  onCancel: () => void;
  onConfirm: (visual: VisualAsset) => void;
}) {
  const quality = typeof visual?.spec?.quality === 'string' ? visual.spec.quality : 'medium';
  // resolved_prompt 由后端用**生成时同一个方法**渲染，界面不自己拼，
  // 否则用户确认的文本和真正发出去的会悄悄分叉。
  const prompt =
    visual?.resolved_prompt ??
    (typeof visual?.spec?.prompt === 'string' ? visual.spec.prompt : '');

  return (
    <Dialog
      open={visual !== null}
      onClose={onCancel}
      title="调用外部图像服务生成插图？"
      description="这一步会把下面的提示词发送到第三方图像服务，并可能产生费用。"
      footer={
        <>
          <Button variant="outline" onClick={onCancel}>
            取消
          </Button>
          <Button onClick={() => visual && onConfirm(visual)}>确认生成</Button>
        </>
      }
    >
      <div className="space-y-3 text-sm">
        <dl className="grid grid-cols-[6rem,minmax(0,1fr)] gap-x-3 gap-y-1.5">
          <dt className="text-muted-foreground">提供商</dt>
          <dd>{capabilities?.provider ?? '未配置'}</dd>
          <dt className="text-muted-foreground">模型</dt>
          <dd className="break-all font-mono text-xs">{capabilities?.model ?? '—'}</dd>
          <dt className="text-muted-foreground">质量</dt>
          <dd>
            {quality === 'low' ? '低' : quality === 'high' ? '高' : '中'}
            （约 {STEPS[quality] ?? 6} 步）
          </dd>
          <dt className="text-muted-foreground">输出尺寸</dt>
          <dd>
            {capabilities && capabilities.supported_sizes.length > 0
              ? (typeof visual?.spec?.size === 'string' ? visual.spec.size : '默认')
              : '由提供商决定（生成后显示实际尺寸）'}
          </dd>
        </dl>

        <div className="space-y-1">
          <p className="text-xs text-muted-foreground">将发送的最终提示词：</p>
          <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap rounded border bg-muted/50 p-2 font-mono text-xs">
            {prompt || '（空）'}
          </pre>
        </div>

        <ul className="space-y-1 rounded-md border border-warning/40 bg-warning/10 p-3 text-xs text-warning-foreground">
          <li className="flex items-start gap-1.5">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>会调用外部服务，可能产生费用。</span>
          </li>
          <li>· 只发送上面这段提示词，<b>不会</b>把论文原文、数据表或上传文件发给图像服务商。</li>
          <li>· 图中的文字大概率无法正确生成——需要文字的信息请改用示意图。</li>
        </ul>
      </div>
    </Dialog>
  );
}
