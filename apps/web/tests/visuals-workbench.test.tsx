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
const prepareVisualGeneration = vi.fn();
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
  prepareVisualGeneration: (...a: unknown[]) => prepareVisualGeneration(...a),
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
  supports_seed: true,
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
    prepareVisualGeneration.mockImplementation((_projectId: string, _visualId: string) =>
      ok(makeVisual({
        id: _visualId,
        kind: 'ai_image',
        generation_status: 'proposed',
        spec: {
          kind: 'ai_image',
          prompt: 'A DeepSeek-composed academic figure prompt.',
          refined_prompt: 'A DeepSeek-composed academic figure prompt.',
        },
        resolved_prompt: 'A DeepSeek-composed academic figure prompt.',
      })),
    );
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

  it('已生成图片展示准确生成时间与实际生成方式', async () => {
    listVisuals.mockReturnValue(
      ok([
        makeVisual({
          id: 'ai1',
          kind: 'ai_image',
          generation_status: 'ready',
          provider: 'yunwu',
          model: 'gpt-image-1',
          generated_at: '2026-07-29T08:15:00Z',
          created_at: '2026-07-29T08:00:00Z',
        }),
      ]),
    );
    render(<VisualsWorkbench />);

    const metadata = await screen.findByTestId('visual-generation-metadata');
    expect(within(metadata).getByText(/生成时间：/)).toHaveTextContent(/2026/);
    expect(within(metadata).getByText(/生成方式：/)).toHaveTextContent('AI 生成');
    expect(metadata).not.toHaveTextContent('Yunwu');
    expect(metadata).not.toHaveTextContent('gpt-image-1');
  });

  it('历史选择依据和错误详情也不暴露供应商或 API', async () => {
    listVisuals.mockReturnValue(
      ok([
        makeVisual({
          id: 'ai1',
          kind: 'ai_image',
          generation_status: 'failed',
          suggestion_reason: 'Yunwu 已配置；自动模式优先生成插图。',
          error: {
            code: 'provider_unavailable',
            message: 'AI 生成暂时不可用。',
            retryable: true,
            detail: 'Yunwu API returned 503 from gpt-image-1',
            request_id: 'req-safe',
          },
        }),
      ]),
    );
    render(<VisualsWorkbench />);

    await userEvent.click(await screen.findByRole('button', { name: '更多' }));
    await userEvent.click(await screen.findByRole('menuitem', { name: /来源与详情/ }));
    expect(await screen.findByText('概念内容适合使用 AI 生成')).toBeInTheDocument();
    expect(screen.getByText(/追踪 ID/)).toBeInTheDocument();
    expect(screen.queryByText(/Yunwu|gpt-image-1|API/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /技术细节/ })).not.toBeInTheDocument();
  });

  it('卡片只区分 AI 生成与本地直出，未生成建议不伪造时间', async () => {
    listVisuals.mockReturnValue(
      ok([
        makeVisual({
          id: 'chart1',
          kind: 'chart',
          generation_status: 'ready',
          provider: 'visuald',
          generated_at: '2026-07-29T08:15:00Z',
        }),
        makeVisual({
          id: 'pending1',
          kind: 'ai_image',
          generation_status: 'proposed',
        }),
      ]),
    );
    render(<VisualsWorkbench />);

    const metadata = await screen.findAllByTestId('visual-generation-metadata');
    expect(metadata.some((item) => item.textContent?.includes('本地直出'))).toBe(true);
    expect(metadata.some((item) => item.textContent?.includes('AI 生成'))).toBe(true);
    expect(metadata.some((item) => item.textContent?.includes('尚未生成'))).toBe(true);
    expect(metadata.every((item) => !item.textContent?.includes('Matplotlib'))).toBe(true);
    expect(metadata.every((item) => !item.textContent?.includes('Graphviz'))).toBe(true);
  });

  it('设置接口失败只提示配置模块，卡片与确定性生成不受影响', async () => {
    getRuntimeSettings.mockReturnValue(fail());
    render(<VisualsWorkbench />);

    expect(await screen.findByText(/AI 生成配置未能加载/)).toBeInTheDocument();
    expect(await screen.findByTestId('visual-card')).toBeInTheDocument();
  });

  it('视觉列表失败时给出重试入口，而不是空白页', async () => {
    listVisuals.mockReturnValue(fail());
    render(<VisualsWorkbench />);

    expect(await screen.findByText('加载失败')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /重试/ })).toBeInTheDocument();
  });

  it('AI 生图必须先过确认框——不确认就不发请求', async () => {
    prepareVisualGeneration.mockReturnValue(
      ok(makeVisual({
        id: 'ai1',
        kind: 'ai_image',
        generation_status: 'proposed',
        spec: {
          kind: 'ai_image',
          prompt: '抽象科研流程',
          refined_prompt: '抽象科研流程. Style: academic flat vector. no text.',
        },
        resolved_prompt: '抽象科研流程. Style: academic flat vector. no text.',
      })),
    );
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
    expect(await screen.findByText('确认使用 AI 生成插图？')).toBeInTheDocument();
    expect(generateVisual).not.toHaveBeenCalled();

    // 展示的是**真正会发出去**的那一句，由后端渲染，界面不自己拼。
    expect(screen.getByText(/Style: academic flat vector/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '确认生成' }));
    await waitFor(() => expect(generateVisual).toHaveBeenCalledWith('p1', 'ai1'));
  });

  it('AI 确认框隐藏供应商、模型和 API 细节', async () => {
    getRuntimeSettings.mockReturnValue(
      ok({
        ai_images_enabled: true,
        image_provider_configured: true,
        image_capabilities: {
          ...CLOUDFLARE_CAPS,
          provider: 'yunwu',
          model: 'gpt-image-1',
          supported_sizes: ['1024x1024', '1536x1024', '1024x1536'],
          note: '通过 Yunwu OpenAI-compatible Images API 生成',
        },
      }),
    );
    listVisuals.mockReturnValue(
      ok([
        makeVisual({
          id: 'ai1',
          kind: 'ai_image',
          generation_status: 'proposed',
          spec: { kind: 'ai_image', prompt: '抽象科研流程', quality: 'medium' },
        }),
      ]),
    );
    render(<VisualsWorkbench />);

    await userEvent.click(await screen.findByRole('button', { name: '生成预览' }));
    expect(await screen.findByText('确认使用 AI 生成插图？')).toBeInTheDocument();
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('AI 生成')).toBeInTheDocument();
    expect(screen.queryByText(/约 6 步/)).not.toBeInTheDocument();
    expect(screen.queryByText('gpt-image-1')).not.toBeInTheDocument();
    expect(screen.queryByText('yunwu')).not.toBeInTheDocument();
    expect(screen.queryByText(/API/i)).not.toBeInTheDocument();
  });

  it('自动优化过的提示词在确认框里清晰标明', async () => {
    prepareVisualGeneration.mockReturnValue(
      ok(makeVisual({
        id: 'ai1',
        kind: 'ai_image',
        generation_status: 'proposed',
        spec: {
          kind: 'ai_image',
          prompt: '抽象科研流程',
          refined_prompt: 'A wide conceptual illustration of a research pipeline.',
        },
        resolved_prompt: 'A wide conceptual illustration of a research pipeline.',
      })),
    );
    listVisuals.mockReturnValue(
      ok([
        makeVisual({
          id: 'ai1',
          kind: 'ai_image',
          generation_status: 'proposed',
          spec: {
            kind: 'ai_image',
            prompt: '抽象科研流程',
            refined_prompt: 'A wide conceptual illustration of a research pipeline.',
          },
          resolved_prompt: 'A wide conceptual illustration of a research pipeline.',
        }),
      ]),
    );
    render(<VisualsWorkbench />);

    await userEvent.click(await screen.findByRole('button', { name: '生成预览' }));
    expect(await screen.findByText(/已自动优化/)).toBeInTheDocument();
    expect(
      screen.getByText(/A wide conceptual illustration of a research pipeline/),
    ).toBeInTheDocument();
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
    expect(screen.queryByText('确认使用 AI 生成插图？')).not.toBeInTheDocument();
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

  it('改动插图描述会丢掉旧的润色提示词，而不是留一句对不上的文案', async () => {
    /**
     * `refined_prompt` 是针对**当时那份描述**润色的。描述一改它就过期了，
     * 留着会让确认框展示一句与主题无关的提示词——而那句正是真正会发出去的。
     */
    listVisuals.mockReturnValue(
      ok([
        makeVisual({
          id: 'ai1',
          kind: 'ai_image',
          spec: {
            kind: 'ai_image',
            prompt: '抽象科研流程',
            semantics: { subject: '旧主题' },
            refined_prompt: 'A wide conceptual illustration of the previous subject.',
          },
        }),
      ]),
    );
    render(<VisualsWorkbench />);

    await userEvent.click(await screen.findByRole('button', { name: '更多' }));
    await userEvent.click(await screen.findByRole('menuitem', { name: /调整视觉/ }));
    await userEvent.click(await screen.findByText('专业检查器'));
    const subject = screen.getByLabelText('主题');
    await userEvent.clear(subject);
    await userEvent.type(subject, '新主题');
    await userEvent.click(screen.getByRole('button', { name: /生成新版本/ }));

    await waitFor(() => expect(regenerateVisual).toHaveBeenCalled());
    const payload = regenerateVisual.mock.calls[0][2] as { spec: Record<string, unknown> };
    expect(payload.spec.semantics).toEqual({ subject: '新主题' });
    expect(payload.spec.refined_prompt).toBeUndefined();
  });

  it('画面元素支持增删改和排序，并同步到最终提示词', async () => {
    listVisuals.mockReturnValue(
      ok([
        makeVisual({
          id: 'ai1',
          kind: 'ai_image',
          spec: {
            kind: 'ai_image',
            prompt: 'A sufficiently long fallback prompt',
            style: 'academic vector',
            semantics: {
              subject: '研究流程',
              composition: '从左到右',
              elements: ['数据', '模型'],
              text_policy: 'none',
            },
          },
        }),
      ]),
    );
    render(<VisualsWorkbench />);

    await userEvent.click(await screen.findByRole('button', { name: '更多' }));
    await userEvent.click(await screen.findByRole('menuitem', { name: /调整视觉/ }));
    await userEvent.click(await screen.findByText('专业检查器'));

    const first = screen.getByLabelText('画面元素 1');
    await userEvent.clear(first);
    await userEvent.type(first, '输入数据');
    await userEvent.click(screen.getByRole('button', { name: '添加画面元素' }));
    const third = screen.getByLabelText('画面元素 3');
    await userEvent.clear(third);
    await userEvent.type(third, '结论');
    await userEvent.click(screen.getByRole('button', { name: '上移画面元素 3' }));
    await userEvent.click(screen.getByRole('button', { name: '删除画面元素 1' }));

    expect(
      (screen.getByLabelText('给 DeepSeek 的生图要求') as HTMLTextAreaElement).value,
    ).toContain('elements: 结论, 模型');
    await userEvent.click(screen.getByRole('button', { name: /生成新版本/ }));

    await waitFor(() => expect(regenerateVisual).toHaveBeenCalled());
    const payload = regenerateVisual.mock.calls[0][2] as {
      spec: { semantics: { elements: string[] } };
    };
    expect(payload.spec.semantics.elements).toEqual(['结论', '模型']);
  });

  it('手动修改生图要求后作为下一轮 DeepSeek 分析输入保存', async () => {
    listVisuals.mockReturnValue(
      ok([
        makeVisual({
          id: 'ai1',
          kind: 'ai_image',
          spec: {
            kind: 'ai_image',
            prompt: 'A sufficiently long fallback prompt',
            semantics: { subject: '旧主题', elements: ['元素甲'] },
          },
        }),
      ]),
    );
    render(<VisualsWorkbench />);

    await userEvent.click(await screen.findByRole('button', { name: '更多' }));
    await userEvent.click(await screen.findByRole('menuitem', { name: /调整视觉/ }));
    await userEvent.click(await screen.findByText('专业检查器'));
    const finalPrompt = screen.getByLabelText('给 DeepSeek 的生图要求');
    await userEvent.clear(finalPrompt);
    await userEvent.type(
      finalPrompt,
      'A user-authored final prompt that must remain exactly in control.',
    );
    await userEvent.clear(screen.getByLabelText('主题'));
    await userEvent.type(screen.getByLabelText('主题'), '新主题');

    expect(screen.getByText(/保存后 DeepSeek 会结合论文全文/)).toBeInTheDocument();
    expect(finalPrompt).toHaveValue(
      'A user-authored final prompt that must remain exactly in control.',
    );
    await userEvent.click(screen.getByRole('button', { name: /生成新版本/ }));

    await waitFor(() => expect(regenerateVisual).toHaveBeenCalled());
    const payload = regenerateVisual.mock.calls[0][2] as {
      spec: Record<string, unknown>;
    };
    expect(payload.spec.prompt_override).toBe(
      'A user-authored final prompt that must remain exactly in control.',
    );
    expect(payload.spec.semantics).toEqual({ subject: '新主题', elements: ['元素甲'] });
  });

  it('只展示当前 provider 声明支持的高级参数', async () => {
    listVisuals.mockReturnValue(
      ok([
        makeVisual({
          id: 'ai1',
          kind: 'ai_image',
          spec: { kind: 'ai_image', prompt: 'A sufficiently long image prompt' },
        }),
      ]),
    );
    render(<VisualsWorkbench />);

    await userEvent.click(await screen.findByRole('button', { name: '更多' }));
    await userEvent.click(await screen.findByRole('menuitem', { name: /调整视觉/ }));
    await userEvent.click(await screen.findByText('专业检查器'));
    expect(screen.getByLabelText('随机种子')).toBeInTheDocument();
    expect(screen.queryByLabelText('负面提示词')).not.toBeInTheDocument();
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

  it('明确选择本地直出示意图时直接形成预览', async () => {
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
    await userEvent.click(screen.getByRole('button', { name: '本地直出 · 示意图' }));
    fireEvent.change(screen.getByLabelText('视觉意图'), {
      target: { value: '展示检索、筛选和证据整合流程' },
    });
    await userEvent.click(screen.getByRole('button', { name: '开始创作' }));

    await waitFor(() => expect(createVisual).toHaveBeenCalled());
    const payload = createVisual.mock.calls[0][1] as { spec: { kind: string } };
    expect(payload.spec.kind).toBe('diagram');
    expect(generateVisual).toHaveBeenCalledWith('p1', 'new1');
  });

  it('创建入口只展示自动选择、AI 生成和本地直出', async () => {
    getRuntimeSettings.mockReturnValue(
      ok({
        ai_images_enabled: true,
        image_provider_configured: true,
        image_capabilities: {
          ...CLOUDFLARE_CAPS,
          provider: 'yunwu',
          model: 'gpt-image-1',
          supported_sizes: ['1024x1024', '1536x1024', '1024x1536'],
        },
      }),
    );
    render(<VisualsWorkbench />);

    expect(await screen.findByRole('button', { name: '自动选择' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(screen.getByText(/概念内容优先使用 AI 生成/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '本地直出 · 示意图' }));
    expect(screen.getByText(/本地直出：适合表达步骤/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'AI 生成' }));
    expect(screen.getByText(/AI 生成：先创建可编辑草稿/)).toBeInTheDocument();
    expect(screen.queryByText(/Yunwu|Cloudflare|OpenAI|API/)).not.toBeInTheDocument();
  });

  it('AI 生图不可用时说明原因，并保留禁用的类型覆盖选项', async () => {
    getRuntimeSettings.mockReturnValue(
      ok({ ai_images_enabled: false, image_provider_configured: false, image_capabilities: null }),
    );
    render(<VisualsWorkbench />);

    expect((await screen.findAllByText(/AI 生成当前不可用/)).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole('button', { name: /限定正文范围或素材/ }));
    const aiOption = await screen.findByRole('button', { name: 'AI 生成' });
    expect(aiOption).toBeDisabled();
  });

  it('AI 可用时类型覆盖选项开放', async () => {
    render(<VisualsWorkbench />);
    await userEvent.click(await screen.findByRole('button', { name: /限定正文范围或素材/ }));
    expect(await screen.findByRole('button', { name: 'AI 生成' })).toBeEnabled();
  });

  it('AI 插图只问一句话，草稿创建后不自动调用外部图像服务', async () => {
    render(<VisualsWorkbench />);
    await userEvent.click(await screen.findByRole('button', { name: /限定正文范围或素材/ }));
    await userEvent.click(await screen.findByRole('button', { name: 'AI 生成' }));
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
