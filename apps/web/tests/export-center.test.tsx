import * as React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EMPTY_PROGRESS } from '@/lib/useProjectProgress';
import { mockProjectContext, ok } from './helpers';

const mock = vi.hoisted(() => ({ startExport: vi.fn(), toast: vi.fn() }));
const context = { current: mockProjectContext() };
vi.mock('@/components/project/project-context', () => ({ useProject: () => context.current, useJobFinished: () => {}, useJobEvent: () => {} }));
vi.mock('@/components/ui/toast', () => ({ useToast: () => ({ toast: mock.toast }) }));
vi.mock('@/components/project/publication-metadata', () => ({ PublicationMetadata: () => null }));
vi.mock('@/components/project/workbench-footer-nav', () => ({ WorkbenchFooterNav: () => null }));
vi.mock('@/lib/api', () => ({
  ALL_EXPORT_FORMATS: ['pdf', 'markdown'],
  ApiError: class extends Error {},
  listExports: () => ok([]), listJobs: () => ok([]), getQuality: () => ok(undefined),
  startExport: (...args: unknown[]) => mock.startExport(...args),
}));
import { ExportCenter } from '@/components/export/export-center';

async function mount() {
  render(<ExportCenter />);
  await act(async () => { await Promise.resolve(); });
}
describe('导出流程', () => {
  beforeEach(() => {
    mock.startExport.mockReset();
    context.current = mockProjectContext({ progress: { ...EMPTY_PROGRESS, loading: false, sectionCount: 1 } });
  });
  it('启动中防止重复导出，失败后允许再次尝试', async () => {
    let reject: (error: Error) => void = () => {};
    mock.startExport.mockImplementation(() => new Promise((_, r) => { reject = r; }));
    await mount();
    fireEvent.click(screen.getByRole('button', { name: '生成投稿文件' }));
    expect(screen.getByRole('button', { name: '正在启动导出…' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '正在启动导出…' }));
    expect(mock.startExport).toHaveBeenCalledTimes(1);
    await act(async () => { reject(new Error('服务暂不可用')); });
    expect(screen.getByRole('button', { name: '生成投稿文件' })).toBeEnabled();
    expect(mock.toast).toHaveBeenCalledWith(expect.objectContaining({ title: '导出未能启动' }));
  });
  it('没有正文时引导写作，未知质量不显示为零覆盖率', async () => {
    context.current.progress = { ...EMPTY_PROGRESS, loading: false };
    await mount();
    expect(screen.getByRole('button', { name: '生成投稿文件' })).toBeDisabled();
    expect(screen.getByRole('link', { name: '写作工作台' })).toHaveAttribute('href', '/projects/p1/write');
    expect(screen.getByText('质量状态待确认')).toBeInTheDocument();
    expect(screen.queryByText(/覆盖.*0%/)).not.toBeInTheDocument();
  });
});
