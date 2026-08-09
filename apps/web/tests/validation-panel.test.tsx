import * as React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ClaimEvidence, QualityReport } from '@/lib/types';
import { makeSection, ok } from './helpers';

const getClaimEvidence = vi.fn();
const reviewClaimEvidence = vi.fn();

vi.mock('@/components/project/project-context', () => ({
  useProjectData: () => ({ projectId: 'p1' }),
}));

vi.mock('@/lib/api', () => ({
  getClaimEvidence: (...args: unknown[]) => getClaimEvidence(...args),
  reviewClaimEvidence: (...args: unknown[]) => reviewClaimEvidence(...args),
}));

import { ValidationPanel } from '@/components/writing/validation-panel';

const report = {
  project_id: 'p1',
  report_id: 'report-new',
  section_count: 2,
  word_count: 1200,
  cite_count: 8,
  unique_cite_count: 4,
  whitelist_size: 5,
  citation_density: 6.7,
  library_coverage: 0.8,
  recent_ratio: 0.5,
  fulltext_coverage: 0.8,
  sections_without_citations: [],
  soft_check: [],
  hints: [],
  quality_profile: 'scholarly',
  review_style: 'narrative',
  readiness_status: 'needs_revision',
  stale: false,
  blockers: [{ code: 'claim_evidence_missing', message: '1 条核心论断缺少证据' }],
  warnings: [],
  scores: {},
  core_claim_count: 1,
  core_claim_fulltext_count: 0,
  core_claim_fulltext_coverage: 0,
  layout_checks: {},
  depth_metrics: {},
} satisfies QualityReport;

const anchor: ClaimEvidence = {
  id: 'anchor-1',
  report_id: 'report-new',
  section_key: 'introduction',
  claim_text: '核心论断',
  claim_kind: 'factual',
  is_core: true,
  cite_key: 'ref2025',
  source_key: 'cite:ref2025',
  source_kind: 'fulltext',
  source_section: 'Results',
  evidence_excerpt: '可定位的全文证据。',
  evidence_hash: 'hash-1',
  support_status: 'insufficient_support',
  manual_status: 'unreviewed',
};

describe('写作页质量证据判定', () => {
  beforeEach(() => {
    getClaimEvidence.mockReset();
    reviewClaimEvidence.mockReset();
    getClaimEvidence.mockReturnValue(ok([anchor]));
  });

  it('明确展示整篇范围，并在确认后立即显示保存结果', async () => {
    reviewClaimEvidence.mockResolvedValue({ ...anchor, manual_status: 'confirmed' });
    render(
      <ValidationPanel
        activeSection={makeSection()}
        activeDraft={null}
        audit={{ hallucinated_cite_keys: [] } as never}
        quality={report}
        numlint={undefined}
        showNumbers={false}
        onGenerateQuality={vi.fn()}
        onRepairQuality={vi.fn()}
        busy={false}
        qualityRunning={false}
        onJumpToSection={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole('tab', { name: '整篇论文' }));
    expect(screen.getByRole('heading', { name: '投稿质量门' })).toBeInTheDocument();
    await userEvent.click(await screen.findByRole('button', { name: '确认证据' }));

    await waitFor(() =>
      expect(reviewClaimEvidence).toHaveBeenCalledWith('p1', 'anchor-1', 'confirmed'),
    );
    expect(await screen.findByText('人工已确认证据')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '确认证据' })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '更改判定' }));
    expect(screen.getByRole('button', { name: '改为不支持' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '撤销判定' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '取消' })).toBeInTheDocument();
  });

  it('保存失败时给出错误反馈且不伪造成功状态', async () => {
    reviewClaimEvidence.mockRejectedValue(new Error('network down'));
    render(
      <ValidationPanel
        activeSection={makeSection()}
        activeDraft={null}
        audit={{ hallucinated_cite_keys: [] } as never}
        quality={report}
        numlint={undefined}
        showNumbers={false}
        onGenerateQuality={vi.fn()}
        onRepairQuality={vi.fn()}
        busy={false}
        qualityRunning={false}
        onJumpToSection={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole('tab', { name: '整篇论文' }));
    await userEvent.click(await screen.findByRole('button', { name: '标记不支持' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('保存失败：network down。请重试。');
    expect(screen.queryByText('人工已判定为不支持')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '标记不支持' })).toBeEnabled();
  });

  it('默认只展示当前章节，并把整篇检查分成独立页签', async () => {
    render(
      <ValidationPanel
        activeSection={makeSection({ cite_keys: ['ref2025'] })}
        activeDraft={null}
        audit={{
          hallucinated_cite_keys: [],
          used_cite_keys: ['ref2025'],
          whitelist_size: 1,
          removed_citation_warnings: [],
          unused_cite_keys: [],
        } as never}
        quality={report}
        numlint={{ consistent: true, checked_count: 1, sourced_count: 1 } as never}
        showNumbers
        onGenerateQuality={vi.fn()}
        onRepairQuality={vi.fn()}
        busy={false}
        qualityRunning={false}
        onJumpToSection={vi.fn()}
      />,
    );

    expect(screen.getByRole('tab', { name: '当前章节' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.getByText('本章引用（1）')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '投稿质量门' })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('tab', { name: '整篇论文' }));
    expect(screen.getByRole('tab', { name: '质量门' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '引用审计' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '数字检查' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '投稿质量门' })).toBeInTheDocument();
  });

  it('把证据阻断映射到具体论断，并可确认启动自动修复', async () => {
    const onRepairQuality = vi.fn();
    const onJumpToSection = vi.fn();
    render(
      <ValidationPanel
        activeSection={makeSection()}
        activeDraft={null}
        audit={{ hallucinated_cite_keys: [] } as never}
        quality={report}
        numlint={undefined}
        showNumbers={false}
        onGenerateQuality={vi.fn()}
        onRepairQuality={onRepairQuality}
        busy={false}
        qualityRunning={false}
        onJumpToSection={onJumpToSection}
      />,
    );

    await userEvent.click(screen.getByRole('tab', { name: '整篇论文' }));
    expect(await screen.findByText('待处理论断（1）')).toBeInTheDocument();
    expect(screen.getByText(/证据摘录只支持论断的一部分/)).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole('button', { name: '手动修复：定位到章节 introduction' }),
    );
    expect(onJumpToSection).toHaveBeenCalledWith('introduction');

    await userEvent.click(screen.getByRole('button', { name: '自动补证据并修订' }));
    expect(screen.getByRole('dialog', { name: '自动补证据并修订全文？' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '开始自动修复' }));
    expect(onRepairQuality).toHaveBeenCalledWith('scholarly', 'narrative');
  });

  it('人工判定已闭合时提示应用判定重查，而不是要求修改正文', async () => {
    const onGenerateQuality = vi.fn();
    getClaimEvidence.mockReturnValue(
      ok([{ ...anchor, manual_status: 'confirmed', source_section: 'Results' }]),
    );
    render(
      <ValidationPanel
        activeSection={makeSection()}
        activeDraft={null}
        audit={{ hallucinated_cite_keys: [] } as never}
        quality={report}
        numlint={undefined}
        showNumbers={false}
        onGenerateQuality={onGenerateQuality}
        onRepairQuality={vi.fn()}
        busy={false}
        qualityRunning={false}
        onJumpToSection={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole('tab', { name: '整篇论文' }));
    expect(await screen.findByText(/上方阻断来自判定前的旧报告/)).toBeInTheDocument();
    expect(screen.queryByText(/待处理论断（/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '应用判定并重新检查' }));
    expect(onGenerateQuality).toHaveBeenCalledWith('scholarly', 'narrative');
  });
});
