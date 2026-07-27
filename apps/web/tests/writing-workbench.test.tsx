import * as React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fail, makeSection, makeToastSpy, makeVisual, mockProjectContext, ok } from './helpers';

const projectCtx = { current: mockProjectContext() };
const toastSpy = makeToastSpy();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => '/projects/p1/write',
}));

vi.mock('@/components/project/project-context', () => ({
  useProject: () => projectCtx.current,
  useJobFinished: () => {},
  useJobEvent: () => {},
}));

vi.mock('@/components/ui/toast', () => ({
  useToast: () => ({ toast: toastSpy.toast }),
}));

// Tiptap 在 jsdom 里跑得起来但很慢，而这里要验的是**工作台的编排**，不是编辑器本身。
vi.mock('@/components/writing/section-editor', () => ({
  SectionEditor: ({ section }: { section: { title: string } }) => (
    <div data-testid="section-editor">{section.title}</div>
  ),
}));

const listSections = vi.fn();
const listVisuals = vi.fn();
const getCitationAudit = vi.fn();
const getMarkdownPreview = vi.fn();
const getQuality = vi.fn();
const getNumLint = vi.fn();
const getRuntimeSettings = vi.fn();
const approveVisual = vi.fn();
const updateSection = vi.fn();

vi.mock('@/lib/api', () => ({
  listSections: (...a: unknown[]) => listSections(...a),
  listVisuals: (...a: unknown[]) => listVisuals(...a),
  getCitationAudit: (...a: unknown[]) => getCitationAudit(...a),
  getMarkdownPreview: (...a: unknown[]) => getMarkdownPreview(...a),
  getQuality: (...a: unknown[]) => getQuality(...a),
  getNumLint: (...a: unknown[]) => getNumLint(...a),
  getRuntimeSettings: (...a: unknown[]) => getRuntimeSettings(...a),
  approveVisual: (...a: unknown[]) => approveVisual(...a),
  updateSection: (...a: unknown[]) => updateSection(...a),
  generateQuality: vi.fn(),
  generateSections: vi.fn(),
  refineText: vi.fn(),
  suggestVisuals: vi.fn(() => ok({ id: 'job-1' })),
  generateVisual: vi.fn(() => ok({ id: 'job-1', kind: 'visual' })),
  createVisual: vi.fn(),
  updateVisual: vi.fn(),
  regenerateVisual: vi.fn(),
  rejectVisual: vi.fn(() => ok(undefined)),
  visualRenditionUrl: () => '#',
}));

import { WritingWorkbench } from '@/components/writing/writing-workbench';

const DRAFT_KEY = 'paperforge:draft:p1:introduction';

/** 与服务端 IR 确有差异的一份草稿——「用户正在写字」的现场。 */
function dirtyDraft(key: string, title: string) {
  return {
    key,
    level: 1,
    title,
    blocks: [{ type: 'paragraph', runs: [{ t: 'text', v: '用户正在写的一句话' }] }],
    citation_warnings: [],
  };
}

describe('写作工作台', () => {
  beforeEach(() => {
    projectCtx.current = mockProjectContext();
    listSections.mockReturnValue(ok([makeSection()]));
    listVisuals.mockReturnValue(ok([makeVisual({ id: 'v1' })]));
    getCitationAudit.mockReturnValue(ok({ hallucinated_cite_keys: [] }));
    getMarkdownPreview.mockReturnValue(ok({ markdown: '' }));
    getQuality.mockReturnValue(ok({ soft_check: [] }));
    getNumLint.mockReturnValue(ok(undefined));
    getRuntimeSettings.mockReturnValue(
      ok({ ai_images_enabled: true, image_provider_configured: true, image_capabilities: null }),
    );
    approveVisual.mockReturnValue(ok(makeVisual({ id: 'v1', review_status: 'approved' })));
    updateSection.mockReturnValue(ok(makeSection()));
  });

  it('视觉接口失败不会让正文编辑器消失——P0 里最要命的一条', async () => {
    listVisuals.mockReturnValue(fail());
    render(<WritingWorkbench />);

    expect(await screen.findByTestId('section-editor')).toBeInTheDocument();
    expect(await screen.findByText(/视觉建议未能加载/)).toBeInTheDocument();
  });

  it('引用审计、预览、质量任一失败，正文照常打开', async () => {
    getCitationAudit.mockReturnValue(fail());
    getMarkdownPreview.mockReturnValue(fail());
    getQuality.mockReturnValue(fail());
    render(<WritingWorkbench />);

    expect(await screen.findByTestId('section-editor')).toBeInTheDocument();
  });

  it('章节接口失败才是硬失败：这时才显示整页加载失败', async () => {
    listSections.mockReturnValue(fail());
    render(<WritingWorkbench />);

    expect(await screen.findByText('加载失败')).toBeInTheDocument();
  });

  it('当前章节有未保存草稿时，先保存再批准——草稿不会被插图冲掉', async () => {
    /*
     * 差异是必须的：内容与服务端相同的草稿本来就会被持久化 effect 清掉，
     * 拿它来断言「已清除」测不出任何东西。
     */
    window.localStorage.setItem(DRAFT_KEY, JSON.stringify(dirtyDraft('introduction', '引言')));
    render(<WritingWorkbench />);

    // 草稿确实被恢复了（也就是说它真的与服务端不同）。
    await waitFor(() => expect(window.localStorage.getItem(DRAFT_KEY)).not.toBeNull());

    await userEvent.click(await screen.findByRole('button', { name: /批准并插入/ }));

    // 顺序是关键：先把用户的修改落盘，再让插图改写这一节。
    await waitFor(() => expect(updateSection).toHaveBeenCalled());
    expect(updateSection.mock.invocationCallOrder[0]).toBeLessThan(
      approveVisual.mock.invocationCallOrder[0],
    );
    await waitFor(() => expect(window.localStorage.getItem(DRAFT_KEY)).toBeNull());
  });

  it('草稿保存失败时不批准插图——否则用户的修改会被后续冲突弄丢', async () => {
    window.localStorage.setItem(DRAFT_KEY, JSON.stringify(dirtyDraft('introduction', '引言')));
    updateSection.mockRejectedValue(new Error('API 409: {"code":"section_changed"}'));
    render(<WritingWorkbench />);

    await waitFor(() => expect(window.localStorage.getItem(DRAFT_KEY)).not.toBeNull());
    await userEvent.click(await screen.findByRole('button', { name: /批准并插入/ }));

    await waitFor(() => expect(updateSection).toHaveBeenCalled());
    expect(approveVisual).not.toHaveBeenCalled();
    // 草稿必须原样留着——用户的修改是不能丢的那一份。
    expect(window.localStorage.getItem(DRAFT_KEY)).not.toBeNull();
  });

  it('批准到「非当前」章节时，也要清掉那一节的旧草稿', async () => {
    // 目标章节不是正在编辑的那一节：这条路径上不会触发保存，
    // 因此显式清除草稿是唯一的防线。留着它，下次切到该节再保存就会删掉刚插入的图。
    const other = makeSection({ section_key: 'results', title: '实验结果', order_no: 2 });
    listSections.mockReturnValue(ok([makeSection(), other]));
    listVisuals.mockReturnValue(
      ok([makeVisual({ id: 'v1', target_section_key: 'results' })]),
    );
    const otherKey = 'paperforge:draft:p1:results';
    window.localStorage.setItem(otherKey, JSON.stringify(dirtyDraft('results', '实验结果')));

    render(<WritingWorkbench />);
    await userEvent.click(await screen.findByRole('button', { name: /批准并插入/ }));

    await waitFor(() => expect(approveVisual).toHaveBeenCalled());
    // 当前节没有草稿，所以不该走保存路径。
    expect(updateSection).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(otherKey)).toBeNull();
  });

  it('批准时带上章节 updated_at 做乐观并发', async () => {
    render(<WritingWorkbench />);
    await userEvent.click(await screen.findByRole('button', { name: /批准并插入/ }));
    await waitFor(() =>
      expect(approveVisual).toHaveBeenCalledWith(
        'p1',
        'v1',
        'introduction',
        expect.any(Number),
        '2026-07-27T00:00:00Z',
      ),
    );
  });

  it('提供「打开视觉工作台」而不是把用户困在写作台里', async () => {
    render(<WritingWorkbench />);
    const link = await screen.findByRole('link', { name: /打开视觉工作台/ });
    expect(link).toHaveAttribute('href', '/projects/p1/visuals');
  });

  it('写作台里能直接「调整」——不再被迫跳去素材中心', async () => {
    render(<WritingWorkbench />);
    await userEvent.click(await screen.findByRole('button', { name: '调整' }));
    expect(await screen.findByText('调整视觉')).toBeInTheDocument();
  });
});
