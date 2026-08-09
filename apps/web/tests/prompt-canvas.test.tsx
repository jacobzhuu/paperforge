import * as React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const push = vi.fn();
const createProject = vi.fn();
const generateAll = vi.fn();
const listProjects = vi.fn();
const uploadAsset = vi.fn();
const getAssetCapabilities = vi.fn();
const getMaterialPreflight = vi.fn();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push }),
}));

vi.mock('@/lib/api', () => ({
  createProject: (...args: unknown[]) => createProject(...args),
  generateAll: (...args: unknown[]) => generateAll(...args),
  listProjects: (...args: unknown[]) => listProjects(...args),
  uploadAsset: (...args: unknown[]) => uploadAsset(...args),
  getAssetCapabilities: (...args: unknown[]) => getAssetCapabilities(...args),
  getMaterialPreflight: (...args: unknown[]) => getMaterialPreflight(...args),
}));

import { PromptCanvas } from '@/components/home/prompt-canvas';

const project = {
  id: 'project-auto',
  title: '自动论文',
  paper_type: 'review',
  writing_mode: 'auto',
  language: 'zh',
  status: 'draft',
  citation_style: 'gbt7714',
};

function chooseWritingMode(mode: 'auto' | 'assisted') {
  fireEvent.click(screen.getByRole('button', { name: /更多设置/ }));
  fireEvent.change(screen.getByLabelText('写作模式'), { target: { value: mode } });
}

function enterTopicAndSubmit() {
  fireEvent.change(screen.getByLabelText('描述你的研究主题、问题或论文目标'), {
    target: { value: '大语言模型事实一致性研究' },
  });
  fireEvent.click(screen.getByRole('button', { name: /^(创建|从材料)/ }));
}

describe('首页创建项目的写作模式', () => {
  beforeEach(() => {
    push.mockReset();
    createProject.mockReset();
    generateAll.mockReset();
    listProjects.mockReset();
    uploadAsset.mockReset();
    getAssetCapabilities.mockReset();
    getMaterialPreflight.mockReset();
    listProjects.mockResolvedValue({ data: [], source: 'live' });
    createProject.mockResolvedValue({ data: project, source: 'live' });
    getAssetCapabilities.mockResolvedValue({
      max_bytes: 32 * 1024 * 1024,
      max_mib: 32,
      preferred_extensions: ['.csv', '.txt'],
      accepts_unrecognized_as_method_note: true,
    });
    getMaterialPreflight.mockResolvedValue({ ready: true, issues: [] });
    generateAll.mockResolvedValue({
      data: { id: 'full-job', project_id: project.id, kind: 'full', status: 'queued' },
      source: 'live',
    });
  });

  it('全自动模式创建后立即启动全管线，再进入项目页', async () => {
    render(<PromptCanvas />);
    chooseWritingMode('auto');
    enterTopicAndSubmit();

    await waitFor(() => {
      // draft 而不是 scholarly：一键全流程承诺「一次跑到 PDF」，scholarly 会在
      // 写完后再跑收敛循环、且未过质量门就不导出——默认走它等于承诺可能落空。
      expect(generateAll).toHaveBeenCalledWith(project.id, {
        quality_profile: 'draft',
        review_style: 'narrative',
      });
    });
    expect(createProject.mock.invocationCallOrder[0]).toBeLessThan(
      generateAll.mock.invocationCallOrder[0],
    );
    expect(push).toHaveBeenCalledWith(`/projects/${project.id}`);
  });

  it('协作模式只创建项目，不擅自启动全管线', async () => {
    render(<PromptCanvas />);
    chooseWritingMode('assisted');
    enterTopicAndSubmit();

    await waitFor(() => expect(push).toHaveBeenCalledWith(`/projects/${project.id}`));
    expect(generateAll).not.toHaveBeenCalled();
  });

  it('原创论文先上传素材，再启动全管线', async () => {
    createProject.mockResolvedValue({
      data: { ...project, paper_type: 'original' },
      source: 'live',
    });
    uploadAsset.mockResolvedValue({ data: { id: 'asset-1' }, source: 'live' });
    render(<PromptCanvas />);
    chooseWritingMode('auto');

    const file = new File(['method,result\nA,0.95'], 'results.csv', { type: 'text/csv' });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });
    enterTopicAndSubmit();

    await waitFor(() => expect(generateAll).toHaveBeenCalledTimes(1));
    expect(uploadAsset).toHaveBeenCalledWith(project.id, file);
    expect(uploadAsset.mock.invocationCallOrder[0]).toBeLessThan(
      generateAll.mock.invocationCallOrder[0],
    );
  });

  it('研究型论文可以只凭文件创建，题目来自文件名且不伪造研究问题', async () => {
    createProject.mockResolvedValue({
      data: { ...project, title: 'experiment-results', paper_type: 'original' },
      source: 'live',
    });
    uploadAsset.mockResolvedValue({ data: { id: 'asset-file-only' }, source: 'live' });
    render(<PromptCanvas />);
    fireEvent.change(screen.getByLabelText('论文类型'), { target: { value: 'original' } });
    chooseWritingMode('assisted');
    const file = new File(['group,value\nA,0.95'], 'experiment-results.csv', {
      type: 'text/csv',
    });
    fireEvent.change(document.querySelector('input[type="file"]') as HTMLInputElement, {
      target: { files: [file] },
    });

    fireEvent.click(screen.getByRole('button', { name: /^(创建|从材料)/ }));
    await waitFor(() => expect(push).toHaveBeenCalledWith(`/projects/${project.id}/assets`));
    expect(createProject).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'experiment-results', paper_type: 'original' }),
    );
    expect(createProject.mock.calls[0][0].topic).toBeUndefined();
    expect(uploadAsset).toHaveBeenCalledWith(project.id, file);
  });
});
