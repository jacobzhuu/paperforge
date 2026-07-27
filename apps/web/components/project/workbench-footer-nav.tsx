'use client';

import Link from 'next/link';
import { ArrowLeft, ArrowRight } from 'lucide-react';
import { buttonVariants } from '@/components/ui/button';
import { useProject } from './project-context';
import { pipelineNeighbors, projectHref, type PipelineStepId } from '@/lib/pipeline';

/**
 * 工作台底部的上一步 / 下一步。
 *
 * 步骤序列按 paper_type 取——研究型论文的素材中心排在写作**之前**
 * （旧的 `PIPELINE_STEPS` 把它排在写作之后，等于让作者写完正文才被邀请
 * 上传那些本该给正文数字接地的数据）。
 *
 * gate-free：这里只是建议路径，不做任何前置校验，每一步都能直接跳。
 */
export function WorkbenchFooterNav({ current }: { current: PipelineStepId }) {
  const { projectId, paperType } = useProject();
  const { prev, next } = pipelineNeighbors(current, paperType);
  if (!projectId || (!prev && !next)) return null;

  return (
    <nav className="flex items-center justify-between gap-3 border-t pt-5" aria-label="管线上下步">
      {prev ? (
        <Link
          href={projectHref(projectId, prev.segment)}
          className={buttonVariants({ variant: 'ghost', size: 'sm' })}
        >
          <ArrowLeft className="h-4 w-4" /> {prev.label}
        </Link>
      ) : (
        <span />
      )}
      {next && (
        <Link
          href={projectHref(projectId, next.segment)}
          className={buttonVariants({ variant: 'outline', size: 'sm' })}
        >
          下一步：{next.label} <ArrowRight className="h-4 w-4" />
        </Link>
      )}
    </nav>
  );
}
