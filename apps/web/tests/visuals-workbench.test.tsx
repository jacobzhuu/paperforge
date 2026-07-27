import * as React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fail, makeSection, makeToastSpy, makeVisual, mockProjectContext, ok } from './helpers';

const projectCtx = { current: mockProjectContext() };
const toastSpy = makeToastSpy();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => '/projects/p1/visuals',
}));

vi.mock('@/components/project/project-context', () => ({
  useProject: () => projectCtx.current,
  useJobFinished: () => {},
  useJobEvent: () => {},
}));

vi.mock('@/components/ui/toast', () => ({
  useToast: () => ({ toast: toastSpy.toast }),
}));

const listVisuals = vi.fn();
const listSections = vi.fn();
const listAssets = vi.fn();
const getRuntimeSettings = vi.fn();
const generateVisual = vi.fn();
const approveVisual = vi.fn();
const createVisual = vi.fn();
const draftVisual = vi.fn();
const regenerateVisual = vi.fn();

vi.mock('@/lib/api', () => ({
  listVisuals: (...a: unknown[]) => listVisuals(...a),
  listSections: (...a: unknown[]) => listSections(...a),
  listAssets: (...a: unknown[]) => listAssets(...a),
  getRuntimeSettings: (...a: unknown[]) => getRuntimeSettings(...a),
  generateVisual: (...a: unknown[]) => generateVisual(...a),
  approveVisual: (...a: unknown[]) => approveVisual(...a),
  createVisual: (...a: unknown[]) => createVisual(...a),
  draftVisual: (...a: unknown[]) => draftVisual(...a),
  updateVisual: vi.fn(),
  regenerateVisual: (...a: unknown[]) => regenerateVisual(...a),
  rejectVisual: vi.fn(() => ok(undefined)),
  suggestVisuals: vi.fn(() => ok({ id: 'job-1' })),
  visualRenditionUrl: () => '#',
}));

import { VisualsWorkbench } from '@/components/visuals/visuals-workbench';

const CLOUDFLARE_CAPS = {
  provider: 'cloudflare',
  model: '@cf/black-forest-labs/flux-1-schnell',
  supported_sizes: [],
  supported_aspect_ratios: [],
  quality_modes: ['low', 'medium', 'high'],
  prompt_max_length: 2048,
  supports_negative_prompt: false,
  supports_seed: false,
  fixed_output_size: null,
  cost_estimate_available: false,
  note: 'Cloudflare Workers AI 只接受提示词与步数',
};

describe('视觉工作台', () => {
  beforeEach(() => {
    projectCtx.current = mockProjectContext();
    listSections.mockReturnValue(ok([makeSection()]));
    listAssets.mockReturnValue(ok([]));
    getRuntimeSettings.mockReturnValue(
      ok({
        ai_images_enabled: true,
        image_provider_configured: true,
        image_capabilities: CLOUDFLARE_CAPS,
      }),
    );
    generateVisual.mockReturnValue(ok({ id: 'job-1', kind: 'visual' }));
    approveVisual.mockReturnValue(ok(makeVisual({ id: 'v1', review_status: 'approved' })));
    createVisual.mockReturnValue(ok(makeVisual({ id: 'new1' })));
    regenerateVisual.mockReturnValue(ok(makeVisual({ id: 'v2', version: 2, generation_status: 'proposed' })));
    draftVisual.mockReturnValue(
      ok({
        kind: 'ai_image',
        title: '根系断裂过程',
        caption: '根系受力后从裂纹萌生到断裂的过程',
        alt_text: '从受力到断裂的四阶段示意',
        spec: {
          kind: 'ai_image',
          prompt: 'root fracture progression, abstract academic illustration',
        },
        generator: 'llm:stub',
        target_section_key: 'introduction',
        suggested_block_index: 1,
        reason: '意图是非精确的概念表达，适合概念插图。',
        context_summary: '已匹配章节「引言」',
        warnings: [],
      }),
    );
    listVisuals.mockReturnValue(ok([makeVisual({ id: 'v1' })]));
  });

  it('章节接口失败时视觉卡片仍可用——正文拿不到不该让整页视觉消失', async () => {
    listSections.mockReturnValue(fail());
    render(<VisualsWorkbench />);

    expect(await screen.findByText(/章节列表未能加载/)).toBeInTheDocument();
    expect(await screen.findByTestId('visual-card')).toBeInTheDocument();
  });

  it('设置接口失败只提示配置模块，卡片与确定性生成不受影响', async () => {
    getRuntimeSettings.mockReturnValue(fail());
    render(<VisualsWorkbench />);

    expect(await screen.findByText(/图像服务配置未能加载/)).toBeInTheDocument();
    expect(await screen.findByTestId('visual-card')).toBeInTheDocument();
  });

  it('视觉列表失败时给出重试入口，而不是空白页', async () => {
    listVisuals.mockReturnValue(fail());
    render(<VisualsWorkbench />);

    expect(await screen.findByText('加载失败')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /重试/ })).toBeInTheDocument();
  });

  it('AI 生图必须先过确认框——不确认就不发请求', async () => {
    listVisuals.mockReturnValue(
      ok([
        makeVisual({
          id: 'ai1',
          kind: 'ai_image',
          generation_status: 'proposed',
          spec: { kind: 'ai_image', prompt: '抽象科研流程', quality: 'medium' },
          resolved_prompt: '抽象科研流程. Style: academic flat vector. no text.',
        }),
      ]),
    );
    render(<VisualsWorkbench />);

    await userEvent.click(await screen.findByRole('button', { name: '生成预览' }));

    // 对话框出现，但请求还没发出去。
    expect(await screen.findByText('调用外部图像服务生成插图？')).toBeInTheDocument();
    expect(generateVisual).not.toHaveBeenCalled();

    // 展示的是**真正会发出去**的那一句，由后端渲染，界面不自己拼。
    expect(screen.getByText(/Style: academic flat vector/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '确认生成' }));
    await waitFor(() => expect(generateVisual).toHaveBeenCalledWith('p1', 'ai1'));
  });

  it('取消确认框不会发起任何生成请求', async () => {
    listVisuals.mockReturnValue(
      ok([makeVisual({ id: 'ai1', kind: 'ai_image', generation_status: 'proposed' })]),
    );
    render(<VisualsWorkbench />);

    await userEvent.click(await screen.findByRole('button', { name: '生成预览' }));
    await userEvent.click(await screen.findByRole('button', { name: '取消' }));
    expect(generateVisual).not.toHaveBeenCalled();
  });

  it('确定性图表不弹确认框：它没有外部调用也不计费', async () => {
    listVisuals.mockReturnValue(
      ok([makeVisual({ id: 'd1', kind: 'diagram', generation_status: 'proposed' })]),
    );
    render(<VisualsWorkbench />);

    await userEvent.click(await screen.findByRole('button', { name: '生成预览' }));
    await waitFor(() => expect(generateVisual).toHaveBeenCalledWith('p1', 'd1'));
    expect(screen.queryByText('调用外部图像服务生成插图？')).not.toBeInTheDocument();
  });

  it('专业设置默认收起，厂商提示词不会挤占卡片主路径', async () => {
    listVisuals.mockReturnValue(
      ok([
        makeVisual({
          id: 'ai1',
          kind: 'ai_image',
          generation_status: 'proposed',
          spec: { kind: 'ai_image', prompt: '一段足够长的概念描述文本' },
        }),
      ]),
    );
    render(<VisualsWorkbench />);

    await userEvent.click(await screen.findByRole('button', { name: '更多' }));
    await userEvent.click(await screen.findByRole('menuitem', { name: /调整视觉/ }));
    expect(await screen.findByRole('heading', { name: '调整视觉' })).toBeInTheDocument();
    expect(screen.getByText('专业检查器')).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /横向 3:2/ })).not.toBeInTheDocument();
  });

  it('批准时带上章节的 updated_at 做乐观并发', async () => {
    render(<VisualsWorkbench />);
    await userEvent.click(await screen.findByRole('button', { name: /^插入论文$/ }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^插入论文$/ }));
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

  it('失败的视觉显示可执行提示与是否值得重试', async () => {
    listVisuals.mockReturnValue(
      ok([
        makeVisual({
          id: 'v1',
          generation_status: 'failed',
          error: {
            code: 'content_rejected',
            message: '提示词可能触发了图像服务的内容检查。改写描述后重试。',
            retryable: false,
            request_id: 'cf-ray-123',
          },
        }),
      ]),
    );
    render(<VisualsWorkbench />);

    expect(await screen.findByText(/触发了图像服务的内容检查/)).toBeInTheDocument();
    expect(screen.getByText(/需要先调整描述/)).toBeInTheDocument();
    expect(screen.getByText('cf-ray-123')).toBeInTheDocument();
  });

  it('可重试的失败给的是不同的操作建议', async () => {
    listVisuals.mockReturnValue(
      ok([
        makeVisual({
          id: 'v1',
          generation_status: 'failed',
          error: {
            code: 'visuald_unavailable',
            message: '图形渲染服务（visuald）不可用。确认该服务已启动后重试。',
            retryable: true,
          },
        }),
      ]),
    );
    render(<VisualsWorkbench />);

    expect(await screen.findByText(/这类失败重试通常有效/)).toBeInTheDocument();
  });

  it('确定性渲染最多并行两个——第三张的生成按钮被禁用', async () => {
    /*
     * 上限不是为了省钱（确定性渲染不计费），而是别把 visuald 一次压垮：
     * 十张卡片一起开跑，结果往往是十张一起超时失败。
     */
    const jobs: Record<string, unknown> = {};
    let seq = 0;
    projectCtx.current = mockProjectContext({
      jobs,
      startJob: vi.fn(() => {
        const id = `job-${++seq}`;
        jobs[id] = { job: { id, kind: 'visual' }, label: '生成中', warnings: [] };
        return id;
      }),
    });
    listVisuals.mockReturnValue(
      ok(
        ['d1', 'd2', 'd3'].map((id) =>
          makeVisual({ id, kind: 'diagram', generation_status: 'proposed' }),
        ),
      ),
    );
    render(<VisualsWorkbench />);

    const buttons = await screen.findAllByRole('button', { name: '生成预览' });
    expect(buttons).toHaveLength(3);
    await userEvent.click(buttons[0]);
    await userEvent.click(buttons[1]);

    await waitFor(() => {
      const remaining = screen.getAllByRole('button', { name: '生成预览' });
      expect(remaining.every((button) => (button as HTMLButtonElement).disabled)).toBe(true);
    });
  });

  it('一句意图直接形成视觉，不出现图注、alt、节点或连线表单', async () => {
    draftVisual.mockReturnValue(
      ok({
        kind: 'diagram',
        title: '证据整合流程',
        caption: '证据整合流程',
        alt_text: '从检索到整合的流程图',
        spec: { kind: 'diagram', direction: 'LR', nodes: [{ id: 'n1', label: '检索' }] },
        generator: 'deterministic',
        target_section_key: 'introduction',
        suggested_block_index: 1,
        reason: '检测到流程意图，示意图更准确。',
        context_summary: '已匹配章节「引言」',
        warnings: [],
      }),
    );
    render(<VisualsWorkbench />);
    expect(screen.queryByLabelText('图注')).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/替代文本/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/节点/)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('视觉意图'), {
      target: { value: '展示检索、筛选和证据整合流程' },
    });
    await userEvent.click(screen.getByRole('button', { name: '开始创作' }));

    await waitFor(() => expect(createVisual).toHaveBeenCalled());
    const payload = createVisual.mock.calls[0][1] as { spec: { kind: string } };
    expect(payload.spec.kind).toBe('diagram');
    expect(generateVisual).toHaveBeenCalledWith('p1', 'new1');
  });

  it('AI 生图不可用时说明原因，并保留禁用的类型覆盖选项', async () => {
    getRuntimeSettings.mockReturnValue(
      ok({ ai_images_enabled: false, image_provider_configured: false, image_capabilities: null }),
    );
    render(<VisualsWorkbench />);

    expect(await screen.findByText(/AI 概念插图当前不可用/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /限定范围或覆盖类型/ }));
    const aiOption = await screen.findByRole('button', { name: '概念插图' });
    expect(aiOption).toBeDisabled();
  });

  it('AI 可用时类型覆盖选项开放', async () => {
    render(<VisualsWorkbench />);
    await userEvent.click(await screen.findByRole('button', { name: /限定范围或覆盖类型/ }));
    expect(await screen.findByRole('button', { name: '概念插图' })).toBeEnabled();
  });

  it('AI 插图只问一句话，草稿创建后不自动调用外部图像服务', async () => {
    render(<VisualsWorkbench />);
    await userEvent.click(await screen.findByRole('button', { name: /限定范围或覆盖类型/ }));
    await userEvent.click(await screen.findByRole('button', { name: '概念插图' }));
    fireEvent.change(screen.getByLabelText('视觉意图'), {
      target: { value: '根系受力后的断裂过程' },
    });
    await userEvent.click(screen.getByRole('button', { name: '开始创作' }));

    await waitFor(() =>
      expect(draftVisual).toHaveBeenCalledWith('p1', {
        kind: 'ai_image',
        intent: '根系受力后的断裂过程',
        target_section_key: null,
        source_asset_refs: [],
      }),
    );
    expect(createVisual).toHaveBeenCalled();
    expect(generateVisual).not.toHaveBeenCalled();
  });

  it('默认显示需要处理，已插入和已拒绝进入历史', async () => {
    listVisuals.mockReturnValue(ok([
      makeVisual({ id: 'pending', title: '待处理图' }),
      makeVisual({ id: 'approved', title: '已插入图', review_status: 'approved' }),
      makeVisual({ id: 'rejected', title: '已拒绝图', review_status: 'rejected' }),
    ]));
    render(<VisualsWorkbench />);
    expect(await screen.findByText('待处理图')).toBeInTheDocument();
    expect(screen.queryByText('已插入图')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /历史/ }));
    expect(await screen.findByText('已插入图')).toBeInTheDocument();
    expect(screen.getByText('已拒绝图')).toBeInTheDocument();
  });

  it('陈旧上下文合并为页面级提醒，但不删除资产', async () => {
    listVisuals.mockReturnValue(ok([makeVisual({ id: 'v1', stale: true })]));
    render(<VisualsWorkbench />);

    expect(await screen.findByText(/1 张视觉基于旧版正文/)).toBeInTheDocument();
    expect(screen.getByTestId('visual-card')).toBeInTheDocument();
  });
});
