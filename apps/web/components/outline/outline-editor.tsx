'use client';

import * as React from 'react';
import { useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { GripVertical, Loader2, PenLine, Plus, Sparkles, Trash2, Wand2 } from 'lucide-react';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { DataSourceBanner } from '@/components/data-source-banner';
import { CiteKeyPicker } from '@/components/writing/cite-key-picker';
import {
  generateOutline,
  generateSections,
  getOutline,
  getWhitelist,
  subscribeJobEvents,
  updateOutline,
} from '@/lib/api';
import type { DataSource, Job, OutlineSection, OutlineTree } from '@/lib/types';

export function OutlineEditor() {
  const params = useSearchParams();
  const projectId = params.get('project') ?? '';

  const [tree, setTree] = React.useState<OutlineTree>({ sections: [] });
  const [version, setVersion] = React.useState(0);
  const [whitelist, setWhitelist] = React.useState<string[]>([]);
  const [source, setSource] = React.useState<DataSource>('live');
  const [note, setNote] = React.useState<string | undefined>();
  const [loading, setLoading] = React.useState(true);
  const [saving, setSaving] = React.useState(false);
  const [job, setJob] = React.useState<Job | null>(null);
  const [jobStage, setJobStage] = React.useState<string | null>(null);
  const [message, setMessage] = React.useState<string | null>(null);

  const reload = React.useCallback(async () => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    const [outline, wl] = await Promise.all([getOutline(projectId), getWhitelist(projectId)]);
    if (outline.data) {
      setTree(outline.data.tree ?? { sections: [] });
      setVersion(outline.data.version);
    }
    setWhitelist(wl.data);
    setSource(outline.source);
    setNote(outline.note);
    setLoading(false);
  }, [projectId]);

  React.useEffect(() => {
    void reload();
  }, [reload]);

  const track = (started: Job | undefined, fallback: string) => {
    if (!started) {
      setMessage(fallback);
      return;
    }
    setJob(started);
    setJobStage(started.stage ?? '排队中');
    setMessage(null);
    subscribeJobEvents(projectId, started.id, {
      onEvent: (event) => {
        setJobStage(event.stage ?? event.type);
        setJob((prev) =>
          prev ? { ...prev, progress: event.progress ?? prev.progress } : prev,
        );
      },
      onClose: () => {
        setJob(null);
        setJobStage(null);
        void reload();
      },
    });
  };

  const save = async (next: OutlineTree, status: 'draft' | 'confirmed' = 'draft') => {
    setTree(next);
    if (!projectId) return;
    setSaving(true);
    const result = await updateOutline(projectId, next, status);
    // 服务端会把 cite_keys 收敛到白名单内（R2 前置），以返回值为准。
    if (result.data) setTree(result.data.tree);
    setSaving(false);
  };

  const updateSection = (index: number, patch: Partial<OutlineSection>) => {
    const sections = tree.sections.map((s, i) => (i === index ? { ...s, ...patch } : s));
    void save({ ...tree, sections });
  };

  const moveSection = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= tree.sections.length) return;
    const sections = [...tree.sections];
    [sections[index], sections[target]] = [sections[target], sections[index]];
    void save({ ...tree, sections });
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
    sections.splice(insertAt >= 0 ? insertAt : sections.length, 0, section);
    void save({ ...tree, sections });
  };

  const removeSection = (index: number) => {
    void save({ ...tree, sections: tree.sections.filter((_, i) => i !== index) });
  };

  const assignedKeys = new Set(tree.sections.flatMap((s) => s.cite_keys ?? []));
  const unassigned = whitelist.filter((key) => !assignedKeys.has(key));

  return (
    <div className="space-y-6">
      <PageHeader
        title="大纲编辑器"
        description={tree.topic ? `主题：${tree.topic}` : '章节树、文献分配与论证要点'}
        actions={
          <>
            <Button
              variant="outline"
              onClick={async () => {
                const started = await generateOutline(projectId);
                track(started.data, '后端不可用：无法生成大纲');
              }}
              disabled={!projectId || !!job}
            >
              <Wand2 className="h-4 w-4" /> 生成大纲
            </Button>
            <Button
              onClick={async () => {
                const started = await generateSections(projectId, true);
                track(started.data, '后端不可用：无法开始写作');
              }}
              disabled={!projectId || !!job || tree.sections.length === 0}
            >
              {job ? <Loader2 className="h-4 w-4 animate-spin" /> : <PenLine className="h-4 w-4" />}
              开始写作
            </Button>
          </>
        }
      />

      <DataSourceBanner source={source} note={note} />

      {job && (
        <Card>
          <CardContent className="flex items-center gap-3 py-3">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
            <div className="flex-1">
              <div className="flex items-center justify-between text-xs">
                <span className="font-medium">{jobStage ?? '进行中'}</span>
                <span className="text-muted-foreground">
                  {Math.round((job.progress ?? 0) * 100)}%
                </span>
              </div>
              <Progress value={Math.round((job.progress ?? 0) * 100)} className="mt-1.5" />
            </div>
          </CardContent>
        </Card>
      )}

      {message && (
        <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
          {message}
        </div>
      )}

      {!projectId ? (
        <EmptyHint />
      ) : loading ? (
        <div className="h-48 animate-pulse rounded-xl border bg-muted/40" />
      ) : tree.sections.length === 0 ? (
        <div className="rounded-xl border border-dashed py-16 text-center text-sm text-muted-foreground">
          还没有大纲。点击「生成大纲」，系统会按文献卡片聚类出章节树。
        </div>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[1fr,18rem]">
          <div className="space-y-3">
            {tree.sections.map((section, index) => (
              <SectionCard
                key={`${section.key}-${index}`}
                section={section}
                whitelist={whitelist}
                onChange={(patch) => updateSection(index, patch)}
                onMove={(delta) => moveSection(index, delta)}
                onRemove={() => removeSection(index)}
              />
            ))}
            <Button variant="outline" onClick={addSection} className="w-full">
              <Plus className="h-4 w-4" /> 新增章节
            </Button>
          </div>

          <aside className="space-y-4">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">大纲状态</CardTitle>
              </CardHeader>
              <CardContent className="space-y-1.5 text-xs text-muted-foreground">
                <p>版本 v{version}{saving ? ' · 保存中…' : ''}</p>
                <p>章节 {tree.sections.length} 个</p>
                <p>
                  已分配文献 {assignedKeys.size} / {whitelist.length}
                </p>
                {unassigned.length > 0 && (
                  <p className="text-warning">
                    有 {unassigned.length} 篇入库文献未分配到任何章节
                  </p>
                )}
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">引用真实性</CardTitle>
              </CardHeader>
              <CardContent className="space-y-1.5 text-xs text-muted-foreground">
                <p>章节只能分配写作白名单内的文献（R1 + R2 前置）。</p>
                <p>手工编辑同样会被服务端收敛到白名单内。</p>
                <p>
                  <Link href={`/write?project=${projectId}`} className="underline">
                    去写作工作台 →
                  </Link>
                </p>
              </CardContent>
            </Card>
          </aside>
        </div>
      )}
    </div>
  );
}

function SectionCard({
  section,
  whitelist,
  onChange,
  onMove,
  onRemove,
}: {
  section: OutlineSection;
  whitelist: string[];
  onChange: (patch: Partial<OutlineSection>) => void;
  onMove: (delta: number) => void;
  onRemove: () => void;
}) {
  const isFrame = section.kind === 'frame';
  const [points, setPoints] = React.useState((section.argument_points ?? []).join('\n'));

  React.useEffect(() => {
    setPoints((section.argument_points ?? []).join('\n'));
  }, [section.argument_points]);

  return (
    <Card>
      <CardHeader className="flex-row items-start gap-2 space-y-0 pb-3">
        <div className="flex flex-col pt-1.5 text-muted-foreground">
          <button aria-label="上移" onClick={() => onMove(-1)} className="hover:text-foreground">
            <GripVertical className="h-4 w-4" />
          </button>
        </div>
        <div className="flex-1 space-y-2">
          <div className="flex items-center gap-2">
            <Input
              value={section.title}
              onChange={(e) => onChange({ title: e.target.value })}
              className="h-8 font-medium"
            />
            {isFrame ? (
              <Badge variant="muted" className="shrink-0">
                框架章节
              </Badge>
            ) : (
              <Badge variant="outline" className="shrink-0">
                {section.key}
              </Badge>
            )}
          </div>
          <Textarea
            value={section.summary ?? ''}
            onChange={(e) => onChange({ summary: e.target.value })}
            placeholder="本章要论证什么"
            className="min-h-[48px] text-xs"
          />
        </div>
        {!isFrame && (
          <button
            aria-label="删除章节"
            onClick={onRemove}
            className="pt-1.5 text-muted-foreground hover:text-destructive"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        )}
      </CardHeader>
      {!isFrame && (
        <CardContent className="space-y-3 pt-0">
          <div>
            <p className="mb-1 text-xs font-medium text-muted-foreground">论证要点（每行一条）</p>
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
              className="min-h-[60px] text-xs"
            />
          </div>
          <div>
            <p className="mb-1 flex items-center gap-1 text-xs font-medium text-muted-foreground">
              <Sparkles className="h-3 w-3" /> 分配文献（{section.cite_keys?.length ?? 0}）
            </p>
            <CiteKeyPicker
              whitelist={whitelist}
              selected={section.cite_keys ?? []}
              onChange={(keys) => onChange({ cite_keys: keys })}
            />
          </div>
        </CardContent>
      )}
    </Card>
  );
}

function EmptyHint() {
  return (
    <div className="rounded-xl border border-dashed py-16 text-center text-sm text-muted-foreground">
      请先从
      <Link href="/projects" className="mx-1 underline">
        项目列表
      </Link>
      进入某个项目。
    </div>
  );
}
