import * as React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
const mock = vi.hoisted(() => ({ getIntake: vi.fn(), submitIntake: vi.fn(), updateProject: vi.fn(), reload: vi.fn(), startJob: vi.fn(), project: {} as any }));
vi.mock('@/lib/api', () => mock);
vi.mock('@/components/project/project-context', () => ({ useProject: () => ({ projectId: 'p1', project: mock.project, reload: mock.reload, startJob: mock.startJob, busy: false }) }));
import { IntakePanel } from '@/components/project/intake-panel';

describe('工作区需求理解', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mock.project = { id: 'p1', topic: '先整理研究问题', paper_type: 'review', language: 'zh', writing_mode: 'assisted', citation_style: 'gbt7714', intake: { version: 1, status: 'needs_input', questions: [{ question: '整理结果还是制定方案？', options: ['整理结果', '制定方案'] }] } };
    mock.getIntake.mockResolvedValue(mock.project.intake);
    mock.submitIntake.mockResolvedValue({ id: 'job-2', status: 'queued' });
  });
  it('已完成的规划默认收起，但待补充材料仍然可见', async () => {
    mock.project.intake = { version: 3, status: 'ready', summary: '整理相关研究', paper_type: 'review', language: 'zh', material_issues: [{ code: 'missing', message: '请补充实验数据' }] };
    mock.getIntake.mockResolvedValue(mock.project.intake);
    render(<IntakePanel />);
    const toggle = screen.getByRole('button', { name: /展开规划/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText('请补充实验数据')).toBeVisible();
    expect(screen.queryByRole('link', { name: /查看并修改研究规划/ })).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.getByRole('link', { name: /查看并修改研究规划/ })).toHaveAttribute('href', '/projects/p1/scope');
  });
  it('刷新恢复必要澄清，提交答案带上当前版本', async () => {
    render(<IntakePanel />);
    await screen.findByText('整理结果还是制定方案？');
    fireEvent.click(screen.getByRole('button', { name: '整理结果' }));
    fireEvent.click(screen.getByRole('button', { name: '继续研究' }));
    await waitFor(() => expect(mock.submitIntake).toHaveBeenCalledWith('p1', { version: 1, answer: '整理结果还是制定方案？：整理结果' }));
    expect(mock.startJob).toHaveBeenCalled();
  });
  it('理解失败允许重试，不要求再建项目', async () => {
    mock.project.intake = { version: 2, status: 'failed', error: '模型暂不可用' };
    mock.getIntake.mockResolvedValue(mock.project.intake);
    render(<IntakePanel />);
    fireEvent.click(screen.getByRole('button', { name: '重试需求理解' }));
    await waitFor(() => expect(mock.submitIntake).toHaveBeenCalledWith('p1', { version: 2 }));
  });
  it('已有下游工作时锁定类型，保留语言和目标修正入口', async () => {
    mock.project.intake = { version: 3, status: 'ready', summary: '整理相关研究', paper_type: 'review', language: 'en', type_locked: true };
    mock.getIntake.mockResolvedValue(mock.project.intake);
    render(<IntakePanel />);
    fireEvent.click(screen.getByRole('button', { name: '修改理解' }));
    expect(screen.getByLabelText('论文类型')).toBeDisabled();
    expect(screen.getByLabelText('语言')).toHaveValue('en');
    fireEvent.change(screen.getByLabelText('研究目标'), { target: { value: '中文补充研究要求' } });
    fireEvent.click(screen.getByRole('button', { name: '保存并更新规划' }));
    await waitFor(() => expect(mock.submitIntake).toHaveBeenCalledWith('p1', expect.objectContaining({ version: 3, topic: '中文补充研究要求', overrides: expect.objectContaining({ language: 'en', paper_type: 'review' }) })));
  });
  it('只修改合作方式与引用格式时不额外调用理解模型', async () => {
    mock.project.intake = { version: 3, status: 'ready', paper_type: 'review', language: 'zh', type_locked: false };
    mock.getIntake.mockResolvedValue(mock.project.intake);
    mock.updateProject.mockResolvedValue(mock.project);
    render(<IntakePanel />);
    fireEvent.click(screen.getByRole('button', { name: '修改理解' }));
    fireEvent.click(screen.getByRole('radio', { name: /全自动/ }));
    fireEvent.click(screen.getByText('投稿要求', { selector: 'summary' }));
    fireEvent.change(screen.getByLabelText('引用样式'), { target: { value: 'ieee' } });
    fireEvent.click(screen.getByRole('button', { name: '保存并更新规划' }));
    await waitFor(() => expect(mock.updateProject).toHaveBeenCalledWith('p1', { writing_mode: 'auto', citation_style: 'ieee', venue_template: 'article' }));
    expect(mock.submitIntake).not.toHaveBeenCalled();
  });

  it('重新理解失败时不把旧摘要和材料记录显示为本次结果', async () => {
    mock.project.intake = { version: 4, status: 'failed', summary: '上一版目标', materials: [{ id: 'a', title: 'previous.csv', parsed: true, used: true }] };
    mock.getIntake.mockResolvedValue(mock.project.intake);
    render(<IntakePanel />);
    await screen.findByRole('button', { name: '重试需求理解' });
    expect(screen.queryByText('上一版目标')).not.toBeInTheDocument();
    expect(screen.queryByText('本次规划使用的材料')).not.toBeInTheDocument();
  });

});
