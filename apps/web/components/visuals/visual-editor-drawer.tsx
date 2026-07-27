'use client';

import * as React from 'react';
import { Info, Loader2, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Drawer } from '@/components/ui/drawer';
import { draftVisual } from '@/lib/api';
import type {
  CreateVisualRequest,
  ImageProviderCapabilities,
  UserAsset,
  VisualAsset,
  VisualKind,
} from '@/lib/types';

const FIELD = 'h-9 w-full rounded-md border bg-background px-3 text-sm';

/**
 * 视觉编辑抽屉。视觉工作台与写作台**共用同一个实例定义**。
 *
 * 此前「调整规格 / 提示词」只存在于素材中心，写作台里看到一条不合适的建议
 * 只能跳到另一个页面去改。现在两边点「调整」打开的是同一个编辑器。
 */
export function VisualEditorDrawer({
  open,
  onClose,
  projectId,
  visual,
  assets,
  capabilities,
  aiGenerationAvailable = false,
  targetSectionKey,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  projectId: string;
  /** null 表示新建一张图。 */
  visual: VisualAsset | null;
  assets: UserAsset[];
  capabilities: ImageProviderCapabilities | null;
  /** AI 生图是否可用；不可用时新建里的「AI 插图」要说明原因而不是凭空消失。 */
  aiGenerationAvailable?: boolean;
  /** 草稿生成时给模型的章节上下文。 */
  targetSectionKey?: string | null;
  onSubmit: (payload: Partial<CreateVisualRequest>) => void;
}) {
  const creating = visual === null;
  const spec = (visual?.spec ?? {}) as Record<string, unknown>;

  // 新建时类型可选；编辑时后端不允许改 kind（会 409），因此锁定。
  const [kind, setKind] = React.useState<VisualKind>('diagram');
  const [caption, setCaption] = React.useState('');
  const [altText, setAltText] = React.useState('');
  const [prompt, setPrompt] = React.useState('');
  const [quality, setQuality] = React.useState('medium');
  const [size, setSize] = React.useState('1536x1024');
  const [nodes, setNodes] = React.useState('');
  const [edges, setEdges] = React.useState('');
  const [chartType, setChartType] = React.useState('bar');
  const [assetRef, setAssetRef] = React.useState('');
  /** 「一句话想画什么」——新建时唯一必填的东西，其余交给模型补。 */
  const [intent, setIntent] = React.useState('');
  const [drafting, setDrafting] = React.useState(false);
  const [draftedBy, setDraftedBy] = React.useState<string | null>(null);
  const [advanced, setAdvanced] = React.useState(false);

  const tables = React.useMemo(
    // 只有解析出至少两列的表格才能作图；没有 asset_ref 的素材无法被 spec 引用。
    () => assets.filter((asset) => Boolean(asset.asset_ref) && (asset.headers?.length ?? 0) >= 2),
    [assets],
  );

  // 每次打开都从当前资产重置：抽屉是复用的，残留上一张图的文本比空白更糟。
  React.useEffect(() => {
    if (!open) return;
    setKind((visual?.kind as VisualKind) ?? 'diagram');
    setIntent('');
    setDraftedBy(null);
    setAdvanced(false);
    setAssetRef(
      typeof spec.source_asset_ref === 'string'
        ? spec.source_asset_ref
        : (tables[0]?.asset_ref ?? ''),
    );
    setCaption(visual?.caption ?? '');
    setAltText(visual?.alt_text ?? '');
    setPrompt(typeof spec.prompt === 'string' ? spec.prompt : '');
    setQuality(typeof spec.quality === 'string' ? spec.quality : 'medium');
    setSize(typeof spec.size === 'string' ? spec.size : '1536x1024');
    setChartType(typeof spec.chart_type === 'string' ? spec.chart_type : 'bar');
    const rawNodes = Array.isArray(spec.nodes) ? (spec.nodes as Record<string, unknown>[]) : [];
    setNodes(rawNodes.map((node) => String(node.label ?? '')).join('\n'));
    const rawEdges = Array.isArray(spec.edges) ? (spec.edges as Record<string, unknown>[]) : [];
    const idToIndex = new Map(rawNodes.map((node, index) => [String(node.id ?? ''), index + 1]));
    setEdges(
      rawEdges
        .map((edge) => {
          const from = idToIndex.get(String(edge.source ?? ''));
          const to = idToIndex.get(String(edge.target ?? ''));
          return from && to ? `${from} -> ${to}` : '';
        })
        .filter(Boolean)
        .join('\n'),
    );
    // spec 是每次渲染新建的对象引用，不入依赖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, visual?.id]);

  const submit = () => {
    const payload: Partial<CreateVisualRequest> = { caption, alt_text: altText };
    if (kind === 'ai_image') {
      payload.spec = {
        ...spec,
        kind: 'ai_image',
        prompt,
        quality,
        // 提供商声明支持尺寸时才下发；不支持时保持原值，界面也不做承诺。
        size: capabilities && capabilities.supported_sizes.length > 0 ? size : spec.size,
      };
    } else if (kind === 'diagram') {
      const labels = nodes
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean)
        .slice(0, 30);
      const parsedNodes = labels.map((label, index) => ({ id: `n${index + 1}`, label }));
      const parsedEdges = edges
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
          const [from, to] = line.split('->').map((part) => Number(part.trim()));
          return from && to && from <= labels.length && to <= labels.length
            ? { source: `n${from}`, target: `n${to}` }
            : null;
        })
        .filter((edge): edge is { source: string; target: string } => edge !== null);
      payload.spec = { ...spec, kind: 'diagram', nodes: parsedNodes, edges: parsedEdges };
    } else {
      const source = tables.find((asset) => asset.asset_ref === assetRef);
      payload.spec = {
        ...spec,
        kind: 'chart',
        chart_type: chartType,
        source_asset_ref: assetRef,
        // 新建图表时坐标轴取素材的前两列；细调留给「调整」。
        ...(creating && source
          ? { x: source.headers[0], y: [source.headers[1]] }
          : {}),
      };
    }
    if (creating) payload.title = caption;
    onSubmit(payload);
    onClose();
  };

  /**
   * 一句话 → 完整规格。
   *
   * 手写图注、替代文本、构图描述三段文本，还要自己避开会触发内容审核的词——
   * 那是把提示词工程外包给了作者。这里只问「你想画什么」，其余让模型补。
   */
  const runDraft = async () => {
    if (kind === 'chart') return;
    setDrafting(true);
    try {
      const result = await draftVisual(projectId, {
        kind,
        intent: intent.trim(),
        target_section_key: targetSectionKey ?? null,
      });
      const draft = result.data;
      if (!draft) return;
      setCaption(draft.caption);
      setAltText(draft.alt_text);
      setDraftedBy(draft.generator);
      const drafted = draft.spec as Record<string, unknown>;
      if (typeof drafted.prompt === 'string') setPrompt(drafted.prompt);
      const raw = Array.isArray(drafted.nodes)
        ? (drafted.nodes as Record<string, unknown>[])
        : [];
      if (raw.length > 0) {
        setNodes(raw.map((node) => String(node.label ?? '')).join('\n'));
        const idToIndex = new Map(raw.map((node, i) => [String(node.id ?? ''), i + 1]));
        const rawEdges = Array.isArray(drafted.edges)
          ? (drafted.edges as Record<string, unknown>[])
          : [];
        setEdges(
          rawEdges
            .map((edge) => {
              const from = idToIndex.get(String(edge.source ?? ''));
              const to = idToIndex.get(String(edge.target ?? ''));
              return from && to ? `${from} -> ${to}` : '';
            })
            .filter(Boolean)
            .join('\n'),
        );
      }
    } finally {
      setDrafting(false);
    }
  };

  const valid =
    caption.trim().length > 0 &&
    altText.trim().length > 0 &&
    (kind !== 'ai_image' || prompt.trim().length >= 10) &&
    (kind !== 'chart' || Boolean(assetRef)) &&
    (kind !== 'diagram' || nodes.split('\n').filter((line) => line.trim()).length >= 2);

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={creating ? '新建视觉' : '调整视觉'}
      description={
        creating
          ? '图表与示意图由本地渲染，不调用外部服务；AI 插图创建后还要再确认一次才会生成。'
          : '改完保存；已有预览的资产会自动生成一个新版本，旧版本仍可回看比较。'
      }
      className="max-w-lg"
      footer={
        <>
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button onClick={submit} disabled={!valid}>
            {creating ? '创建' : '保存'}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {creating && (
          <fieldset className="space-y-1.5 text-sm">
            <legend className="mb-1">类型</legend>
            <div className="grid gap-1.5 sm:grid-cols-3">
              <KindOption
                id="diagram"
                label="学术示意图"
                hint="节点与连线，信息可读"
                current={kind}
                onSelect={setKind}
              />
              <KindOption
                id="chart"
                label="数据图表"
                hint={
                  tables.length > 0 ? '从上传的表格素材出图' : '需要先上传含数值列的表格素材'
                }
                current={kind}
                onSelect={setKind}
                disabled={tables.length === 0}
              />
              <KindOption
                id="ai_image"
                label="AI 概念插图"
                hint={
                  aiGenerationAvailable
                    ? '调用外部服务，可能计费'
                    : '当前不可用：AI 生图未启用或未配置图像服务凭据'
                }
                current={kind}
                onSelect={setKind}
                disabled={!aiGenerationAvailable}
              />
            </div>
          </fieldset>
        )}

        {creating && kind !== 'chart' && (
          <div className="space-y-1.5 rounded-md border bg-muted/30 p-3">
            <label className="block space-y-1 text-sm">
              <span className="font-medium">想画什么？</span>
              <input
                className={FIELD}
                value={intent}
                onChange={(e) => setIntent(e.target.value)}
                placeholder="一句话就行，例如：根系受力后从裂纹萌生到断裂的过程"
              />
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" onClick={runDraft} disabled={drafting}>
                {drafting ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Sparkles className="h-3.5 w-3.5" />
                )}
                让 AI 补全
              </Button>
              <span className="text-xs text-muted-foreground">
                {draftedBy
                  ? draftedBy.startsWith('llm:')
                    ? '已由模型补全，下面可以再改。'
                    : '文本模型不可用，给了一份可直接提交的骨架。'
                  : '图注、替代文本与构图都会自动填好，不必手写。'}
              </span>
            </div>
          </div>
        )}

        <label className="block space-y-1 text-sm">
          <span>图注</span>
          <input className={FIELD} value={caption} onChange={(e) => setCaption(e.target.value)} />
        </label>
        <label className="block space-y-1 text-sm">
          <span>替代文本（alt）</span>
          <input className={FIELD} value={altText} onChange={(e) => setAltText(e.target.value)} />
        </label>

        {kind === 'ai_image' && (
          <>
            <label className="block space-y-1 text-sm">
              <span>概念描述</span>
              <textarea
                className="min-h-32 w-full rounded-md border bg-background p-3 text-sm"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                maxLength={capabilities?.prompt_max_length ?? 2048}
                placeholder="描述构图与元素关系，不要写文字标签——生成的文字必然是乱码"
              />
            </label>
            <button
              type="button"
              onClick={() => setAdvanced((v) => !v)}
              className="text-xs text-muted-foreground underline underline-offset-2"
            >
              {advanced ? '收起高级选项' : '高级选项（提供商与质量）'}
            </button>
            {/*
              提供商型号、尺寸、质量属于**协议细节**：默认折叠。把一串
              `@cf/black-forest-labs/flux-1-schnell` 摊在新建表单里，
              既没让用户少做决定，也没让结果变好。
            */}
            {advanced && (
            <>
            <ProviderCapabilityNotice capabilities={capabilities} />
            <div className="grid gap-3 sm:grid-cols-2">
              {/*
                只有提供商真的支持尺寸时才给这个下拉框。Cloudflare FLUX 的请求体
                只有 prompt 与 steps，此前界面却让用户在「横向 3:2 / 方形 / 竖向」
                之间选——选完仍然拿到方图。
              */}
              {capabilities && capabilities.supported_sizes.length > 0 ? (
                <label className="space-y-1 text-sm">
                  <span>尺寸</span>
                  <select className={FIELD} value={size} onChange={(e) => setSize(e.target.value)}>
                    {capabilities.supported_sizes.map((item) => (
                      <option key={item} value={item}>
                        {item}
                      </option>
                    ))}
                  </select>
                </label>
              ) : (
                <label className="space-y-1 text-sm">
                  <span>尺寸</span>
                  <p className={`${FIELD} flex items-center text-muted-foreground`}>
                    由提供商决定
                  </p>
                </label>
              )}
              <label className="space-y-1 text-sm">
                <span>质量</span>
                <select
                  className={FIELD}
                  value={quality}
                  onChange={(e) => setQuality(e.target.value)}
                >
                  {(capabilities?.quality_modes ?? ['low', 'medium', 'high']).map((mode) => (
                    <option key={mode} value={mode}>
                      {mode === 'low' ? '低' : mode === 'medium' ? '中' : '高'}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            </>
            )}
          </>
        )}

        {kind === 'diagram' && (
          <>
            <label className="block space-y-1 text-sm">
              <span>节点（每行一个）</span>
              <textarea
                className="min-h-28 w-full rounded-md border bg-background p-3 text-sm"
                value={nodes}
                onChange={(e) => setNodes(e.target.value)}
              />
            </label>
            <label className="block space-y-1 text-sm">
              <span>连线（每行 `起点序号 -&gt; 终点序号`）</span>
              <textarea
                className="min-h-20 w-full rounded-md border bg-background p-3 font-mono text-sm"
                value={edges}
                onChange={(e) => setEdges(e.target.value)}
                placeholder="1 -> 2"
              />
            </label>
          </>
        )}

        {kind === 'chart' && (
          <>
            <label className="block space-y-1 text-sm">
              <span>图表类型</span>
              <select
                className={FIELD}
                value={chartType}
                onChange={(e) => setChartType(e.target.value)}
              >
                <option value="bar">柱状图</option>
                <option value="line">折线图</option>
                <option value="scatter">散点图</option>
              </select>
            </label>
            {creating ? (
              <label className="block space-y-1 text-sm">
                <span>数据来源</span>
                <select
                  className={FIELD}
                  value={assetRef}
                  onChange={(e) => setAssetRef(e.target.value)}
                >
                  {tables.map((asset) => (
                    <option key={asset.asset_ref ?? asset.id} value={asset.asset_ref ?? ''}>
                      {asset.title ?? asset.asset_ref ?? '未命名素材'}
                    </option>
                  ))}
                </select>
                <span className="block text-xs text-muted-foreground">
                  数值由代码直接从素材展开，模型不参与——图里的每个数字都能追回这份表格。
                </span>
              </label>
            ) : (
              <p className="text-xs text-muted-foreground">
                数据来源：{describeSource(spec, assets)}。更换数据源需要新建一个图表。
              </p>
            )}
          </>
        )}
      </div>
    </Drawer>
  );
}

/** 新建时的类型选择。不可用的类型**保留但禁用**，并写清楚为什么。 */
function KindOption({
  id,
  label,
  hint,
  current,
  onSelect,
  disabled,
}: {
  id: VisualKind;
  label: string;
  hint: string;
  current: VisualKind;
  onSelect: (kind: VisualKind) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(id)}
      disabled={disabled}
      aria-pressed={current === id}
      title={hint}
      className={[
        'rounded-md border px-2.5 py-2 text-left transition-colors',
        current === id ? 'border-primary bg-accent' : 'hover:bg-accent/50',
        disabled ? 'cursor-not-allowed opacity-50' : '',
      ].join(' ')}
    >
      <span className="block text-sm font-medium">{label}</span>
      <span className="mt-0.5 block text-xs text-muted-foreground">{hint}</span>
    </button>
  );
}

function describeSource(spec: Record<string, unknown>, assets: UserAsset[]): string {
  const ref = typeof spec.source_asset_ref === 'string' ? spec.source_asset_ref : '';
  const match = assets.find((asset) => asset.asset_ref === ref);
  return match?.title || ref || '未知素材';
}

/** 把提供商的能力边界写在用户眼前，而不是让他试出来。 */
function ProviderCapabilityNotice({
  capabilities,
}: {
  capabilities: ImageProviderCapabilities | null;
}) {
  if (!capabilities) return null;
  return (
    <p className="flex items-start gap-1.5 rounded border bg-muted/40 px-2 py-1.5 text-xs text-muted-foreground">
      <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>
        {capabilities.provider} · {capabilities.model}
        {capabilities.note ? `——${capabilities.note}` : ''}
      </span>
    </p>
  );
}
