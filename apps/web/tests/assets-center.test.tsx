import * as React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fail, makeToastSpy, mockProjectContext, ok } from './helpers';

const projectCtx = { current: mockProjectContext({ paperType: 'original' }) };
const replaceSpy = vi.fn();
const toastSpy = makeToastSpy();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: replaceSpy, push: vi.fn() }),
  usePathname: () => '/projects/p1/assets',
}));

vi.mock('@/components/project/project-context', () => ({
  useProject: () => projectCtx.current,
  useJobFinished: () => {},
  useJobEvent: () => {},
}));

vi.mock('@/components/ui/toast', () => ({
  useToast: () => ({ toast: toastSpy.toast }),
}));

const listAssets = vi.fn();
const getNumLint = vi.fn();
const getMaterialPreflight = vi.fn();

vi.mock('@/lib/api', () => ({
  listAssets: (...args: unknown[]) => listAssets(...args),
  getNumLint: (...args: unknown[]) => getNumLint(...args),
  getMaterialPreflight: (...args: unknown[]) => getMaterialPreflight(...args),
  deleteAsset: vi.fn(),
  uploadAsset: vi.fn(),
  assetDownloadUrl: () => '#',
}));

import { AssetsCenter } from '@/components/assets/assets-center';

describe('素材中心', () => {
  beforeEach(() => {
    projectCtx.current = mockProjectContext({ paperType: 'original' });
    replaceSpy.mockClear();
    listAssets.mockReturnValue(
      ok([
        {
          id: 'a1',
          kind: 'result_table',
          title: '结果表',
          headers: [],
          preview_rows: [],
          warnings: [],
          number_count: 3,
        },
      ]),
    );
    getNumLint.mockReturnValue(ok({ consistent: true, checked_count: 3, sourced_count: 3 }));
    getMaterialPreflight.mockResolvedValue({ ready: true, issues: [] });
  });

  it('NUMLINT 失败时素材列表仍然渲染——这是 P0 的核心验收', async () => {
    getNumLint.mockReturnValue(fail());
    render(<AssetsCenter />);

    // 局部告警出现……
    expect(await screen.findByText(/数字一致性报告未能加载/)).toBeInTheDocument();
    // ……而素材本身照常显示，没有整页「加载失败」。
    expect(await screen.findByText('结果表')).toBeInTheDocument();
    expect(screen.queryByText('加载失败')).not.toBeInTheDocument();
  });

  it('素材接口失败时只有素材区降级，NUMLINT 摘要仍在', async () => {
    listAssets.mockReturnValue(fail());
    render(<AssetsCenter />);

    expect(await screen.findByText(/正文数字与素材一致/)).toBeInTheDocument();
    expect(await screen.findByText('加载失败')).toBeInTheDocument();
  });

  it('给出视觉工作台入口——旧用户在这里找过「图表与插图」页签', async () => {
    render(<AssetsCenter />);
    const link = await screen.findByRole('link', { name: /打开视觉工作台/ });
    expect(link).toHaveAttribute('href', '/projects/p1/visuals');
  });

  it('综述项目访问 /assets 时跳到视觉工作台，而不是渲染一个没意义的上传页', async () => {
    projectCtx.current = mockProjectContext({ paperType: 'review' });
    render(<AssetsCenter />);
    await waitFor(() => expect(replaceSpy).toHaveBeenCalledWith('/projects/p1/visuals'));
    expect(screen.queryByText('上传素材')).not.toBeInTheDocument();
  });
});
