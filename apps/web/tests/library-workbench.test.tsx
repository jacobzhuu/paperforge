import * as React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { LibraryEntry, LibraryPdfUpload } from '@/lib/types';
import { makeSection, makeToastSpy, mockProjectContext, ok } from './helpers';

const projectCtx = { current: mockProjectContext() };
const toastSpy = makeToastSpy();

vi.mock('@/components/project/project-context', () => ({
  useProject: () => projectCtx.current,
  useProjectData: () => projectCtx.current,
  useProjectActions: () => projectCtx.current,
  useJobFinished: () => {},
  useJobEvent: () => {},
}));

vi.mock('@/components/ui/toast', () => ({
  useToast: () => ({ toast: toastSpy.toast }),
}));

const api = {
  confirmPdfUpload: vi.fn(),
  deleteLibraryEntry: vi.fn(),
  generateAll: vi.fn(),
  generateCards: vi.fn(),
  importReferences: vi.fn(),
  listLibrary: vi.fn(),
  listPdfUploads: vi.fn(),
  listSearchRuns: vi.fn(),
  listSections: vi.fn(),
  rejectPdfUpload: vi.fn(),
  retryPdfUpload: vi.fn(),
  selectEntries: vi.fn(),
  startIngest: vi.fn(),
  startSearch: vi.fn(),
  startSnowball: vi.fn(),
  updateLibraryEntry: vi.fn(),
  uploadLibraryPdf: vi.fn(),
};

vi.mock('@/lib/api', () => ({
  confirmPdfUpload: (...args: unknown[]) => api.confirmPdfUpload(...args),
  deleteLibraryEntry: (...args: unknown[]) => api.deleteLibraryEntry(...args),
  generateAll: (...args: unknown[]) => api.generateAll(...args),
  generateCards: (...args: unknown[]) => api.generateCards(...args),
  importReferences: (...args: unknown[]) => api.importReferences(...args),
  listLibrary: (...args: unknown[]) => api.listLibrary(...args),
  listPdfUploads: (...args: unknown[]) => api.listPdfUploads(...args),
  listSearchRuns: (...args: unknown[]) => api.listSearchRuns(...args),
  listSections: (...args: unknown[]) => api.listSections(...args),
  rejectPdfUpload: (...args: unknown[]) => api.rejectPdfUpload(...args),
  retryPdfUpload: (...args: unknown[]) => api.retryPdfUpload(...args),
  selectEntries: (...args: unknown[]) => api.selectEntries(...args),
  startIngest: (...args: unknown[]) => api.startIngest(...args),
  startSearch: (...args: unknown[]) => api.startSearch(...args),
  startSnowball: (...args: unknown[]) => api.startSnowball(...args),
  updateLibraryEntry: (...args: unknown[]) => api.updateLibraryEntry(...args),
  uploadLibraryPdf: (...args: unknown[]) => api.uploadLibraryPdf(...args),
}));

import { LibraryWorkbench } from '@/components/library/library-workbench';

function makeEntry(overrides: Partial<LibraryEntry> = {}): LibraryEntry {
  return {
    id: 'entry-1',
    status: 'candidate',
    literature_role: 'general',
    relevance_score: 0.9,
    added_via: 'search',
    verified_at: '2026-07-31T00:00:00Z',
    work: {
      id: 'work-1',
      canonical_title: 'Reliable Evidence Synthesis',
      authors: ['Ada Researcher'],
      publication_year: 2026,
      venue_name: 'Journal of Tests',
      doi: '10.1000/test',
    },
    ...overrides,
  };
}

function makeUpload(overrides: Partial<LibraryPdfUpload> = {}): LibraryPdfUpload {
  return {
    id: 'upload-1',
    filename: 'paper.pdf',
    status: 'needs_confirmation',
    extracted_metadata: {
      title: 'Reliable Evidence Synthesis',
      authors: ['Ada Researcher'],
      publication_year: 2026,
      doi: '10.1000/test',
    },
    matched_work: {
        id: 'work-1',
        canonical_title: 'Reliable Evidence Synthesis',
        authors: ['Ada Researcher'],
        publication_year: 2026,
        doi: '10.1000/test',
    },
    match_method: 'doi',
    match_confidence: 0.99,
    ...overrides,
  };
}

function arrange(entries: LibraryEntry[] = [makeEntry()], uploads: LibraryPdfUpload[] = []) {
  api.listLibrary.mockReturnValue(ok(entries));
  api.listPdfUploads.mockReturnValue(ok(uploads));
  api.listSearchRuns.mockReturnValue(ok([]));
  api.listSections.mockReturnValue(ok([]));
  api.selectEntries.mockReturnValue(ok([]));
  api.rejectPdfUpload.mockResolvedValue(undefined);
  api.updateLibraryEntry.mockImplementation(
    (_projectId: string, entryId: string, changes: Partial<LibraryEntry>) =>
      Promise.resolve({
        ...entries.find((entry) => entry.id === entryId)!,
        ...changes,
      }),
  );
}

describe('文献工作台 7.31 第一阶段', () => {
  beforeEach(() => {
    projectCtx.current = mockProjectContext();
    arrange();
  });

  it('统一“添加文献”菜单包含 PDF、DOI、BibTeX 和搜索', async () => {
    const user = userEvent.setup();
    render(<LibraryWorkbench />);
    await screen.findByText('Reliable Evidence Synthesis');

    await user.click(screen.getByRole('button', { name: /添加文献/ }));
    const menu = screen.getByRole('menu', { name: '添加文献' });
    expect(within(menu).getByRole('menuitem', { name: /上传 PDF/ })).toBeInTheDocument();
    expect(within(menu).getByRole('menuitem', { name: /输入 DOI/ })).toBeInTheDocument();
    expect(within(menu).getByRole('menuitem', { name: /粘贴 BibTeX/ })).toBeInTheDocument();
    expect(within(menu).getByRole('menuitem', { name: /搜索添加/ })).toBeInTheDocument();
  });

  it('批量复选与“纳入写作”是两套互不污染的状态', async () => {
    const user = userEvent.setup();
    render(<LibraryWorkbench />);
    await screen.findByText('Reliable Evidence Synthesis');

    await user.click(
      screen.getByRole('checkbox', { name: /选择用于批量操作：Reliable Evidence/ }),
    );
    expect(screen.getAllByText('已选 1').length).toBeGreaterThan(0);
    expect(api.updateLibraryEntry).not.toHaveBeenCalled();

    await user.click(screen.getByRole('switch', { name: /纳入写作：Reliable Evidence/ }));
    await waitFor(() =>
      expect(api.selectEntries).toHaveBeenCalledWith('p1', ['work-1'], 'selected'),
    );
    expect(screen.getAllByText('已选 1').length).toBeGreaterThan(0);
  });

  it('展示待确认 PDF，并把用户选择的文献角色交给确认端点', async () => {
    const upload = makeUpload();
    const parsingUpload = { ...upload, status: 'parsing' as const };
    arrange([makeEntry({ literature_role: 'core' })], [upload]);
    api.listPdfUploads
      .mockReturnValueOnce(ok([upload]))
      .mockReturnValue(ok([parsingUpload]));
    api.confirmPdfUpload.mockResolvedValue({
      upload: parsingUpload,
      job: null,
    });
    const user = userEvent.setup();
    render(<LibraryWorkbench />);

    expect(await screen.findByText('1 份待确认')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '确认匹配' }));
    const dialog = screen.getByRole('dialog', { name: '确认 PDF 匹配' });
    expect(within(dialog).getByText('核验后的唯一匹配结果')).toBeInTheDocument();
    expect(within(dialog).getAllByText(
      'Reliable Evidence Synthesis',
    ).length).toBeGreaterThan(0);
    expect(within(dialog).getByRole('checkbox', { name: '标记为核心文献' }))
      .toBeChecked();
    await user.click(within(dialog).getByRole('button', { name: '确认并解析' }));

    await waitFor(() =>
      expect(api.confirmPdfUpload).toHaveBeenCalledWith('p1', 'upload-1', 'core'),
    );
    expect(await screen.findByText('解析中')).toBeInTheDocument();
  });

  it.each([
    {
      failedStatus: 'match_failed' as const,
      nextStatus: 'matching' as const,
      actionName: '重新匹配',
      successStatus: '待匹配',
      matchedWork: null,
      canReject: true,
    },
    {
      failedStatus: 'parse_failed' as const,
      nextStatus: 'parsing' as const,
      actionName: '重新解析',
      successStatus: '解析中',
      matchedWork: makeUpload().matched_work,
      canReject: false,
    },
  ])('$failedStatus 可在队列内直接执行 $actionName', async ({
    failedStatus,
    nextStatus,
    actionName,
    successStatus,
    matchedWork,
    canReject,
  }) => {
    const upload = makeUpload({
      status: failedStatus,
      matched_work: matchedWork,
      error: { message: '处理失败' },
    });
    const job = {
      id: `job-${failedStatus}`,
      project_id: 'p1',
      kind: 'ingest' as const,
      status: 'queued' as const,
      progress: 0,
    };
    arrange([makeEntry()], [upload]);
    api.retryPdfUpload.mockResolvedValue({
      upload: { ...upload, status: nextStatus, error: null },
      job,
    });
    const user = userEvent.setup();
    render(<LibraryWorkbench />);

    expect(await screen.findByText(failedStatus === 'match_failed' ? '匹配失败' : '解析失败'))
      .toBeInTheDocument();
    const rejectButton = screen.queryByRole('button', {
      name: /拒绝并移除 paper\.pdf/,
    });
    expect(Boolean(rejectButton)).toBe(canReject);

    await user.click(screen.getByRole('button', { name: actionName }));

    await waitFor(() =>
      expect(api.retryPdfUpload).toHaveBeenCalledWith('p1', 'upload-1'),
    );
    expect(projectCtx.current.startJob).toHaveBeenCalledWith(
      job,
      expect.stringContaining(actionName),
    );
    expect(await screen.findByText(successStatus)).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: '确认 PDF 匹配' })).not.toBeInTheDocument();
  });

  it('每篇展示全文/证据/分配/正文四级状态，并汇总核心未使用告警', async () => {
    arrange([
      makeEntry({
        status: 'selected',
        literature_role: 'core',
        utilization: {
          fulltext_status: 'available',
          fulltext_source: 'user_pdf',
          evidence_status: 'extracted',
          assignment_status: 'assigned',
          citation_status: 'not_cited',
          evidence_count: 3,
          assignment_count: 2,
          citation_count: 0,
          usage_evaluated: true,
          unused_reason: '已有相关材料，但正文尚未采用',
        },
      }),
    ]);
    render(<LibraryWorkbench />);

    expect((await screen.findAllByText('全文可用')).length).toBeGreaterThan(0);
    expect(screen.getByText('证据 3')).toBeInTheDocument();
    expect(screen.getByText('已分配 2')).toBeInTheDocument();
    expect(screen.getByText('未进入正文')).toBeInTheDocument();
    expect(screen.getAllByText('核心未使用').length).toBeGreaterThan(0);
    expect(screen.getByText('1 篇核心未使用')).toBeInTheDocument();
  });

  it('正文引用仍可由 section cite_keys 回填到第四级状态', async () => {
    arrange([
      makeEntry({
        status: 'selected',
        bibtex_key: 'ada2026',
      }),
    ]);
    api.listSections.mockReturnValue(
      ok([makeSection({ title: '相关工作', cite_keys: ['ada2026'] })]),
    );
    render(<LibraryWorkbench />);

    expect(await screen.findByText('被引用于 相关工作')).toBeInTheDocument();
    expect(screen.getByText('正文 1')).toBeInTheDocument();
  });
});
