'use client';

import * as React from 'react';
import { ArrowDown, ArrowUp, ChevronDown, Plus, RotateCcw, Sparkles, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Drawer } from '@/components/ui/drawer';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import type {
  ImageProviderCapabilities,
  RegenerateVisualRequest,
  UserAsset,
  VisualAsset,
} from '@/lib/types';

type NodeRow = { id: string; label: string };
type EdgeRow = { source: string; target: string; label?: string };

/** Natural-language revision first; structured controls stay in an optional inspector. */
export function VisualEditorDrawer({
  open,
  onClose,
  visual,
  assets,
  capabilities,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  projectId: string;
  visual: VisualAsset | null;
  assets: UserAsset[];
  capabilities: ImageProviderCapabilities | null;
  aiGenerationAvailable?: boolean;
  targetSectionKey?: string | null;
  onSubmit: (payload: RegenerateVisualRequest) => void;
}) {
  const [instruction, setInstruction] = React.useState('');
  const [caption, setCaption] = React.useState('');
  const [altText, setAltText] = React.useState('');
  const [spec, setSpec] = React.useState<Record<string, unknown>>({});
  const [nodes, setNodes] = React.useState<NodeRow[]>([]);
  const [edges, setEdges] = React.useState<EdgeRow[]>([]);

  React.useEffect(() => {
    if (!open || !visual) return;
    setInstruction('');
    setCaption(visual.caption);
    setAltText(visual.alt_text);
    setSpec({ ...visual.spec });
    setNodes(
      Array.isArray(visual.spec.nodes)
        ? (visual.spec.nodes as NodeRow[]).map((node) => ({ ...node }))
        : [],
    );
    setEdges(
      Array.isArray(visual.spec.edges)
        ? (visual.spec.edges as EdgeRow[]).map((edge) => ({ ...edge }))
        : [],
    );
  }, [open, visual]);

  if (!visual) return null;

  const submit = () => {
    const nextSpec =
      visual.kind === 'diagram' ? { ...spec, nodes, edges } : spec;
    onSubmit({
      revision_instruction: instruction.trim() || undefined,
      // 结构化编辑与自然语言修订可以同时发生。后端会先采用这份 spec，再应用
      // revision_instruction；丢掉 spec 会让用户刚编辑的 elements 悄悄消失。
      spec: nextSpec,
      caption,
      alt_text: altText,
    });
    onClose();
  };

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title="调整视觉"
      description="用一句话说明变化。系统会创建新版本，当前版本不会被覆盖。"
      className="max-w-2xl"
      footer={
        <div className="flex w-full flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button variant="outline" className="h-11" onClick={onClose}>取消</Button>
          <Button className="h-11" onClick={submit} disabled={!instruction.trim() && !caption.trim()}>
            <Sparkles /> 生成新版本
          </Button>
        </div>
      }
    >
      <div className="space-y-6">
        <div className="space-y-2">
          <Label htmlFor={`revision-${visual.id}`}>你希望怎么改？</Label>
          <Textarea
            id={`revision-${visual.id}`}
            value={instruction}
            onChange={(event) => setInstruction(event.target.value)}
            placeholder="例如：减少节点、改成横向，并突出防御方法"
            className="min-h-28 text-base"
            autoFocus
          />
          <div className="flex flex-wrap gap-2">
            {suggestionsFor(visual.kind).map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                onClick={() => setInstruction(suggestion)}
                className="min-h-9 rounded-full border px-3 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              >
                {suggestion}
              </button>
            ))}
          </div>
        </div>

        <details className="group rounded-lg border">
          <summary className="flex min-h-12 cursor-pointer list-none items-center justify-between px-4 text-sm font-medium">
            专业检查器
            <ChevronDown className="h-4 w-4 transition-transform group-open:rotate-180" />
          </summary>
          <div className="space-y-5 border-t p-4">
            {visual.kind === 'chart' && (
              <ChartInspector spec={spec} assets={assets} onChange={setSpec} />
            )}
            {visual.kind === 'diagram' && (
              <DiagramInspector
                spec={spec}
                nodes={nodes}
                edges={edges}
                onSpecChange={setSpec}
                onNodesChange={setNodes}
                onEdgesChange={setEdges}
              />
            )}
            {visual.kind === 'ai_image' && (
              <AIImageInspector
                spec={spec}
                resolvedPrompt={visual.resolved_prompt}
                capabilities={capabilities}
                onChange={setSpec}
              />
            )}
          </div>
        </details>

        <details className="group rounded-lg border">
          <summary className="flex min-h-12 cursor-pointer list-none items-center justify-between px-4 text-sm font-medium">
            论文说明
            <ChevronDown className="h-4 w-4 transition-transform group-open:rotate-180" />
          </summary>
          <div className="space-y-4 border-t p-4">
            <div className="space-y-2">
              <Label htmlFor={`caption-${visual.id}`}>图注</Label>
              <Input id={`caption-${visual.id}`} value={caption} onChange={(event) => setCaption(event.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor={`alt-${visual.id}`}>替代文本</Label>
              <Textarea id={`alt-${visual.id}`} value={altText} onChange={(event) => setAltText(event.target.value)} />
            </div>
          </div>
        </details>
      </div>
    </Drawer>
  );
}

function ChartInspector({
  spec,
  assets,
  onChange,
}: {
  spec: Record<string, unknown>;
  assets: UserAsset[];
  onChange: (spec: Record<string, unknown>) => void;
}) {
  const ref = typeof spec.source_asset_ref === 'string' ? spec.source_asset_ref : '';
  const source = assets.find((asset) => asset.asset_ref === ref);
  const headers = source?.headers ?? [];
  const y = Array.isArray(spec.y) ? String(spec.y[0] ?? '') : '';
  return (
    <div className="space-y-4">
      <p className="rounded-md bg-muted/50 px-3 py-2 text-xs text-muted-foreground">
        数据来源：{source?.title || ref || '项目素材'}。自然语言修订不会改动数据源或实验数值。
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        <FieldSelect
          label="图表形式"
          value={String(spec.chart_type ?? 'bar')}
          options={[['bar', '柱状图'], ['line', '折线图'], ['scatter', '散点图'], ['box', '箱线图']]}
          onChange={(chart_type) => onChange({ ...spec, chart_type })}
        />
        <FieldSelect
          label="配色"
          value={String(spec.palette ?? 'colorblind')}
          options={[['colorblind', '色觉友好'], ['grayscale', '灰度'] ]}
          onChange={(palette) => onChange({ ...spec, palette })}
        />
        {headers.length > 0 && (
          <>
            <FieldSelect
              label="横轴"
              value={String(spec.x ?? '')}
              options={headers.map((header) => [header, header])}
              onChange={(x) => onChange({ ...spec, x })}
            />
            <FieldSelect
              label="纵轴"
              value={y}
              options={headers.map((header) => [header, header])}
              onChange={(nextY) => onChange({ ...spec, y: [nextY] })}
            />
          </>
        )}
      </div>
    </div>
  );
}

function DiagramInspector({
  spec,
  nodes,
  edges,
  onSpecChange,
  onNodesChange,
  onEdgesChange,
}: {
  spec: Record<string, unknown>;
  nodes: NodeRow[];
  edges: EdgeRow[];
  onSpecChange: (spec: Record<string, unknown>) => void;
  onNodesChange: (nodes: NodeRow[]) => void;
  onEdgesChange: (edges: EdgeRow[]) => void;
}) {
  const updateNode = (index: number, label: string) => {
    const next = [...nodes];
    next[index] = { ...next[index], label };
    onNodesChange(next);
  };
  return (
    <div className="space-y-5">
      <FieldSelect
        label="阅读方向"
        value={String(spec.direction ?? 'TB')}
        options={[['LR', '从左到右'], ['TB', '从上到下']]}
        onChange={(direction) => onSpecChange({ ...spec, direction })}
      />
      <div className="space-y-2">
        <Label>节点</Label>
        {nodes.map((node, index) => (
          <div key={node.id} className="flex items-center gap-2">
            <Input value={node.label} onChange={(event) => updateNode(index, event.target.value)} aria-label={`节点 ${index + 1}`} />
            <Button
              variant="ghost"
              size="icon"
              className="h-11 w-11"
              onClick={() => {
                onNodesChange(nodes.filter((item) => item.id !== node.id));
                onEdgesChange(edges.filter((edge) => edge.source !== node.id && edge.target !== node.id));
              }}
              aria-label={`删除节点 ${node.label}`}
            ><Trash2 /></Button>
          </div>
        ))}
        <Button
          variant="outline"
          size="sm"
          onClick={() => onNodesChange([...nodes, { id: `n${Date.now()}`, label: '新节点' }])}
        ><Plus /> 添加节点</Button>
      </div>
      <div className="space-y-2">
        <Label>关系</Label>
        {edges.map((edge, index) => (
          <div key={`${edge.source}-${edge.target}-${index}`} className="grid grid-cols-[1fr,auto,1fr,auto] items-center gap-2">
            <NodeSelect value={edge.source} nodes={nodes} onChange={(source) => {
              const next = [...edges]; next[index] = { ...edge, source }; onEdgesChange(next);
            }} />
            <span className="text-xs text-muted-foreground">连接到</span>
            <NodeSelect value={edge.target} nodes={nodes} onChange={(target) => {
              const next = [...edges]; next[index] = { ...edge, target }; onEdgesChange(next);
            }} />
            <Button variant="ghost" size="icon" className="h-11 w-11" onClick={() => onEdgesChange(edges.filter((_, i) => i !== index))} aria-label="删除关系"><Trash2 /></Button>
          </div>
        ))}
        {nodes.length >= 2 && (
          <Button variant="outline" size="sm" onClick={() => onEdgesChange([...edges, { source: nodes[0].id, target: nodes[1].id }])}>
            <Plus /> 添加关系
          </Button>
        )}
      </div>
    </div>
  );
}

function AIImageInspector({
  spec,
  resolvedPrompt,
  capabilities,
  onChange,
}: {
  spec: Record<string, unknown>;
  resolvedPrompt?: string | null;
  capabilities: ImageProviderCapabilities | null;
  onChange: (spec: Record<string, unknown>) => void;
}) {
  const semantics = (spec.semantics ?? {}) as Record<string, unknown>;
  const elements = Array.isArray(semantics.elements)
    ? semantics.elements.filter((item): item is string => typeof item === 'string')
    : [];
  const hasPromptOverride =
    Object.prototype.hasOwnProperty.call(spec, 'prompt_override') &&
    typeof spec.prompt_override === 'string';
  const promptOverride =
    typeof spec.prompt_override === 'string' ? spec.prompt_override : '';
  const refinedPrompt =
    typeof spec.refined_prompt === 'string' ? spec.refined_prompt : '';
  // 描述一改，服务端润色出来的 refined_prompt 就配不上它了。删掉它，
  // 最终提示词退回可预测的拼接式版本——绝不能让确认框展示一句与描述无关的旧提示词。
  // prompt_override 保留为用户本轮提交给 DeepSeek 的明确要求；后端会结合全文
  // 重新生成 refined_prompt，不会把这段未经分析的文字直接交给 Yunwu。
  const editStructured = (next: Record<string, unknown>) => {
    const { refined_prompt: _dropped, ...rest } = { ...spec, ...next };
    onChange(rest);
  };
  const updateElements = (nextElements: string[]) => {
    editStructured({ semantics: { ...semantics, elements: nextElements } });
  };
  const finalPrompt =
    hasPromptOverride
      ? promptOverride
      : refinedPrompt ||
        assembleImagePrompt(spec) ||
        resolvedPrompt ||
        String(spec.prompt ?? '');
  const resetFinalPrompt = () => {
    const {
      prompt_override: _override,
      refined_prompt: _refined,
      ...rest
    } = spec;
    onChange(rest);
  };
  const fieldId = React.useId();
  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor={`${fieldId}-subject`}>主题</Label>
        <Input id={`${fieldId}-subject`} value={String(semantics.subject ?? '')} onChange={(event) => editStructured({ semantics: { ...semantics, subject: event.target.value } })} />
      </div>
      <div className="space-y-2">
        <Label htmlFor={`${fieldId}-composition`}>构图</Label>
        <Textarea id={`${fieldId}-composition`} value={String(semantics.composition ?? '')} onChange={(event) => editStructured({ semantics: { ...semantics, composition: event.target.value } })} />
      </div>
      <div className="space-y-2">
        <Label>画面元素</Label>
        {elements.map((element, index) => (
          <div key={index} className="flex items-center gap-2">
            <Input
              value={element}
              onChange={(event) => {
                const next = [...elements];
                next[index] = event.target.value;
                updateElements(next);
              }}
              aria-label={`画面元素 ${index + 1}`}
            />
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-11 w-11"
              disabled={index === 0}
              onClick={() => updateElements(moveItem(elements, index, index - 1))}
              aria-label={`上移画面元素 ${index + 1}`}
            ><ArrowUp /></Button>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-11 w-11"
              disabled={index === elements.length - 1}
              onClick={() => updateElements(moveItem(elements, index, index + 1))}
              aria-label={`下移画面元素 ${index + 1}`}
            ><ArrowDown /></Button>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-11 w-11"
              onClick={() => updateElements(elements.filter((_, itemIndex) => itemIndex !== index))}
              aria-label={`删除画面元素 ${index + 1}`}
            ><Trash2 /></Button>
          </div>
        ))}
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => updateElements([...elements, '新元素'])}
        ><Plus /> 添加画面元素</Button>
      </div>
      <div className="space-y-2">
        <Label htmlFor={`${fieldId}-style`}>风格</Label>
        <Input id={`${fieldId}-style`} value={String(spec.style ?? '')} onChange={(event) => editStructured({ style: event.target.value })} />
      </div>
      {capabilities?.supported_sizes.length ? (
        <FieldSelect
          label="尺寸"
          value={String(spec.size ?? capabilities.supported_sizes[0])}
          options={capabilities.supported_sizes.map((size) => [size, size])}
          onChange={(size) => onChange({ ...spec, size })}
        />
      ) : null}
      {capabilities?.quality_modes.length ? (
        <FieldSelect
          label="质量"
          value={String(spec.quality ?? capabilities.quality_modes[0])}
          options={capabilities.quality_modes.map((quality) => [
            quality,
            quality === 'low' ? '低' : quality === 'high' ? '高' : '中',
          ])}
          onChange={(quality) => onChange({ ...spec, quality })}
        />
      ) : null}
      {capabilities?.supports_negative_prompt ? (
        <div className="space-y-2">
          <Label htmlFor={`${fieldId}-negative-prompt`}>负面提示词</Label>
          <Textarea
            id={`${fieldId}-negative-prompt`}
            value={String(spec.negative_prompt ?? '')}
            onChange={(event) =>
              onChange(withOptionalValue(spec, 'negative_prompt', event.target.value))
            }
          />
        </div>
      ) : null}
      {capabilities?.supports_seed ? (
        <div className="space-y-2">
          <Label htmlFor={`${fieldId}-seed`}>随机种子</Label>
          <Input
            id={`${fieldId}-seed`}
            type="number"
            min={0}
            max={4294967295}
            step={1}
            value={typeof spec.seed === 'number' ? spec.seed : ''}
            onChange={(event) =>
              onChange(
                withOptionalValue(
                  spec,
                  'seed',
                  event.target.value === '' ? undefined : Number(event.target.value),
                ),
              )
            }
          />
        </div>
      ) : null}
      <div className="space-y-2 rounded-md bg-muted/50 px-3 py-3">
        <div className="flex items-center justify-between gap-3">
          <Label htmlFor={`${fieldId}-final-prompt`}>给 DeepSeek 的生图要求</Label>
          {hasPromptOverride ? (
            <Button type="button" variant="ghost" size="sm" onClick={resetFinalPrompt}>
              <RotateCcw /> 恢复自动同步
            </Button>
          ) : null}
        </div>
        <Textarea
          id={`${fieldId}-final-prompt`}
          value={finalPrompt}
          onChange={(event) => onChange({ ...spec, prompt_override: event.target.value })}
          className="min-h-32 font-mono text-xs"
        />
        <p className="text-xs text-muted-foreground">
          {hasPromptOverride
            ? '已记录本轮手动要求；保存后 DeepSeek 会结合论文全文重新生成最终提示词。'
            : refinedPrompt
              ? '这里显示当前 DeepSeek 成品提示词；直接编辑可提出下一轮修改要求。'
              : '当前与主题、构图、元素和风格同步；保存时会交给 DeepSeek 结合全文分析。'}
        </p>
      </div>
      <p className="text-xs text-muted-foreground">
        {capabilities ? 'AI 生成功能已就绪' : 'AI 生成功能尚未就绪'}；生成图片前仍会再次展示并确认提示词。
      </p>
    </div>
  );
}

function FieldSelect({ label, value, options, onChange }: { label: string; value: string; options: string[][]; onChange: (value: string) => void }) {
  return (
    <label className="space-y-2 text-sm">
      <span className="font-medium">{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)} className="h-11 w-full rounded-md border bg-background px-3">
        {options.map(([option, text]) => <option key={option} value={option}>{text}</option>)}
      </select>
    </label>
  );
}

function NodeSelect({ value, nodes, onChange }: { value: string; nodes: NodeRow[]; onChange: (value: string) => void }) {
  return (
    <select value={value} onChange={(event) => onChange(event.target.value)} className="h-11 min-w-0 rounded-md border bg-background px-2 text-sm">
      {nodes.map((node) => <option key={node.id} value={node.id}>{node.label}</option>)}
    </select>
  );
}

function suggestionsFor(kind: VisualAsset['kind']): string[] {
  if (kind === 'diagram') return ['减少节点，让结构更简洁', '改成横向阅读', '突出核心方法'];
  if (kind === 'chart') return ['改成更适合比较的柱状图', '改成折线图突出趋势', '改为灰度，便于打印'];
  return ['构图更克制，减少装饰元素', '改成横向构图', '突出研究对象'];
}

function moveItem<T>(items: T[], from: number, to: number): T[] {
  if (from === to || to < 0 || to >= items.length) return items;
  const next = [...items];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

function withOptionalValue(
  spec: Record<string, unknown>,
  key: string,
  value: unknown,
): Record<string, unknown> {
  if (value !== '' && value !== undefined) return { ...spec, [key]: value };
  const next = { ...spec };
  delete next[key];
  return next;
}

/** Mirrors AIImageSpec.render_prompt for immediate editing feedback. */
function assembleImagePrompt(spec: Record<string, unknown>): string {
  const semantics = (spec.semantics ?? {}) as Record<string, unknown>;
  const subject = typeof semantics.subject === 'string' ? semantics.subject.trim() : '';
  const composition =
    typeof semantics.composition === 'string' ? semantics.composition.trim() : '';
  const elements = Array.isArray(semantics.elements)
    ? semantics.elements
        .filter((item): item is string => typeof item === 'string')
        .map((item) => item.trim())
        .filter(Boolean)
    : [];
  const parts: string[] = [];
  if (subject) {
    parts.push(subject);
    if (composition) parts.push(`composition: ${composition}`);
    if (elements.length) parts.push(`elements: ${elements.join(', ')}`);
  } else if (typeof spec.prompt === 'string' && spec.prompt.trim()) {
    parts.push(spec.prompt.trim());
  }
  const style =
    typeof spec.style === 'string' && spec.style.trim()
      ? spec.style.trim()
      : 'clean academic conceptual illustration';
  parts.push(`Style: ${style}`);
  if (semantics.text_policy === 'none') {
    parts.push('no text, no labels, no numerals');
  } else if (semantics.text_policy === 'minimal') {
    parts.push('keep any lettering to a few short, correctly spelled labels');
  }
  return `${parts.map((part) => part.replace(/\.+$/, '')).join('. ')}.`;
}
