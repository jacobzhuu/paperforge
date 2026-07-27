import * as React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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
  regenerateVisual: vi.fn(),
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
    draftVisual.mockReturnValue(
      ok({
        title: '根系断裂过程',
        caption: '根系受力后从裂纹萌生到断裂的过程',
        alt_text: '从受力到断裂的四阶段示意',
        spec: {
          kind: 'ai_image',
          prompt: 'root fracture progression, abstract academic illustration',
        },
        generator: 'llm:stub',
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

  it('Cloudflare 不显示尺寸承诺——它根本不接受尺寸参数', async () => {
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

    await userEvent.click(await screen.findByRole('button', { name: '调整' }));
    // 尺寸/质量属于协议细节，默认折叠——展开才看得到。
    await userEvent.click(await screen.findByRole('button', { name: /高级选项/ }));
    expect(await screen.findByText('由提供商决定')).toBeInTheDocument();
    // 旧界面里那三个不会生效的选项必须消失。
    expect(screen.queryByRole('option', { name: /横向 3:2/ })).not.toBeInTheDocument();
  });

  it('批准时带上章节的 updated_at 做乐观并发', async () => {
    render(<VisualsWorkbench />);
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
    expect(screen.getByText(/重试不会改变结果/)).toBeInTheDocument();
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

  it('能自己新建一张视觉——不只是被动接受建议', async () => {
    /*
     * 回归防线：把旧的 visuals-gallery 删掉时，连同它的「新建」对话框一起没了，
     * 于是用户只能处理规划器提出的建议，想自己加一张图完全无路可走。
     */
    render(<VisualsWorkbench />);
    await userEvent.click(await screen.findByRole('button', { name: /新建视觉/ }));

    // 用 heading 定位抽屉标题：页面上还有一个同名的按钮。
    expect(await screen.findByRole('heading', { name: '新建视觉' })).toBeInTheDocument();

    /*
     * 用 fireEvent.change 直接赋值，而不是逐键 type：抽屉打开时的焦点陷阱会在
     * 一个 requestAnimationFrame 后把焦点移到首个可聚焦元素，逐键输入会在那一刻
     * 被打断。真实浏览器里这个 rAF 在开屏 ~16ms 内就跑完了，人不可能打得那么快；
     * 这里要验的是「表单能提交成什么」，不是输入法时序。
     */
    fireEvent.change(screen.getByLabelText('图注'), { target: { value: '手工示意图' } });
    fireEvent.change(screen.getByLabelText('替代文本（alt）'), {
      target: { value: '手工画的流程' },
    });
    fireEvent.change(screen.getByLabelText('节点（每行一个）'), {
      target: { value: '输入\n输出' },
    });
    await userEvent.click(screen.getByRole('button', { name: '创建' }));

    await waitFor(() => expect(createVisual).toHaveBeenCalled());
    const payload = createVisual.mock.calls[0][1] as { spec: { kind: string } };
    expect(payload.spec.kind).toBe('diagram');
  });

  it('AI 生图不可用时说明原因，而不是让选项凭空消失', async () => {
    getRuntimeSettings.mockReturnValue(
      ok({ ai_images_enabled: false, image_provider_configured: false, image_capabilities: null }),
    );
    render(<VisualsWorkbench />);

    expect(await screen.findByText(/AI_IMAGES_ENABLED/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /新建视觉/ }));
    const aiOption = await screen.findByRole('button', { name: /AI 概念插图/ });
    expect(aiOption).toBeDisabled();
    expect(aiOption).toHaveAccessibleName(/未启用|未配置/);
  });

  it('AI 可用时新建里的插图选项是开放的', async () => {
    render(<VisualsWorkbench />);
    await userEvent.click(await screen.findByRole('button', { name: /新建视觉/ }));
    expect(await screen.findByRole('button', { name: /AI 概念插图/ })).toBeEnabled();
    expect(screen.queryByText(/AI_IMAGES_ENABLED/)).not.toBeInTheDocument();
  });

  it('AI 插图只问一句话，图注与描述由模型补全', async () => {
    /*
     * 此前新建一张 AI 插图要手写图注、替代文本、构图描述三段文本，还得自己避开
     * 会触发内容审核的词——那是把提示词工程外包给了作者。
     */
    render(<VisualsWorkbench />);
    await userEvent.click(await screen.findByRole('button', { name: /新建视觉/ }));
    await userEvent.click(await screen.findByRole('button', { name: /AI 概念插图/ }));

    fireEvent.change(screen.getByLabelText('想画什么？'), {
      target: { value: '根系受力后的断裂过程' },
    });
    await userEvent.click(screen.getByRole('button', { name: /让 AI 补全/ }));

    await waitFor(() =>
      expect(draftVisual).toHaveBeenCalledWith('p1', {
        kind: 'ai_image',
        intent: '根系受力后的断裂过程',
        target_section_key: 'introduction',
      }),
    );
    // 三个字段都被填好，用户不必自己写。
    await waitFor(() =>
      expect((screen.getByLabelText('图注') as HTMLInputElement).value).toBe(
        '根系受力后从裂纹萌生到断裂的过程',
      ),
    );
    expect((screen.getByLabelText('替代文本（alt）') as HTMLInputElement).value).toBe(
      '从受力到断裂的四阶段示意',
    );
    expect((screen.getByLabelText('概念描述') as HTMLTextAreaElement).value).toContain(
      'root fracture progression',
    );
  });

  it('厂商型号与尺寸质量默认折叠——那是协议细节，不是创作决定', async () => {
    render(<VisualsWorkbench />);
    await userEvent.click(await screen.findByRole('button', { name: /新建视觉/ }));
    await userEvent.click(await screen.findByRole('button', { name: /AI 概念插图/ }));

    expect(screen.queryByText(/flux-1-schnell/)).not.toBeInTheDocument();
    expect(screen.queryByText('由提供商决定')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /高级选项/ }));
    expect(await screen.findByText(/flux-1-schnell/)).toBeInTheDocument();
  });

  it('建议基于旧版正文时给出提示，但不删除资产', async () => {
    listVisuals.mockReturnValue(ok([makeVisual({ id: 'v1', stale: true })]));
    render(<VisualsWorkbench />);

    expect(await screen.findByText(/建议基于旧版正文/)).toBeInTheDocument();
    expect(screen.getByTestId('visual-card')).toBeInTheDocument();
  });
});
