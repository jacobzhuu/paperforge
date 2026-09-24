import * as React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ createProject: vi.fn(), submitIntake: vi.fn(), listProjects: vi.fn(),
  uploadAsset: vi.fn(), getAssetCapabilities: vi.fn(), push: vi.fn() }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: api.push }) }));
vi.mock('@/lib/api', () => api);
import { PromptCanvas } from '@/components/home/prompt-canvas';

const project = { id: 'intent-project', title: '研究', paper_type: 'review', writing_mode: 'assisted',
  language: 'zh', status: 'draft', citation_style: 'gbt7714' };
function goal() {
  fireEvent.change(screen.getByLabelText('描述你的研究主题、问题或论文目标'), { target: { value: '根据实验数据写一篇英文研究型论文' } });
}
function upload(file = new File(['group,value\nA,0.95'], 'results.csv')) {
  fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [file] } });
}
function submit() { fireEvent.click(screen.getByRole('button', { name: /^(开始研究|继续开始研究)$/ })); }

describe('研究意图首页', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listProjects.mockResolvedValue({ data: [], source: 'live' });
    api.createProject.mockResolvedValue({ data: project, source: 'live' });
    api.submitIntake.mockResolvedValue({ id: 'intake-job', status: 'queued' });
    api.uploadAsset.mockResolvedValue({ data: { id: 'asset-1', warnings: [] }, source: 'live' });
    api.getAssetCapabilities.mockResolvedValue({ max_bytes: 33554432, max_mib: 32, preferred_extensions: ['.csv', '.txt'] });
  });
  it('选择示例保留已有研究目标并返回输入框', async () => {
    render(<PromptCanvas />);
    const example = await screen.findByRole('button', { name: '比较近五年大语言模型事实一致性评估方法' });
    goal();
    fireEvent.click(example);
    const input = screen.getByLabelText('描述你的研究主题、问题或论文目标');
    expect(input).toHaveValue('根据实验数据写一篇英文研究型论文\n比较近五年大语言模型事实一致性评估方法');
    expect(input).toHaveFocus();
  });
  it('默认无下拉框，协作与中文默认值可见，文字提交真实启动理解', async () => {
    render(<PromptCanvas />);
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.getByText('协作 · 中文（默认）')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '快速草稿' })).toHaveAttribute('aria-pressed', 'false');
    goal(); submit();
    await waitFor(() => expect(api.push).toHaveBeenCalledWith('/projects/intent-project'));
    expect(api.createProject).toHaveBeenCalledWith(expect.objectContaining({ intake: {}, writing_mode: 'assisted', language: 'zh', execution_profile: 'standard' }));
    expect(api.submitIntake).toHaveBeenCalledWith(project.id, { version: 0 });
  });
  it('调整面板保存手动英文与全自动，快速草稿独立', async () => {
    render(<PromptCanvas />);
    fireEvent.click(screen.getByRole('button', { name: '快速草稿' }));
    fireEvent.click(screen.getByRole('button', { name: '调整' }));
    fireEvent.click(screen.getByRole('radio', { name: /全自动/ }));
    fireEvent.change(screen.getByLabelText('语言'), { target: { value: 'en' } });
    fireEvent.change(screen.getByLabelText('论文类型'), { target: { value: 'original' } });
    fireEvent.click(screen.getByRole('button', { name: '完成' }));
    expect(screen.getByText('全自动 · English')).toBeInTheDocument();
    goal(); submit();
    await waitFor(() => expect(api.submitIntake).toHaveBeenCalled());
    expect(api.createProject).toHaveBeenCalledWith(expect.objectContaining({
      intake: { language: 'en', paper_type: 'original' }, writing_mode: 'auto', execution_profile: 'fast_draft', citation_style: 'gbt7714',
    }));
  });
  it('只上传材料也可启动，上传完成前不调用模型，不按文件类型决定管线', async () => {
    render(<PromptCanvas />); upload(); submit();
    await waitFor(() => expect(api.push).toHaveBeenCalled());
    expect(api.createProject).toHaveBeenCalledWith(expect.objectContaining({ title: 'results', intake: {}, topic: undefined }));
    expect(api.uploadAsset.mock.invocationCallOrder[0]).toBeLessThan(api.submitIntake.mock.invocationCallOrder[0]);
  });
  it('文件失败保留项目，重试不重复创建', async () => {
    api.uploadAsset.mockRejectedValueOnce(new Error('网络异常'));
    render(<PromptCanvas />); goal(); upload(); submit();
    await screen.findByRole('alert');
    expect(api.submitIntake).not.toHaveBeenCalled();
    submit();
    await waitFor(() => expect(api.push).toHaveBeenCalled());
    expect(api.createProject).toHaveBeenCalledTimes(1);
    expect(api.uploadAsset).toHaveBeenCalledTimes(2);
  });
  it('重复点击不会产生两个项目，创建失败不显示假工作区', async () => {
    api.createProject.mockResolvedValue({ data: project, source: 'mock', note: 'offline' });
    render(<PromptCanvas />); goal();
    const button = screen.getByRole('button', { name: '开始研究' });
    fireEvent.click(button); fireEvent.click(button);
    await screen.findByRole('alert');
    expect(api.createProject).toHaveBeenCalledTimes(1);
    expect(api.submitIntake).not.toHaveBeenCalled();
    expect(api.push).not.toHaveBeenCalled();
  });
  it('模型任务入队失败时保留已保存项目并可重试', async () => {
    api.submitIntake.mockRejectedValueOnce(new Error('queue unavailable'));
    render(<PromptCanvas />); goal(); submit();
    await screen.findByRole('alert');
    expect(screen.getByRole('link', { name: '进入已保存的项目' })).toHaveAttribute('href', '/projects/intent-project');
    submit();
    await waitFor(() => expect(api.push).toHaveBeenCalled());
    expect(api.createProject).toHaveBeenCalledTimes(1);
  });
  it('无效文件就地提示并阻止开始', async () => {
    render(<PromptCanvas />);
    await screen.findByText('支持文献、数据与代码，单文件最大 32 MiB。');
    upload(new File([], 'empty.csv'));
    expect(screen.getByText('空文件无法上传')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始研究' })).toBeDisabled();
  });
});
