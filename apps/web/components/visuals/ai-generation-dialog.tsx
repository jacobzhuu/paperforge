'use client';

import * as React from 'react';
import { AlertTriangle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import type { ImageProviderCapabilities, VisualAsset } from '@/lib/types';

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
  const negativePrompt =
    capabilities?.supports_negative_prompt &&
    typeof visual?.spec?.negative_prompt === 'string'
      ? visual.spec.negative_prompt
      : '';
  const seed =
    capabilities?.supports_seed && typeof visual?.spec?.seed === 'number'
      ? visual.spec.seed
      : null;

  return (
    <Dialog
      open={visual !== null}
      onClose={onCancel}
      title="确认使用 AI 生成插图？"
      description="请确认生成参数和提示词；生成图片可能消耗可用额度。"
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
          <dt className="text-muted-foreground">生成方式</dt>
          <dd>AI 生成</dd>
          <dt className="text-muted-foreground">质量</dt>
          <dd>
            {quality === 'low' ? '低' : quality === 'high' ? '高' : '中'}
          </dd>
          <dt className="text-muted-foreground">输出尺寸</dt>
          <dd>
            {capabilities && capabilities.supported_sizes.length > 0
              ? (typeof visual?.spec?.size === 'string' ? visual.spec.size : '默认')
              : '自动适配（生成后显示实际尺寸）'}
          </dd>
          {seed !== null ? (
            <>
              <dt className="text-muted-foreground">随机种子</dt>
              <dd className="font-mono">{seed}</dd>
            </>
          ) : null}
        </dl>

        <div className="space-y-1">
          <p className="text-xs text-muted-foreground">
            将发送的最终提示词
            {visual?.spec?.prompt_override
              ? '（用户手动覆盖）'
              : visual?.spec?.refined_prompt
                ? '（已自动优化）'
                : ''}：
          </p>
          <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap rounded border bg-muted/50 p-2 font-mono text-xs">
            {prompt || '（空）'}
          </pre>
        </div>
        {negativePrompt ? (
          <div className="space-y-1">
            <p className="text-xs text-muted-foreground">负面提示词：</p>
            <pre className="max-h-28 overflow-y-auto whitespace-pre-wrap rounded border bg-muted/50 p-2 font-mono text-xs">
              {negativePrompt}
            </pre>
          </div>
        ) : null}

        <ul className="space-y-1 rounded-md border border-warning/40 bg-warning/10 p-3 text-xs text-warning-foreground">
          <li className="flex items-start gap-1.5">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>AI 生成可能消耗可用额度。</span>
          </li>
          <li>· 上述提示词已由模型读取当前论文全文并结合本次意图生成。</li>
          <li>· Yunwu 只接收上面这段最终提示词，<b>不会</b>附带论文原文、数据表或上传文件。</li>
          <li>· 图中文字可能出错，生成后请核对；承载数据的图请改用图表或示意图。</li>
        </ul>
      </div>
    </Dialog>
  );
}
