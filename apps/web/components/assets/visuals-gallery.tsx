'use client';

import * as React from 'react';
import { Image as ImageIcon, Loader2, Plus, RefreshCw, Sparkles, X } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog } from '@/components/ui/dialog';
import { useToast } from '@/components/ui/toast';
import { useProject } from '@/components/project/project-context';
import {
  createVisual,
  generateVisual,
  regenerateVisual,
  rejectVisual,
  suggestVisuals,
  updateVisual,
} from '@/lib/api';
import { describeError } from '@/lib/errors';
import type { UserAsset, VisualAsset, VisualKind } from '@/lib/types';

const KIND_NAME: Record<VisualKind, string> = {
  chart: '数据图表',
  diagram: '学术示意图',
  ai_image: 'AI 概念插图',
};

export function VisualsGallery({
  projectId,
  assets,
  visuals,
  aiGenerationAvailable,
  onChanged,
}: {
  projectId: string;
  assets: UserAsset[];
  visuals: VisualAsset[];
  aiGenerationAvailable: boolean;
  onChanged: () => void;
}) {
  const { startJob } = useProject();
  const { toast } = useToast();
  const [creating, setCreating] = React.useState(false);
  const [open, setOpen] = React.useState(false);
  const [editing, setEditing] = React.useState<VisualAsset | null>(null);

  const startSuggestion = async () => {
    try {
      const result = await suggestVisuals(projectId);
      startJob(result.data, '后端不可用：无法分析视觉建议');
    } catch (error) {
      toast({ title: '视觉建议未能启动', description: describeError(error), variant: 'error' });
    }
  };

  const runGeneration = async (visual: VisualAsset) => {
    try {
      const result = await generateVisual(projectId, visual.id);
      startJob(result.data, '后端不可用：无法生成视觉预览');
    } catch (error) {
      toast({ title: '预览未能生成', description: describeError(error), variant: 'error' });
    }
  };

  const regenerate = async (visual: VisualAsset) => {
    try {
      const revision = await regenerateVisual(projectId, visual.id);
      if (revision.data) await runGeneration(revision.data);
      onChanged();
    } catch (error) {
      toast({ title: '重新生成失败', description: describeError(error), variant: 'error' });
    }
  };

  const reject = async (visual: VisualAsset) => {
    try {
      await rejectVisual(projectId, visual.id);
      onChanged();
    } catch (error) {
      toast({ title: '未能拒绝建议', description: describeError(error), variant: 'error' });
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border bg-muted/30 p-3">
        <p className="text-sm text-muted-foreground">
          数据图表和示意图由隔离渲染器确定性生成；AI 插图只用于概念表达。
        </p>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={startSuggestion}>
            <Sparkles className="h-3.5 w-3.5" /> 分析全文
          </Button>
          <Button size="sm" onClick={() => { setEditing(null); setOpen(true); }}>
            <Plus className="h-3.5 w-3.5" /> 新建图表或插图
          </Button>
        </div>
      </div>

      {visuals.length === 0 ? (
        <div className="rounded-xl border border-dashed py-14 text-center">
          <ImageIcon className="mx-auto h-8 w-8 text-muted-foreground" />
          <p className="mt-3 font-medium">还没有图表或插图</p>
          <p className="mt-1 text-sm text-muted-foreground">从数据素材生成图表，或让系统先分析全文。</p>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {visuals.map((visual) => {
            const preview = visual.renditions.png?.url || visual.renditions.svg?.url;
            const running = ['queued', 'running'].includes(visual.generation_status);
            return (
              <Card key={visual.id} className="overflow-hidden">
                {preview ? (
                  // eslint-disable-next-line @next/next/no-img-element -- protected API rendition URL
                  <img src={preview} alt={visual.alt_text} className="h-52 w-full bg-white object-contain" />
                ) : (
                  <div className="flex h-32 items-center justify-center bg-muted/40 text-sm text-muted-foreground">
                    {running ? <Loader2 className="h-5 w-5 animate-spin" /> : '尚未生成预览'}
                  </div>
                )}
                <CardHeader className="pb-2">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <CardTitle className="text-sm">{visual.title || visual.caption || '未命名视觉'}</CardTitle>
                      <p className="mt-1 text-xs text-muted-foreground">{KIND_NAME[visual.kind]} · v{visual.version}</p>
                    </div>
                    <div className="flex gap-1">
                      {visual.kind === 'ai_image' && <Badge variant="warning">AI</Badge>}
                      <Badge variant={visual.review_status === 'approved' ? 'success' : visual.review_status === 'rejected' ? 'muted' : 'secondary'}>
                        {visual.review_status === 'approved' ? '已插入' : visual.review_status === 'rejected' ? '已拒绝' : '待确认'}
                      </Badge>
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="space-y-2">
                  <p className="text-sm">{visual.caption || '未填写图注'}</p>
                  {visual.caption_hint && <p className="text-xs text-warning-foreground">{visual.caption_hint}</p>}
                  {visual.error_message && <p className="text-xs text-destructive-strong">{visual.error_message}</p>}
                  {visual.review_status === 'pending' && (
                    <div className="flex flex-wrap gap-1.5">
                      <Button variant="outline" size="sm" onClick={() => { setEditing(visual); setOpen(true); }} disabled={running}>
                        调整
                      </Button>
                      {visual.generation_status !== 'ready' && (
                        <Button
                          size="sm"
                          onClick={() => runGeneration(visual)}
                          disabled={running || (visual.kind === 'ai_image' && !aiGenerationAvailable)}
                          title={visual.kind === 'ai_image' && !aiGenerationAvailable ? 'AI 图像生成未启用或未配置密钥' : undefined}
                        >
                          {running && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                          {visual.kind === 'ai_image' ? '生成（可能计费）' : '生成预览'}
                        </Button>
                      )}
                      {visual.generation_status === 'ready' && (
                        <Button variant="outline" size="sm" onClick={() => regenerate(visual)}>
                          <RefreshCw className="h-3.5 w-3.5" /> 新版本
                        </Button>
                      )}
                      <Button variant="ghost" size="sm" onClick={() => reject(visual)}>
                        <X className="h-3.5 w-3.5" /> 拒绝
                      </Button>
                    </div>
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      <VisualCreateDialog
        key={editing?.id ?? 'new-visual'}
        open={open}
        onClose={() => { setOpen(false); setEditing(null); }}
        assets={assets}
        initial={editing}
        creating={creating}
        onCreate={async (payload, autoGenerate) => {
          setCreating(true);
          try {
            const created = editing
              ? editing.generation_status === 'ready'
                ? await regenerateVisual(projectId, editing.id, payload)
                : await updateVisual(projectId, editing.id, payload)
              : await createVisual(projectId, payload);
            if (!created.data) throw new Error('视觉资产未创建');
            setOpen(false);
            setEditing(null);
            onChanged();
            if (autoGenerate) await runGeneration(created.data);
            else toast({ title: 'AI 插图建议已创建，请确认后再生成', variant: 'success' });
          } catch (error) {
            toast({ title: '视觉资产未能创建', description: describeError(error), variant: 'error' });
          } finally {
            setCreating(false);
          }
        }}
      />
    </div>
  );
}

function VisualCreateDialog({
  open,
  onClose,
  assets,
  initial,
  creating,
  onCreate,
}: {
  open: boolean;
  onClose: () => void;
  assets: UserAsset[];
  initial: VisualAsset | null;
  creating: boolean;
  onCreate: (payload: Parameters<typeof createVisual>[1], autoGenerate: boolean) => void;
}) {
  const tables = assets.filter((asset) => asset.headers.length >= 2 && asset.asset_ref);
  const spec = initial?.spec ?? {};
  const initialKind = (typeof spec.kind === 'string' ? spec.kind : 'chart') as VisualKind;
  const [kind, setKind] = React.useState<VisualKind>(initialKind);
  const [assetRef, setAssetRef] = React.useState(
    typeof spec.source_asset_ref === 'string' ? spec.source_asset_ref : tables[0]?.asset_ref || '',
  );
  const selected = tables.find((asset) => asset.asset_ref === assetRef) || tables[0];
  const [chartType, setChartType] = React.useState(typeof spec.chart_type === 'string' ? spec.chart_type : 'bar');
  const [x, setX] = React.useState(typeof spec.x === 'string' ? spec.x : selected?.headers[0] || '');
  const [yColumns, setYColumns] = React.useState<string[]>(
    Array.isArray(spec.y) ? spec.y.filter((item): item is string => typeof item === 'string') : selected?.headers[1] ? [selected.headers[1]] : [],
  );
  const [unit, setUnit] = React.useState(typeof spec.unit === 'string' ? spec.unit : '');
  const [palette, setPalette] = React.useState(typeof spec.palette === 'string' ? spec.palette : 'colorblind');
  const [width, setWidth] = React.useState(typeof spec.width === 'string' ? spec.width : initialKind === 'chart' ? 'column' : 'full');
  const [aggregation, setAggregation] = React.useState(typeof spec.aggregation === 'string' ? spec.aggregation : 'none');
  const [sort, setSort] = React.useState(typeof spec.sort === 'string' ? spec.sort : 'none');
  const [caption, setCaption] = React.useState(initial?.caption ?? '');
  const [altText, setAltText] = React.useState(initial?.alt_text ?? '');
  const initialNodes = Array.isArray(spec.nodes) ? spec.nodes as Array<Record<string, unknown>> : [];
  const [nodes, setNodes] = React.useState(
    initialNodes.length > 0
      ? initialNodes.map((node) => `${typeof node.group === 'string' ? `[${node.group}] ` : ''}${String(node.label ?? '')}`).join('\n')
      : '输入\n处理\n输出',
  );
  const initialEdges = Array.isArray(spec.edges) ? spec.edges as Array<Record<string, unknown>> : [];
  const [edges, setEdges] = React.useState(
    initialEdges.map((edge) => `${String(edge.source ?? '')} -> ${String(edge.target ?? '')}${edge.label ? ` | ${String(edge.label)}` : ''}`).join('\n'),
  );
  const initialGroups = Array.isArray(spec.groups) ? spec.groups as Array<Record<string, unknown>> : [];
  const [groups, setGroups] = React.useState(
    initialGroups.map((group) => `${String(group.id ?? '')} | ${String(group.label ?? '')}`).join('\n'),
  );
  const [direction, setDirection] = React.useState(typeof spec.direction === 'string' ? spec.direction : 'LR');
  const [prompt, setPrompt] = React.useState(typeof spec.prompt === 'string' ? spec.prompt : '');
  const [imageSize, setImageSize] = React.useState(typeof spec.size === 'string' ? spec.size : '1536x1024');
  const [imageQuality, setImageQuality] = React.useState(typeof spec.quality === 'string' ? spec.quality : 'medium');

  const previousAssetRef = React.useRef(assetRef);
  React.useEffect(() => {
    if (!selected) return;
    if (previousAssetRef.current === assetRef) return;
    previousAssetRef.current = assetRef;
    setX(selected.headers[0] || '');
    setYColumns(selected.headers[1] ? [selected.headers[1]] : selected.headers[0] ? [selected.headers[0]] : []);
  }, [assetRef, selected]);

  const submit = () => {
    if (!caption.trim() || !altText.trim()) return;
    if (kind === 'chart') {
      onCreate(
        {
          spec: {
            kind: 'chart',
            chart_type: chartType,
            source_asset_ref: selected?.asset_ref,
            x,
            y: yColumns,
            x_label: x,
            y_label: yColumns.join(', '),
            unit,
            filters: [],
            aggregation,
            sort,
            width,
            palette,
          },
          title: caption,
          caption,
          alt_text: altText,
        },
        true,
      );
      return;
    }
    if (kind === 'diagram') {
      const parsedGroups = groups
        .split('\n')
        .map((item) => item.trim())
        .filter(Boolean)
        .slice(0, 12)
        .map((item, index) => {
          const [id, ...label] = item.split('|').map((part) => part.trim());
          return { id: id || `g${index + 1}`, label: label.join(' | ') || id };
        });
      const parsedNodes = nodes
        .split('\n')
        .map((item) => item.trim())
        .filter(Boolean)
        .slice(0, 30)
        .map((item, index) => {
          const groupMatch = item.match(/^\[([A-Za-z][A-Za-z0-9_-]*)\]\s*(.+)$/);
          return {
            id: `n${index + 1}`,
            label: groupMatch?.[2] || item,
            ...(groupMatch?.[1] ? { group: groupMatch[1] } : {}),
          };
        });
      const parsedEdges = edges.trim()
        ? edges
            .split('\n')
            .map((item) => item.trim())
            .filter(Boolean)
            .slice(0, 60)
            .map((item) => {
              const [path, label = ''] = item.split('|').map((part) => part.trim());
              const [source = '', target = ''] = path.split('->').map((part) => part.trim());
              return { source, target, label };
            })
        : parsedNodes.slice(1).map((_, index) => ({ source: `n${index + 1}`, target: `n${index + 2}` }));
      onCreate(
        {
          spec: {
            kind: 'diagram',
            direction,
            nodes: parsedNodes,
            edges: parsedEdges,
            groups: parsedGroups,
            width,
          },
          title: caption,
          caption,
          alt_text: altText,
        },
        true,
      );
      return;
    }
    onCreate(
      {
        spec: {
          kind: 'ai_image',
          prompt,
          size: imageSize,
          quality: imageQuality,
          style: 'clean academic conceptual illustration',
          width,
        },
        title: caption,
        caption,
        alt_text: altText,
      },
      false,
    );
  };

  const valid = Boolean(
    caption.trim()
      && altText.trim()
      && (kind !== 'chart' || (selected && yColumns.length > 0 && (chartType !== 'heatmap' || yColumns.length >= 2)))
      && (kind !== 'ai_image' || prompt.trim().length >= 10),
  );
  const fieldClass = 'h-9 w-full rounded-md border bg-background px-3 text-sm';
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={initial ? '调整图表或插图' : '新建图表或插图'}
      description="使用表单描述视觉内容，系统不会执行脚本或读取远程链接。"
      footer={
        <>
          <Button variant="outline" onClick={onClose}>取消</Button>
          <Button onClick={submit} disabled={!valid || creating}>
            {creating && <Loader2 className="h-4 w-4 animate-spin" />}
            {kind === 'ai_image' ? (initial ? '保存新版本' : '创建建议') : initial ? '保存并生成新预览' : '创建并生成预览'}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <label className="block space-y-1 text-sm">
          <span>类型</span>
          <select className={fieldClass} value={kind} onChange={(event) => setKind(event.target.value as VisualKind)} disabled={Boolean(initial)}>
            <option value="chart">数据图表</option>
            <option value="diagram">学术示意图</option>
            <option value="ai_image">AI 概念插图</option>
          </select>
        </label>
        {kind === 'chart' && (
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1 text-sm sm:col-span-2"><span>数据素材</span><select className={fieldClass} value={assetRef} onChange={(event) => setAssetRef(event.target.value)}>{tables.map((asset) => <option key={asset.id} value={asset.asset_ref || ''}>{asset.title}</option>)}</select></label>
            <label className="space-y-1 text-sm"><span>图形</span><select className={fieldClass} value={chartType} onChange={(event) => setChartType(event.target.value)}><option value="bar">柱状图</option><option value="line">折线图</option><option value="scatter">散点图</option><option value="box">箱线图</option><option value="heatmap">热力图</option></select></label>
            <label className="space-y-1 text-sm"><span>横轴</span><select className={fieldClass} value={x} onChange={(event) => setX(event.target.value)}>{selected?.headers.map((header) => <option key={header}>{header}</option>)}</select></label>
            <label className="space-y-1 text-sm"><span>数值字段（可多选）</span><select multiple className="min-h-24 w-full rounded-md border bg-background px-3 py-2 text-sm" value={yColumns} onChange={(event) => setYColumns(Array.from(event.target.selectedOptions, (option) => option.value))}>{selected?.headers.map((header) => <option key={header}>{header}</option>)}</select></label>
            <label className="space-y-1 text-sm"><span>单位</span><input className={fieldClass} value={unit} onChange={(event) => setUnit(event.target.value)} placeholder="%、ms、个" /></label>
            <label className="space-y-1 text-sm"><span>配色</span><select className={fieldClass} value={palette} onChange={(event) => setPalette(event.target.value)}><option value="colorblind">色盲友好</option><option value="grayscale">灰度</option></select></label>
            <label className="space-y-1 text-sm"><span>聚合</span><select className={fieldClass} value={aggregation} onChange={(event) => setAggregation(event.target.value)}><option value="none">不聚合</option><option value="mean">平均值</option><option value="median">中位数</option><option value="sum">求和</option><option value="count">计数</option></select></label>
            <label className="space-y-1 text-sm"><span>横轴排序</span><select className={fieldClass} value={sort} onChange={(event) => setSort(event.target.value)}><option value="none">原顺序</option><option value="asc">升序</option><option value="desc">降序</option></select></label>
          </div>
        )}
        {kind === 'diagram' && (
          <div className="space-y-3">
            <label className="block space-y-1 text-sm"><span>节点（每行一个；可用 [g1] 前缀放入分组）</span><textarea className="min-h-32 w-full rounded-md border bg-background p-3" value={nodes} onChange={(event) => setNodes(event.target.value)} /></label>
            <label className="block space-y-1 text-sm"><span>连线（可选，例如 n1 -&gt; n2 | 数据流；留空则按顺序连接）</span><textarea className="min-h-20 w-full rounded-md border bg-background p-3" value={edges} onChange={(event) => setEdges(event.target.value)} /></label>
            <label className="block space-y-1 text-sm"><span>分组（可选，每行 g1 | 分组名）</span><textarea className="min-h-20 w-full rounded-md border bg-background p-3" value={groups} onChange={(event) => setGroups(event.target.value)} /></label>
            <label className="block space-y-1 text-sm"><span>方向</span><select className={fieldClass} value={direction} onChange={(event) => setDirection(event.target.value)}><option value="LR">从左到右</option><option value="TB">从上到下</option></select></label>
          </div>
        )}
        {kind === 'ai_image' && (
          <div className="space-y-2">
            <label className="block space-y-1 text-sm"><span>概念描述</span><textarea className="min-h-32 w-full rounded-md border bg-background p-3" value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="例如：抽象地表现人机协作的科研工作流，不含文字和数据坐标轴" /></label>
            <div className="grid gap-3 sm:grid-cols-2"><label className="space-y-1 text-sm"><span>比例</span><select className={fieldClass} value={imageSize} onChange={(event) => setImageSize(event.target.value)}><option value="1536x1024">横向 3:2</option><option value="1024x1024">方形 1:1</option><option value="1024x1536">竖向 2:3</option></select></label><label className="space-y-1 text-sm"><span>质量</span><select className={fieldClass} value={imageQuality} onChange={(event) => setImageQuality(event.target.value)}><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label></div>
            <p className="rounded-md border border-warning/40 bg-warning/10 p-2 text-xs text-warning-foreground">只会创建建议，不会立即计费。实验曲线、准确率图和精确技术结构必须使用确定性图表或示意图。</p>
          </div>
        )}
        <label className="block space-y-1 text-sm"><span>论文宽度</span><select className={fieldClass} value={width} onChange={(event) => setWidth(event.target.value)}><option value="column">单栏</option><option value="full">通栏</option></select></label>
        <label className="block space-y-1 text-sm"><span>图注</span><input className={fieldClass} value={caption} onChange={(event) => setCaption(event.target.value)} /></label>
        <label className="block space-y-1 text-sm"><span>无障碍描述</span><textarea className="min-h-20 w-full rounded-md border bg-background p-3" value={altText} onChange={(event) => setAltText(event.target.value)} /></label>
      </div>
    </Dialog>
  );
}
