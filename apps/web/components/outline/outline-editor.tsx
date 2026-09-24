'use client';

import * as React from 'react';
import Link from 'next/link';
import {
  ChevronDown,
  ChevronUp,
  GripVertical,
  Loader2,
  PenLine,
  Plus,
  Trash2,
  Wand2,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Callout } from '@/components/ui/callout';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Dialog } from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { useToast } from '@/components/ui/toast';
import { LoadState } from '@/components/layout/load-state';
import { WorkbenchHeader } from '@/components/project/workbench-header';
import { WorkbenchFooterNav } from '@/components/project/workbench-footer-nav';
import { useJobFinished, useProject } from '@/components/project/project-context';
import { CiteKeyPicker } from '@/components/writing/cite-key-picker';
import { rebuildDependencies, generateOutline, generateSections, getOutline, updateOutline } from '@/lib/api';
import type { Job, OutlineSection, OutlineTree } from '@/lib/types';
import { describeError } from '@/lib/errors';
import { projectHref } from '@/lib/pipeline';
import { cn } from '@/lib/utils';

/** 输入停顿多久后才落盘。此前每敲一个键就发一次 PUT。 */
const SAVE_DEBOUNCE_MS = 400;

/**
 * 摘要/引言/结论在大纲阶段只占位（设计 §4.4.1「后写」），内容由 write 阶段在正文
 * 完成后依据滚动摘要生成。这里逐节说明，否则空白卡片看着像生成失败。
 */
const FRAME_HINT: Record<string, string> = {
  abstract: '正文写完后自动生成——届时系统才能概括全文。',
  introduction: '正文写完后自动生成——引言要交代全篇脉络。',
  conclusion: '正文写完后自动生成——结论要回收各章论证。',
};

/**
 * 大纲还没生成（或 outline 行已建、tree_json 仍为空）时，GET 会返回 `tree: {}`，
 * 没有 sections 字段——`?? {sections: []}` 拦不住它。统一在这里补齐。
 */
function normalizeTree(tree: Partial<OutlineTree> | null | undefined): OutlineTree {
  return { ...tree, sections: Array.isArray(tree?.sections) ? tree.sections : [] };
}

/**
 * 大纲编辑器：左树 + 右详情。
 *
 * 此前是一列等权重的大卡片——7 章约 3000px，摘要/引言/结论三个天生空壳的框架章节
 * 各占一整张卡，实测吃掉首屏一半，而真正要看的结构反倒一眼看不全。
 */
export function OutlineEditor() {
  const { projectId, whitelist, busy, startJob, reload: reloadProject } = useProject();
  const { toast } = useToast();

  const [tree, setTree] = React.useState<OutlineTree>({ sections: [] });
  const [version, setVersion] = React.useState(0);
  const [outlineId, setOutlineId] = React.useState<string | null>(null);
  const [contentHash, setContentHash] = React.useState<string | null>(null);
  const [confirmed, setConfirmed] = React.useState(false);
  const [polishPolicy, setPolishPolicy] = React.useState<import('@/lib/types').PolishPolicy>('legacy');
  const [activeIndex, setActiveIndex] = React.useState(0);
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [pendingDelete, setPendingDelete] = React.useState<number | null>(null);

  const reload = React.useCallback(async () => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    const outline = await getOutline(projectId);
    if (outline.data) {
      setTree(normalizeTree(outline.data.tree));
      setVersion(outline.data.version);
      setOutlineId(outline.data.outline_id || null);
      setContentHash(outline.data.content_hash || null);
      setConfirmed(outline.data.status === 'confirmed');
    }
    setLoadError(null);
    setLoading(false);
  }, [projectId]);

  const runReload = React.useCallback(() => {
    setLoadError(null);
    reload().catch((err) => {
      setLoadError(describeError(err));
      setLoading(false);
    });
  }, [reload]);

  React.useEffect(() => {
    runReload();
  }, [runReload]);
  useJobFinished(runReload);

  // 防抖落盘 + 请求序号：慢响应不能覆盖用户在此期间敲进去的字符。
  const timerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = React.useRef<{ tree: OutlineTree; status: 'draft' | 'confirmed' } | null>(null);
  const seqRef = React.useRef(0);

  const flush = React.useCallback(async () => {
    const pending = pendingRef.current;
    pendingRef.current = null;
    if (!pending || !projectId) return;
    const seq = ++seqRef.current;
    setSaving(true);
    try {
      const result = await updateOutline(projectId, pending.tree, pending.status);
      // 服务端会把 cite_keys 收敛到白名单内（R2 前置），以返回值为准——
      // 但只在这之后没有更新的编辑时才回填，否则会吞掉用户刚敲的字。
      if (result.data && seq === seqRef.current && !pendingRef.current) {
        setTree(normalizeTree(result.data.tree));
        setVersion(result.data.version);
        setOutlineId(result.data.outline_id || null);
        setContentHash(result.data.content_hash || null);
        setConfirmed(result.data.status === 'confirmed');
      }
    } catch (err) {
      toast({ title: '大纲未能保存', description: describeError(err), variant: 'error' });
    } finally {
      // 必须在 finally：写在 await 之后时，一次 500 会让「保存中…」永久悬停。
      if (seq === seqRef.current) setSaving(false);
    }
  }, [projectId, toast]);

  const save = React.useCallback(
    (next: OutlineTree, status: 'draft' | 'confirmed' = 'draft', immediate = false) => {
      setTree(next);
      setContentHash(null);
      setConfirmed(false);
      if (!projectId) return;
      pendingRef.current = { tree: next, status };
      if (timerRef.current) clearTimeout(timerRef.current);
      if (immediate) {
        void flush();
      } else {
        timerRef.current = setTimeout(() => void flush(), SAVE_DEBOUNCE_MS);
      }
    },
    [projectId, flush],
  );

  // 卸载前把未落盘的编辑冲出去，避免切页丢失最后几个字。
  React.useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      if (pendingRef.current) void flush();
    },
    [flush],
  );

  const updateSection = (index: number, patch: Partial<OutlineSection>) => {
    const sections = tree.sections.map((s, i) => (i === index ? { ...s, ...patch } : s));
    save({ ...tree, sections });
  };

  const moveSection = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= tree.sections.length) return;
    const sections = [...tree.sections];
    [sections[index], sections[target]] = [sections[target], sections[index]];
    save({ ...tree, sections }, 'draft', true);
    setActiveIndex(target);
  };

  /** 拖拽落位：把 from 抽出来插到 to 之前（而不是与 to 对调）。 */
  const reorderSection = (from: number, to: number) => {
    if (from === to || from < 0 || to < 0) return;
    const sections = [...tree.sections];
    const [moved] = sections.splice(from, 1);
    // 从上往下拖时，抽走一项会让目标下标左移一位。
    sections.splice(from < to ? to - 1 : to, 0, moved);
    save({ ...tree, sections }, 'draft', true);
    setActiveIndex(from < to ? to - 1 : to);
  };

  const addSection = () => {
    const bodyCount = tree.sections.filter((s) => s.kind !== 'frame').length;
    const insertAt = tree.sections.findIndex((s) => s.key === 'conclusion');
    const section: OutlineSection = {
      key: `s${bodyCount + 1}`,
      level: 1,
      title: '新章节',
      summary: '',
      argument_points: [],
      cite_keys: [],
      kind: 'body',
    };
    const sections = [...tree.sections];
    const at = insertAt >= 0 ? insertAt : sections.length;
    sections.splice(at, 0, section);
    save({ ...tree, sections }, 'draft', true);
    setActiveIndex(at);
  };

  const confirmRemove = () => {
    if (pendingDelete === null) return;
    save({ ...tree, sections: tree.sections.filter((_, i) => i !== pendingDelete) }, 'draft', true);
    setActiveIndex((i) => Math.max(0, Math.min(i, tree.sections.length - 2)));
    setPendingDelete(null);
  };

  const runAction = async (
    action: () => Promise<{ data: Job | undefined }>,
    fallback: string,
    failTitle: string,
  ) => {
    try {
      const started = await action();
      startJob(started.data, fallback);
      reloadProject();
    } catch (err) {
      toast({ title: failTitle, description: describeError(err), variant: 'error' });
    }
  };

  const assignedKeys = new Set(tree.sections.flatMap((s) => s.cite_keys ?? []));
  const unassigned = whitelist.filter((key) => !assignedKeys.has(key));
  const deleteTarget = pendingDelete === null ? undefined : tree.sections[pendingDelete];
  const active = tree.sections[activeIndex];

  return (
    <div className="space-y-4">
      <WorkbenchHeader
        title="大纲编辑器"
        description={tree.topic ? `主题：${tree.topic}` : '章节结构、文献分配与论证要点'}
        actions={
          <>
            <span className="text-meta text-muted-foreground">
              v{version}
              {saving ? ' · 保存中…' : ''}
            </span>
            <Button
              variant="outline"
              onClick={() =>
                runAction(
                  () => generateOutline(projectId),
                  '后端不可用：无法生成大纲',
                  '大纲生成未能启动',
                )
              }
              disabled={!projectId || busy}
            >
              <Wand2 className="h-4 w-4" /> 生成大纲
            </Button>
            <Button variant="outline" disabled={busy || saving || !outlineId || !contentHash}
              onClick={() => runAction(() => rebuildDependencies(projectId, outlineId!, contentHash!),
                '暂时无法优化章节关系', '章节关系优化未能启动')}>优化章节依赖</Button>
            {tree.dependency_contract?.requires_confirmation !== false && !!tree.dependency_contract && !confirmed && <Button variant="outline"
              disabled={busy || saving || !contentHash} onClick={() => save(tree, 'confirmed', true)}>
              确认章节关系</Button>}
            <select aria-label="润色策略" className="max-w-full rounded border bg-background p-2 text-sm"
              value={polishPolicy} onChange={event => setPolishPolicy(event.target.value as typeof polishPolicy)}>
              <option value="legacy">逐节全部润色</option>
              <option value="full_parallel">并行全部润色（实验）</option>
              <option value="selective_parallel">按需并行润色（实验）</option>
            </select>
            <Button
              onClick={() =>
                runAction(
                  () => generateSections(projectId, true, polishPolicy),
                  '后端不可用：无法开始写作',
                  '写作未能启动',
                )
              }
              disabled={!projectId || busy || saving || !contentHash || tree.sections.length === 0 || (tree.dependency_contract?.requires_confirmation !== false && !!tree.dependency_contract && !confirmed)}
            >
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <PenLine className="h-4 w-4" />}
              开始写作
            </Button>
          </>
        }
      />

      {tree.dependency_contract && <section className="rounded border p-3 text-sm" aria-label="章节执行关系">
        <p>以下关系用于新写作任务，已有稿件保留。内容修改后会重新检查依赖。</p>
        <ul className="mt-2 space-y-1">{tree.sections.filter(s => s.kind !== 'frame').map(s =>
          <li key={s.key}><strong>{s.title}</strong>：{s.depends_on?.length ? `等待 ${s.depends_on.join('、')}` : '可独立执行'}
            {s.dependency_reason && <span className="ml-2 text-muted-foreground">{s.dependency_reason}</span>}</li>)}</ul>
      </section>}
      <LoadState loading={loading} error={loadError} onRetry={runReload} skeletonClassName="h-80">
        {tree.sections.length === 0 ? (
          <div className="rounded-lg border border-dashed py-16 text-center text-body text-muted-foreground">
            还没有大纲。点「生成大纲」，系统会按文献卡片聚类出章节树。
          </div>
        ) : (
          <div className="grid gap-6 lg:grid-cols-[18rem,minmax(0,1fr)]">
            <div className="space-y-2 lg:sticky lg:top-4 lg:max-h-[calc(100vh-2rem)] lg:self-start lg:overflow-y-auto scrollbar-thin">
              <OutlineTreeNav
                sections={tree.sections}
                activeIndex={activeIndex}
                onSelect={setActiveIndex}
                onMove={moveSection}
                onReorder={reorderSection}
              />
              <Button variant="outline" size="sm" onClick={addSection} className="w-full">
                <Plus className="h-4 w-4" /> 新增章节
              </Button>

              <section>
                <div className="space-y-1 py-3 text-meta text-muted-foreground">
                  <p>
                    已分配文献 {assignedKeys.size} / {whitelist.length}
                  </p>
                  {unassigned.length > 0 && (
                    <p className="text-warning-strong">
                      有 {unassigned.length} 篇入库文献未分配到任何章节
                    </p>
                  )}
                  <p className="border-t pt-1.5">
                    章节只能分配写作白名单内的文献；手工编辑同样会被服务端收敛到白名单内。
                  </p>
                </div>
              </section>
            </div>

            {active && (
              <SectionDetail
                key={`${active.key}-${activeIndex}`}
                section={active}
                whitelist={whitelist}
                projectId={projectId}
                onChange={(patch) => updateSection(activeIndex, patch)}
                onRemove={() => setPendingDelete(activeIndex)}
              />
            )}
          </div>
        )}
      </LoadState>

      <Dialog
        open={pendingDelete !== null}
        onClose={() => setPendingDelete(null)}
        title="删除章节？"
        description={
          deleteTarget
            ? `「${deleteTarget.title}」及其论证要点与已分配文献将一并移除，无法撤销。`
            : undefined
        }
        footer={
          <>
            <Button variant="outline" onClick={() => setPendingDelete(null)}>
              取消
            </Button>
            <Button variant="destructive" onClick={confirmRemove}>
              删除
            </Button>
          </>
        }
      />

      <WorkbenchFooterNav current="outline" />
    </div>
  );
}

function OutlineTreeNav({
  sections,
  activeIndex,
  onSelect,
  onMove,
  onReorder,
}: {
  sections: OutlineSection[];
  activeIndex: number;
  onSelect: (index: number) => void;
  onMove: (index: number, delta: number) => void;
  onReorder: (from: number, to: number) => void;
}) {
  // 拖拽源与落位同时存在 ref 与 state 里：ref 供事件处理器立即读取
  // （dragstart 之后的 dragover 可能在同一帧到达，此时 state 还没 flush，
  // 用 state 会读到 null 而永远不落位），state 只驱动视觉指示。
  const draggingRef = React.useRef<number | null>(null);
  const dropAtRef = React.useRef<number | null>(null);
  const [dragging, setDragging] = React.useState<number | null>(null);
  const [dropAt, setDropAt] = React.useState<number | null>(null);

  const setDrag = (value: number | null) => {
    draggingRef.current = value;
    setDragging(value);
  };
  const setDrop = (value: number | null) => {
    dropAtRef.current = value;
    setDropAt(value);
  };

  return (
    <nav aria-label="章节结构" className="overflow-hidden rounded-lg border">
      <p id="outline-dnd-hint" className="sr-only">
        可拖拽调整章节顺序；也可以用每行的上移、下移按钮。
      </p>
      {sections.map((section, index) => {
        const isFrame = section.kind === 'frame';
        const active = index === activeIndex;
        const level = section.level ?? 1;
        return (
          <div
            key={`${section.key}-${index}`}
            draggable
            onDragStart={(e) => {
              setDrag(index);
              e.dataTransfer.effectAllowed = 'move';
              // Firefox 要求必须 setData 才会真正开始拖拽。
              e.dataTransfer.setData('text/plain', String(index));
            }}
            onDragEnd={() => {
              setDrag(null);
              setDrop(null);
            }}
            onDragOver={(e) => {
              if (draggingRef.current === null) return;
              e.preventDefault();
              e.dataTransfer.dropEffect = 'move';
              // 落在行的上半 → 插到它前面，下半 → 插到它后面。
              const box = e.currentTarget.getBoundingClientRect();
              const after = e.clientY - box.top > box.height / 2;
              setDrop(after ? index + 1 : index);
            }}
            onDrop={(e) => {
              e.preventDefault();
              const from = draggingRef.current;
              const to = dropAtRef.current;
              if (from !== null && to !== null) onReorder(from, to);
              setDrag(null);
              setDrop(null);
            }}
            className={cn(
              'relative flex items-center gap-1 border-b last:border-b-0 transition-colors',
              active ? 'bg-secondary' : 'hover:bg-muted/50',
              dragging === index && 'opacity-40',
              // 落位指示线
              dropAt === index && 'before:absolute before:inset-x-0 before:top-0 before:h-0.5 before:bg-primary',
              dropAt === index + 1 &&
                'after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 after:bg-primary',
            )}
          >
            <GripVertical
              aria-hidden
              className="ml-1 h-3.5 w-3.5 shrink-0 cursor-grab text-muted-foreground/60"
            />
            <button
              type="button"
              onClick={() => onSelect(index)}
              aria-current={active ? 'true' : undefined}
              aria-describedby="outline-dnd-hint"
              className="min-w-0 flex-1 py-2 pr-2.5 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
              style={{ paddingLeft: `${(level - 1) * 0.75}rem` }}
            >
              <span className="flex items-center gap-1.5">
                <span
                  className={cn('line-clamp-1 text-body', active && 'font-medium', isFrame && 'text-muted-foreground')}
                >
                  {section.title}
                </span>
              </span>
              <span className="mt-0.5 flex items-center gap-1">
                {isFrame ? (
                  <Badge variant="muted" className="text-meta">
                    框架
                  </Badge>
                ) : (
                  <>
                    <Badge variant="outline" className="font-mono text-meta">
                      {section.key}
                    </Badge>
                    <span className="text-meta text-muted-foreground">
                      {section.cite_keys?.length ?? 0} 篇
                    </span>
                  </>
                )}
                {section.grounding === 'user_asset' && (
                  <Badge variant="secondary" className="text-meta">
                    素材支撑
                  </Badge>
                )}
              </span>
            </button>
            <div className="flex shrink-0 flex-col pr-1 text-muted-foreground">
              <button
                type="button"
                aria-label={`上移 ${section.title}`}
                onClick={() => onMove(index, -1)}
                disabled={index === 0}
                className="rounded transition-colors hover:text-foreground disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <ChevronUp className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                aria-label={`下移 ${section.title}`}
                onClick={() => onMove(index, 1)}
                disabled={index === sections.length - 1}
                className="rounded transition-colors hover:text-foreground disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <ChevronDown className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        );
      })}
    </nav>
  );
}

function SectionDetail({
  section,
  whitelist,
  projectId,
  onChange,
  onRemove,
}: {
  section: OutlineSection;
  whitelist: string[];
  projectId: string;
  onChange: (patch: Partial<OutlineSection>) => void;
  onRemove: () => void;
}) {
  const isFrame = section.kind === 'frame';
  const [points, setPoints] = React.useState((section.argument_points ?? []).join('\n'));

  React.useEffect(() => {
    setPoints((section.argument_points ?? []).join('\n'));
  }, [section.argument_points]);

  return (
    <div className="min-w-0 space-y-4">
      <section>
        <header className="flex-row items-start justify-between gap-3 space-y-0 pb-3">
          <div className="min-w-0 flex-1 space-y-1.5">
            <Label htmlFor="section-title">章节标题</Label>
            <Input
              id="section-title"
              value={section.title}
              onChange={(e) => onChange({ title: e.target.value })}
              className="font-medium"
            />
          </div>
          {!isFrame && (
            <button
              type="button"
              aria-label="删除章节"
              onClick={onRemove}
              className="mt-6 rounded p-1 text-muted-foreground transition-colors hover:text-destructive-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Trash2 className="h-4 w-4" />
            </button>
          )}
        </header>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="section-summary">本章要论证什么</Label>
            <Textarea
              id="section-summary"
              value={section.summary ?? ''}
              onChange={(e) => onChange({ summary: e.target.value })}
              placeholder={
                isFrame
                  ? '可选：写下这一节希望侧重什么；留空则由系统按正文自动拟定'
                  : '本章的论点与覆盖范围'
              }
              className="min-h-24"
            />
          </div>
          {isFrame && (
            <p className="rounded-md bg-muted/40 px-3 py-2 text-meta leading-relaxed text-muted-foreground">
              {FRAME_HINT[section.key] ?? '内容在正文写完后自动生成。'}
            </p>
          )}
        </div>
      </section>

      {!isFrame && (
        <>
          <section>
            <header className="pb-2">
              <h3 className="text-body">论证要点（每行一条）</h3>
            </header>
            <div>
              <Textarea
                value={points}
                onChange={(e) => setPoints(e.target.value)}
                onBlur={() =>
                  onChange({
                    argument_points: points
                      .split('\n')
                      .map((p) => p.trim())
                      .filter(Boolean),
                  })
                }
                className="min-h-32"
                aria-label="论证要点"
              />
            </div>
          </section>

          <section>
            <header className="pb-2">
              <h3 className="text-body">
                分配文献（{section.cite_keys?.length ?? 0}）
              </h3>
            </header>
            <div className="space-y-2">
              {section.grounding === 'user_asset' && (
                <Callout variant="warning">
                  这一章由<span className="font-medium">用户素材</span>接地，通常不需要分配文献。
                  正文里的数字会从
                  <Link href={projectHref(projectId, 'assets')} className="mx-1 underline">
                    素材中心
                  </Link>
                  确定性注入。
                </Callout>
              )}
              <CiteKeyPicker
                whitelist={whitelist}
                selected={section.cite_keys ?? []}
                onChange={(keys) => onChange({ cite_keys: keys })}
              />
            </div>
          </section>
        </>
      )}
    </div>
  );
}
