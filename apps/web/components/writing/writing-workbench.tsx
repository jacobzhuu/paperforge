'use client';

import * as React from 'react';
import Link from 'next/link';
import {
  AlertTriangle,
  Check,
  ChevronDown,
  Loader2,
  Maximize2,
  Minimize2,
  PenLine,
  RefreshCw,
  Save,
  ShieldCheck,
  Image as ImageIcon,
  X,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Drawer } from '@/components/ui/drawer';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useToast } from '@/components/ui/toast';
import { LoadState } from '@/components/layout/load-state';
import { WorkbenchHeader } from '@/components/project/workbench-header';
import { WorkbenchFooterNav } from '@/components/project/workbench-footer-nav';
import { useJobEvent, useJobFinished, useProject } from '@/components/project/project-context';
import { SectionEditor, type RefineRequest } from '@/components/writing/section-editor';
import { DiffPreviewDialog } from '@/components/writing/diff-preview-dialog';
import { ValidationPanel } from '@/components/writing/validation-panel';
import { MarkdownPreview } from '@/components/writing/markdown-preview';
import {
  generateQuality,
  generateSections,
  getCitationAudit,
  getMarkdownPreview,
  getNumLint,
  getQuality,
  listSections,
  refineText,
  listVisuals,
  suggestVisuals,
  generateVisual,
  approveVisual,
  rejectVisual,
  regenerateVisual,
  getRuntimeSettings,
} from '@/lib/api';
import type {
  CitationAudit,
  MarkdownPreview as MarkdownPreviewData,
  NumLintReport,
  PaperSection,
  QualityReport,
  RefineAction,
  SectionIR,
  VisualAsset,
} from '@/lib/types';
import { countWords, normalizeSectionIR } from '@/lib/ir-serde';
import { describeError } from '@/lib/errors';
import { projectHref } from '@/lib/pipeline';
import { updateSection } from '@/lib/api';
import { cn } from '@/lib/utils';

type View = 'editor' | 'preview';

const SECTION_STATUS: Record<PaperSection['status'], { label: string; variant: 'muted' | 'secondary' | 'success' }> = {
  generated: { label: '已生成', variant: 'muted' },
  edited: { label: '已编辑', variant: 'secondary' },
  approved: { label: '已定稿', variant: 'success' },
};

/** 本地草稿键：崩溃或误关标签页后能恢复。 */
function draftKey(projectId: string, sectionKey: string): string {
  return `paperforge:draft:${projectId}:${sectionKey}`;
}

export function WritingWorkbench() {
  const {
    projectId,
    paperType,
    whitelist,
    busy,
    tracked,
    startJob,
    reload: reloadProject,
  } = useProject();
  const { toast } = useToast();
  /** 正文正在被逐节写出——章节树底部给一行提示，说明列表还会继续变长。 */
  const writing = tracked?.job.stage === 'write';

  const [sections, setSections] = React.useState<PaperSection[]>([]);
  const [activeKey, setActiveKey] = React.useState<string | null>(null);
  const [audit, setAudit] = React.useState<CitationAudit | undefined>();
  const [preview, setPreview] = React.useState<MarkdownPreviewData | undefined>();
  const [quality, setQuality] = React.useState<QualityReport | undefined>();
  const [numlint, setNumlint] = React.useState<NumLintReport | undefined>();
  const [visuals, setVisuals] = React.useState<VisualAsset[]>([]);
  const [aiGenerationAvailable, setAiGenerationAvailable] = React.useState(false);
  const [view, setView] = React.useState<View>('editor');
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);

  const [draft, setDraft] = React.useState<SectionIR | null>(null);
  const [pristine, setPristine] = React.useState<string>('');
  const [saving, setSaving] = React.useState(false);
  const [savedAt, setSavedAt] = React.useState<Date | null>(null);
  const [pendingSwitch, setPendingSwitch] = React.useState<string | null>(null);
  const [confirmRegenerate, setConfirmRegenerate] = React.useState(false);
  const [focusMode, setFocusMode] = React.useState(false);
  const [panelOpen, setPanelOpen] = React.useState(false);
  /** 递增即强制 Tiptap 重挂载（放弃草稿、服务端回填后用）。 */
  const [editorRevision, setEditorRevision] = React.useState(0);

  const [refining, setRefining] = React.useState<RefineAction | null>(null);
  const [diff, setDiff] = React.useState<{
    original: string;
    refined: string;
    note?: string | null;
    apply: (text: string) => void;
  } | null>(null);

  const reload = React.useCallback(async () => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    const [rows, auditResult, previewResult, qualityResult, lintResult, visualResult, runtime] = await Promise.all([
      listSections(projectId),
      getCitationAudit(projectId),
      getMarkdownPreview(projectId),
      getQuality(projectId),
      paperType === 'original' ? getNumLint(projectId) : Promise.resolve({ data: undefined }),
      listVisuals(projectId),
      getRuntimeSettings(),
    ]);
    setSections(rows.data);
    setAudit(auditResult.data);
    setPreview(previewResult.data);
    setQuality(qualityResult.data);
    setNumlint(lintResult.data as NumLintReport | undefined);
    setVisuals(visualResult.data);
    setAiGenerationAvailable(Boolean(
      runtime.data?.ai_images_enabled && runtime.data?.image_provider_configured,
    ));
    setActiveKey((current) => current ?? rows.data[0]?.section_key ?? null);
    setLoadError(null);
    setLoading(false);
  }, [projectId, paperType]);

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

  const active = sections.find((s) => s.section_key === activeKey);
  const dirtyRef = React.useRef(false);

  /**
   * 写作期间章节逐节浮现（docs/ui-design.md §3.1）。
   *
   * worker 的 write 阶段**写完一节存一节**并逐节 emit `write.section`
   * （pipelines/document.py::write_document）。此前前端只拿这个事件拼了一句状态文案，
   * 于是一次 18 分钟的写作全程只有一根进度条——draft-first 的后端配了一个
   * 「等着看」的前端。这里收到事件就重取章节列表，正文一节一节长出来。
   *
   * 只重取 sections：audit / preview / quality 都要等全文写完才有意义，
   * 中途拉只会拿到半截数据并让右侧校验面板反复闪烁。收尾时 useJobFinished
   * 会走完整的 runReload 把它们补齐。
   */
  useJobEvent((event) => {
    if (event.type !== 'write.section' || !projectId) return;
    void listSections(projectId)
      .then(({ data }) => {
        setSections((prev) => {
          // 正在编辑的那一节保留本地行：服务端行的 updated_at 一变，
          // 载入 effect 就会重跑并弹出「已恢复未保存的草稿」——数据不会丢
          // （localStorage 每次编辑都落盘），但对正在打字的人是一次莫名其妙的打断。
          if (!dirtyRef.current) return data;
          const keep = prev.find((s) => s.section_key === activeKeyRef.current);
          if (!keep) return data;
          return data.map((s) => (s.section_key === keep.section_key ? keep : s));
        });
        // 首节到达时正文区还是空态，把它选中，用户立刻有东西可读。
        setActiveKey((current) => current ?? data[0]?.section_key ?? null);
        setLoading(false);
      })
      .catch(() => {
        /* 增量刷新失败不影响任务本身，收尾时 runReload 会兜底 */
      });
  });

  // 载入章节：优先恢复本地草稿（上次崩溃/误关留下的未保存修改）。
  React.useEffect(() => {
    if (!active) {
      setDraft(null);
      setPristine('');
      return;
    }
    const body = active.body_ir as SectionIR;
    const server: SectionIR = normalizeSectionIR(
      body && 'blocks' in body
        ? body
        : {
            key: active.section_key,
            level: 1,
            title: active.title,
            blocks: [],
            citation_warnings: [],
          },
    );
    const serverJson = JSON.stringify(server);

    let next = server;
    if (typeof window !== 'undefined') {
      const stored = window.localStorage.getItem(draftKey(projectId, active.section_key));
      if (stored && stored !== serverJson) {
        try {
          next = normalizeSectionIR(JSON.parse(stored) as SectionIR);
          toast({
            title: '已恢复未保存的草稿',
            description: `「${active.title}」有上次未保存的修改。不想要就点「放弃草稿」。`,
          });
        } catch {
          window.localStorage.removeItem(draftKey(projectId, active.section_key));
        }
      }
    }
    setDraft(next);
    setPristine(serverJson);
    setSavedAt(active.updated_at ? new Date(active.updated_at) : null);
    // toast 是稳定引用，但不入依赖以免章节切换时重复触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active?.section_key, active?.updated_at, projectId]);

  const dirty = draft !== null && JSON.stringify(draft) !== pristine;

  // 供 write.section 增量刷新读取——事件回调不能依赖闭包里的旧值。
  const activeKeyRef = React.useRef<string | null>(activeKey);
  dirtyRef.current = dirty;
  activeKeyRef.current = activeKey;

  // 本地草稿随编辑落盘。
  React.useEffect(() => {
    if (!draft || !active || typeof window === 'undefined') return;
    const key = draftKey(projectId, active.section_key);
    if (dirty) window.localStorage.setItem(key, JSON.stringify(draft));
    else window.localStorage.removeItem(key);
  }, [draft, dirty, active, projectId]);

  // 直接关标签页此前会静默丢掉未保存的修改——只拦截了切换章节。
  React.useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [dirty]);

  const requestSwitch = (key: string) => {
    if (key === activeKey) return;
    if (dirty) {
      setPendingSwitch(key);
      return;
    }
    setActiveKey(key);
  };

  const save = async () => {
    if (!draft || !active || !projectId) return;
    setSaving(true);
    try {
      const result = await updateSection(projectId, active.section_key, draft, draft.title);
      if (result.data) {
        // 服务端会再过一次白名单（R2 兜底），以返回值为准。
        setSections((prev) =>
          prev.map((s) => (s.section_key === result.data!.section_key ? result.data! : s)),
        );
        const body = result.data.body_ir as SectionIR;
        const normalized = normalizeSectionIR(
          body && 'blocks' in body ? body : draft,
        );
        // 服务端会再过一次 R2 白名单，可能删掉越权引用。真被改动时必须让
        // 编辑器跟着重挂载，否则屏幕上还留着已经被服务端剔除的引用 chip。
        // 未改动时不重挂载——否则每存一次都会丢光标位置。
        if (JSON.stringify(normalized) !== JSON.stringify(draft)) {
          setEditorRevision((r) => r + 1);
        }
        setPristine(JSON.stringify(normalized));
        setDraft(normalized);
        setSavedAt(new Date());
        window.localStorage.removeItem(draftKey(projectId, active.section_key));
        toast({ title: '本节已保存', variant: 'success' });
        runReload();
        reloadProject();
      } else {
        toast({
          title: '本节未保存',
          description: '后端不可用。修改已留在本地草稿里，恢复后再点保存。',
          variant: 'error',
        });
      }
    } catch (err) {
      // finally 是关键：写在 await 之后时，一次 500 会让保存按钮永久禁用，
      // 用户再也没有办法把稿子存下来。
      toast({ title: '本节未能保存', description: describeError(err), variant: 'error' });
    } finally {
      setSaving(false);
    }
  };

  const discardDraft = () => {
    if (!active) return;
    window.localStorage.removeItem(draftKey(projectId, active.section_key));
    setDraft(JSON.parse(pristine) as SectionIR);
    // 光重置 draft state 不够：Tiptap 的文档只在挂载时从 content 初始化，
    // 不跟着 props 走。不强制重挂载的话，用户点了「放弃草稿」会看到正文
    // **原封不动**，而背后的 draft 已经换成服务端版本——两者悄悄分叉。
    setEditorRevision((r) => r + 1);
  };

  const startWriting = async () => {
    setConfirmRegenerate(false);
    try {
      const started = await generateSections(projectId, true);
      startJob(started.data, '后端不可用：无法开始写作');
    } catch (err) {
      toast({ title: '写作未能启动', description: describeError(err), variant: 'error' });
    }
  };

  const runQuality = async () => {
    try {
      const started = await generateQuality(projectId);
      startJob(started.data, '后端不可用：无法生成质量报告');
    } catch (err) {
      toast({ title: '质量报告未能启动', description: describeError(err), variant: 'error' });
    }
  };

  const startVisualSuggestions = async () => {
    try {
      const started = await suggestVisuals(projectId);
      startJob(started.data, '后端不可用：无法生成视觉建议');
    } catch (err) {
      toast({ title: '视觉建议未能启动', description: describeError(err), variant: 'error' });
    }
  };

  const startVisualGeneration = async (visual: VisualAsset) => {
    try {
      const started = await generateVisual(projectId, visual.id);
      startJob(started.data, '后端不可用：无法生成预览');
    } catch (err) {
      toast({ title: '预览未能生成', description: describeError(err), variant: 'error' });
    }
  };

  const approveVisualIntoPaper = async (
    visual: VisualAsset,
    sectionKey: string,
    blockIndex: number,
  ) => {
    const section = sections.find((item) => item.section_key === sectionKey);
    const body = section?.body_ir as SectionIR | undefined;
    try {
      await approveVisual(
        projectId,
        visual.id,
        sectionKey,
        Math.max(0, Math.min(blockIndex, body?.blocks?.length ?? 0)),
      );
      toast({ title: '图片已插入论文', variant: 'success' });
      await reload();
      setEditorRevision((revision) => revision + 1);
    } catch (err) {
      toast({ title: '图片未能插入', description: describeError(err), variant: 'error' });
    }
  };

  const rejectVisualSuggestion = async (visual: VisualAsset) => {
    try {
      await rejectVisual(projectId, visual.id);
      runReload();
    } catch (err) {
      toast({ title: '建议未能拒绝', description: describeError(err), variant: 'error' });
    }
  };

  const regenerateVisualPreview = async (visual: VisualAsset) => {
    try {
      const revision = await regenerateVisual(projectId, visual.id);
      if (revision.data) {
        const started = await generateVisual(projectId, revision.data.id);
        startJob(started.data, '后端不可用：无法重新生成预览');
      }
    } catch (err) {
      toast({ title: '重新生成失败', description: describeError(err), variant: 'error' });
    }
  };

  const onRefine = async (request: RefineRequest) => {
    if (!active) return;
    setRefining(request.action);
    try {
      const result = await refineText(projectId, active.section_key, request.action, request.text);
      if (!result.data) {
        toast({ title: '润色未执行', description: '后端不可用。', variant: 'error' });
        return;
      }
      setDiff({
        original: request.text,
        refined: result.data.refined,
        note: result.data.note,
        apply: request.apply,
      });
    } catch (err) {
      toast({ title: '润色失败', description: describeError(err), variant: 'error' });
    } finally {
      setRefining(null);
    }
  };

  const totalWords = sections.reduce((sum, s) => sum + s.word_count, 0);
  const liveWords = draft ? countWords(draft) : 0;

  // 需要用户处理的信任告警数，用于窄屏「校验」按钮上的角标。
  const trustAlerts =
    (audit?.hallucinated_cite_keys.length ?? 0) +
    (paperType === 'original' && numlint && !numlint.consistent ? numlint.unsourced_count : 0);

  // 抽屉与右栏共用同一个面板实例定义。
  const validationPanel = (
    <ValidationPanel
      activeSection={active}
      activeDraft={draft}
      audit={audit}
      quality={quality}
      numlint={numlint}
      showNumbers={paperType === 'original'}
      onGenerateQuality={runQuality}
      busy={busy}
      onJumpToSection={(key) => {
        requestSwitch(key);
        setPanelOpen(false);
      }}
    />
  );

  return (
    <div className="space-y-4">
      <WorkbenchHeader
        title="写作工作台"
        description={`共 ${sections.length} 节 · ${totalWords.toLocaleString()} 字`}
        actions={
          <>
            <SaveStatus dirty={dirty} saving={saving} savedAt={savedAt} />
            {dirty && (
              <Button variant="ghost" size="sm" onClick={discardDraft}>
                放弃草稿
              </Button>
            )}
            <Button variant="outline" onClick={save} disabled={!draft || saving || !dirty}>
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              保存本节
            </Button>
            <Button onClick={() => setConfirmRegenerate(true)} disabled={!projectId || busy}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <PenLine className="h-4 w-4" />}
              重新生成全文
            </Button>
          </>
        }
      />

      {!focusMode && (
        <VisualSuggestions
          visuals={visuals}
          sections={sections}
          activeSectionKey={activeKey}
          busy={busy}
          aiGenerationAvailable={aiGenerationAvailable}
          onSuggest={startVisualSuggestions}
          onGenerate={startVisualGeneration}
          onApprove={approveVisualIntoPaper}
          onReject={rejectVisualSuggestion}
          onRegenerate={regenerateVisualPreview}
        />
      )}

      <LoadState loading={loading} error={loadError} onRetry={runReload} skeletonClassName="h-96">
        {sections.length === 0 ? (
          <EmptyState projectId={projectId} writing={writing} />
        ) : (
          <div
            className={cn(
              'grid gap-6 lg:grid-cols-[12rem,minmax(0,1fr)]',
              /*
               * 三栏只在 2xl（1536px+）才成立。
               *
               * 实测：1280px 视口下 侧栏 224 + 章节树 208 + 右栏 320 + 间距 = 752px 的
               * 外壳，正文只剩 416px（约 43 字符/行），仍远低于 60–80 的舒适区。
               * 与其硬塞三栏，不如在窄屏把校验面板收进抽屉——正文优先。
               */
              !focusMode && 'xl:grid-cols-[13rem,minmax(0,1fr)] 2xl:grid-cols-[13rem,minmax(0,1fr),19rem]',
            )}
          >
            <SectionTree
              sections={sections}
              activeKey={activeKey}
              onSelect={requestSwitch}
              dirtyKey={dirty ? activeKey : null}
              writing={writing}
            />

            <div className="min-w-0 space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Tabs value={view} onValueChange={(v) => setView(v as View)}>
                  <TabsList>
                    <TabsTrigger value="editor">编辑</TabsTrigger>
                    <TabsTrigger value="preview">全文预览</TabsTrigger>
                  </TabsList>
                </Tabs>
                <div className="flex items-center gap-2">
                  {view === 'editor' && draft && (
                    <span className="text-xs tabular-nums text-muted-foreground">
                      本节 {liveWords.toLocaleString()} 字
                    </span>
                  )}
                  {/*
                    单章重写：后端只有整份文稿的 POST /sections/generate，
                    没有按 section 的端点。这里**显示但禁用**，而不是隐藏——
                    让用户看得见能力边界，否则「为什么只能全量重跑」无从得知。
                  */}
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled
                    title="后端目前只支持整份文稿重生成，暂无单章重写端点"
                  >
                    <RefreshCw className="h-3.5 w-3.5" /> 重写本章
                  </Button>
                  {/* 窄屏（<2xl）没有右栏的位置，校验面板收进抽屉。 */}
                  {!focusMode && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setPanelOpen(true)}
                      className="2xl:hidden"
                    >
                      <ShieldCheck className="h-3.5 w-3.5" /> 校验
                      {trustAlerts > 0 && (
                        <Badge variant="destructive" className="ml-1 text-xs">
                          {trustAlerts}
                        </Badge>
                      )}
                    </Button>
                  )}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setFocusMode((v) => !v)}
                    aria-pressed={focusMode}
                  >
                    {focusMode ? (
                      <Minimize2 className="h-3.5 w-3.5" />
                    ) : (
                      <Maximize2 className="h-3.5 w-3.5" />
                    )}
                    {focusMode ? '退出专注' : '专注模式'}
                  </Button>
                </div>
              </div>

              {view === 'editor' && draft && (
                /* 正文按 measure 限宽而不是撑满剩余空间：此前 1280px 屏上
                   正文栏只有 390px（约 24–28 字符/行），远低于 60–80 的舒适区。 */
                <div className={cn('mx-auto w-full', focusMode ? 'max-w-[78ch]' : 'max-w-[72ch]')}>
                  <SectionEditor
                    key={`${active?.section_key}:${editorRevision}`}
                    section={draft}
                    whitelist={whitelist}
                    onChange={setDraft}
                    softChecks={quality?.soft_check ?? []}
                    onRefine={onRefine}
                    refining={refining}
                    visuals={visuals}
                  />
                </div>
              )}

              {view === 'preview' && (
                <div className="mx-auto w-full max-w-[78ch]">
                  <MarkdownPreview markdown={preview?.markdown ?? ''} />
                </div>
              )}
            </div>

            {!focusMode && (
              <aside className="hidden scrollbar-thin 2xl:sticky 2xl:top-4 2xl:block 2xl:max-h-[calc(100vh-2rem)] 2xl:self-start 2xl:overflow-y-auto 2xl:pb-4">
                {validationPanel}
              </aside>
            )}
          </div>
        )}
      </LoadState>

      {/* <2xl 时的校验抽屉：不占正文宽度，但一键可达。 */}
      <Drawer
        open={panelOpen}
        onClose={() => setPanelOpen(false)}
        title="校验"
        description="引用真实性、数字一致性与质量提示"
        className="max-w-md"
      >
        {validationPanel}
      </Drawer>

      <Dialog
        open={pendingSwitch !== null}
        onClose={() => setPendingSwitch(null)}
        title="放弃未保存的编辑？"
        description="当前章节有尚未保存的修改，切走后这些修改会丢失。"
        footer={
          <>
            <Button variant="outline" onClick={() => setPendingSwitch(null)}>
              留在本节
            </Button>
            <Button
              onClick={async () => {
                const target = pendingSwitch;
                setPendingSwitch(null);
                await save();
                if (target) setActiveKey(target);
              }}
            >
              保存后切换
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (active) window.localStorage.removeItem(draftKey(projectId, active.section_key));
                setActiveKey(pendingSwitch);
                setPendingSwitch(null);
              }}
            >
              放弃修改
            </Button>
          </>
        }
      />

      {/* 后端只有全文重生成端点（POST /sections/generate 重跑整份文稿），
          没有单章重写。既然一次点击会覆盖所有章节的手工修改，就必须先确认。 */}
      <Dialog
        open={confirmRegenerate}
        onClose={() => setConfirmRegenerate(false)}
        title="重新生成全文？"
        description="后端目前只支持整份文稿重生成，不能只重写某一章。"
        footer={
          <>
            <Button variant="outline" onClick={() => setConfirmRegenerate(false)}>
              取消
            </Button>
            <Button variant="destructive" onClick={startWriting}>
              覆盖并重新生成
            </Button>
          </>
        }
      >
        <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 p-3 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning-strong" />
          <div className="space-y-1">
            <p>
              <span className="font-medium">全部 {sections.length} 节</span>
              会按当前大纲重新生成，你在任何一节里的手工修改都会被覆盖。
            </p>
            <p className="text-xs text-muted-foreground">
              已导出的产物不受影响，仍可在导出中心下载。
            </p>
          </div>
        </div>
      </Dialog>

      <DiffPreviewDialog
        open={diff !== null}
        original={diff?.original ?? ''}
        refined={diff?.refined ?? ''}
        note={diff?.note}
        onCancel={() => setDiff(null)}
        onAccept={() => {
          diff?.apply(diff.refined);
          setDiff(null);
        }}
      />

      <WorkbenchFooterNav current="write" />
    </div>
  );
}

function VisualSuggestions({
  visuals,
  sections,
  activeSectionKey,
  busy,
  aiGenerationAvailable,
  onSuggest,
  onGenerate,
  onApprove,
  onReject,
  onRegenerate,
}: {
  visuals: VisualAsset[];
  sections: PaperSection[];
  activeSectionKey: string | null;
  busy: boolean;
  aiGenerationAvailable: boolean;
  onSuggest: () => void;
  onGenerate: (visual: VisualAsset) => void;
  onApprove: (visual: VisualAsset, sectionKey: string, blockIndex: number) => void;
  onReject: (visual: VisualAsset) => void;
  onRegenerate: (visual: VisualAsset) => void;
}) {
  const pending = visuals.filter((visual) => visual.review_status === 'pending');
  return (
    <section className="rounded-xl border bg-card/60 p-3" aria-label="视觉建议">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="flex items-center gap-2 text-sm font-medium">
            <ImageIcon className="h-4 w-4 text-primary" /> 视觉建议
            {pending.length > 0 && <Badge variant="secondary">{pending.length}</Badge>}
          </p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            建议不会自动插入论文；AI 插图也只在你点击生成后调用外部服务。
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={onSuggest} disabled={busy}>
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          分析全文
        </Button>
      </div>
      {pending.length > 0 && (
        <div className="mt-3 grid gap-3 xl:grid-cols-2">
          {pending.map((visual) => (
            <VisualSuggestionCard
              key={visual.id}
              visual={visual}
              sections={sections}
              defaultSection={visual.target_section_key || activeSectionKey || sections[0]?.section_key || ''}
              aiGenerationAvailable={aiGenerationAvailable}
              onGenerate={onGenerate}
              onApprove={onApprove}
              onReject={onReject}
              onRegenerate={onRegenerate}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function VisualSuggestionCard({
  visual,
  sections,
  defaultSection,
  aiGenerationAvailable,
  onGenerate,
  onApprove,
  onReject,
  onRegenerate,
}: {
  visual: VisualAsset;
  sections: PaperSection[];
  defaultSection: string;
  aiGenerationAvailable: boolean;
  onGenerate: (visual: VisualAsset) => void;
  onApprove: (visual: VisualAsset, sectionKey: string, blockIndex: number) => void;
  onReject: (visual: VisualAsset) => void;
  onRegenerate: (visual: VisualAsset) => void;
}) {
  const [sectionKey, setSectionKey] = React.useState(defaultSection);
  const selectedSection = sections.find((section) => section.section_key === sectionKey);
  const maxBlockIndex = ((selectedSection?.body_ir as SectionIR | undefined)?.blocks ?? []).length;
  const [blockIndex, setBlockIndex] = React.useState(
    Math.min(visual.suggested_block_index ?? maxBlockIndex, maxBlockIndex),
  );
  const preview = visual.renditions.png?.url || visual.renditions.svg?.url;
  const generating = visual.generation_status === 'queued' || visual.generation_status === 'running';
  return (
    <article className="overflow-hidden rounded-lg border bg-background">
      {preview ? (
        // eslint-disable-next-line @next/next/no-img-element -- authenticated API rendition URL
        <img src={preview} alt={visual.alt_text || visual.caption} className="h-44 w-full bg-white object-contain" />
      ) : (
        <div className="flex h-28 items-center justify-center bg-muted/50 text-xs text-muted-foreground">
          {generating ? <Loader2 className="h-5 w-5 animate-spin" /> : '尚未生成预览'}
        </div>
      )}
      <div className="space-y-2 p-3">
        <div className="flex items-start justify-between gap-2">
          <div>
            <p className="text-sm font-medium">{visual.title || visual.caption || '未命名视觉'}</p>
            <p className="text-xs text-muted-foreground">
              {visual.kind === 'chart' ? '数据图表' : visual.kind === 'diagram' ? '学术示意图' : 'AI 概念插图'}
              {' · '}v{visual.version}
            </p>
          </div>
          {visual.kind === 'ai_image' && <Badge variant="warning">外部 AI</Badge>}
        </div>
        {visual.error_message && <p className="text-xs text-destructive-strong">{visual.error_message}</p>}
        {visual.caption_hint && <p className="text-xs text-warning-foreground">{visual.caption_hint}</p>}
        {visual.generation_status === 'ready' && (
          <div className="grid gap-2 sm:grid-cols-[1fr,8rem]">
            <select
              value={sectionKey}
              onChange={(event) => {
                const next = event.target.value;
                setSectionKey(next);
                const section = sections.find((item) => item.section_key === next);
                setBlockIndex(((section?.body_ir as SectionIR | undefined)?.blocks ?? []).length);
              }}
              className="h-9 w-full rounded-md border bg-background px-2 text-sm"
              aria-label="插入章节"
            >
              {sections.map((section) => (
                <option key={section.section_key} value={section.section_key}>{section.title}</option>
              ))}
            </select>
            <label className="flex items-center gap-1 text-xs text-muted-foreground">
              位置
              <input
                type="number"
                min={0}
                max={maxBlockIndex}
                value={blockIndex}
                onChange={(event) => setBlockIndex(Number(event.target.value))}
                className="h-9 min-w-0 flex-1 rounded-md border bg-background px-2 text-sm text-foreground"
                aria-label="插入 block 位置"
              />
            </label>
          </div>
        )}
        <div className="flex flex-wrap gap-1.5">
          {visual.generation_status !== 'ready' && (
            <Button
              size="sm"
              onClick={() => onGenerate(visual)}
              disabled={generating || (visual.kind === 'ai_image' && !aiGenerationAvailable)}
              title={visual.kind === 'ai_image' && !aiGenerationAvailable ? 'AI 图像生成未启用或未配置密钥' : undefined}
            >
              {generating && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {visual.kind === 'ai_image' ? '生成预览（可能计费）' : '生成预览'}
            </Button>
          )}
          {visual.generation_status === 'ready' && (
            <>
              <Button size="sm" onClick={() => onApprove(visual, sectionKey, blockIndex)} disabled={!sectionKey}>
                <Check className="h-3.5 w-3.5" /> 批准并插入
              </Button>
              <Button variant="outline" size="sm" onClick={() => onRegenerate(visual)}>
                <RefreshCw className="h-3.5 w-3.5" /> 重新生成
              </Button>
            </>
          )}
          <Button variant="ghost" size="sm" onClick={() => onReject(visual)}>
            <X className="h-3.5 w-3.5" /> 拒绝
          </Button>
        </div>
      </div>
    </article>
  );
}

function SectionTree({
  sections,
  activeKey,
  onSelect,
  dirtyKey,
  writing,
}: {
  sections: PaperSection[];
  activeKey: string | null;
  onSelect: (key: string) => void;
  dirtyKey: string | null;
  /** write 阶段进行中：列表还会继续变长。 */
  writing?: boolean;
}) {
  // 窄屏默认折叠：7 节全展开会把正文顶到一屏之外。
  const [expanded, setExpanded] = React.useState(false);
  const activeTitle = sections.find((s) => s.section_key === activeKey)?.title ?? '选择章节';

  return (
    <>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center justify-between gap-2 rounded-md border px-3 py-2 text-sm transition-colors hover:bg-accent/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring lg:hidden"
      >
        <span className="min-w-0 truncate font-medium">{activeTitle}</span>
        <span className="shrink-0 text-xs text-muted-foreground">
          {sections.length} 节
          <ChevronDown
            className={cn('ml-1 inline h-3.5 w-3.5 transition-transform', expanded && 'rotate-180')}
          />
        </span>
      </button>

      <nav
        aria-label="章节"
        className={cn(
          'space-y-0.5 lg:sticky lg:top-4 lg:block lg:max-h-[calc(100vh-2rem)] lg:self-start lg:overflow-y-auto scrollbar-thin',
          expanded ? 'block' : 'hidden',
        )}
      >
        {sections.map((section) => {
        const active = section.section_key === activeKey;
        const status = SECTION_STATUS[section.status] ?? SECTION_STATUS.generated;
          return (
            <button
              key={section.section_key}
              onClick={() => {
                onSelect(section.section_key);
                setExpanded(false);
              }}
              aria-current={active ? 'true' : undefined}
              className={cn(
                'w-full rounded-md px-2.5 py-2 text-left text-sm transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                active ? 'bg-secondary font-medium' : 'hover:bg-muted/60',
              )}
            >
              <span className="flex items-center justify-between gap-2">
                <span className="line-clamp-2">{section.title}</span>
                <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                  {section.word_count}
                </span>
              </span>
              <span className="mt-1 flex items-center gap-1">
                <Badge variant={status.variant} className="text-xs">
                  {status.label}
                </Badge>
                {dirtyKey === section.section_key && (
                  <Badge variant="warning" className="text-xs">
                    未保存
                  </Badge>
                )}
              </span>
            </button>
          );
        })}

        {/*
          「还在写」的提示（ui-design.md 原则 08 Calm by default）：
          一个脉冲点 + 一行字，不用进度条也不用 spinner——进度条已经在 shell 的
          任务条上了，这里只需要说明「列表还会继续变长」。
        */}
        {writing && (
          <p
            className="flex items-center gap-2 px-2.5 py-2 text-xs text-muted-foreground"
            role="status"
            aria-live="polite"
          >
            <span className="relative flex h-1.5 w-1.5 shrink-0" aria-hidden>
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary/50 motion-reduce:animate-none" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-primary" />
            </span>
            正在写作，章节会陆续出现
          </p>
        )}
      </nav>
    </>
  );
}

function SaveStatus({
  dirty,
  saving,
  savedAt,
}: {
  dirty: boolean;
  saving: boolean;
  savedAt: Date | null;
}) {
  if (saving) {
    return (
      <span className="flex items-center gap-1 text-xs text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin" /> 保存中…
      </span>
    );
  }
  if (dirty) {
    return (
      <span className="flex items-center gap-1 text-xs text-warning-strong" role="status">
        <span className="h-1.5 w-1.5 rounded-full bg-warning" /> 有未保存的修改
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1 text-xs text-muted-foreground" role="status">
      <Check className="h-3 w-3 text-success-strong" />
      {savedAt ? `已保存 ${savedAt.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}` : '已保存'}
    </span>
  );
}

function EmptyState({ projectId, writing }: { projectId: string; writing?: boolean }) {
  /*
   * 写作刚开始、第一节还没落库的那几十秒，正文区是空的。此前这里照样显示
   * 「还没有正文，先去生成大纲」——用户刚点完「重新生成全文」，却被告知去做
   * 一件他已经做完的事。写作进行中要说的是「马上就来」。
   */
  if (writing) {
    return (
      <div className="flex flex-col items-center gap-2 py-20 text-center" role="status" aria-live="polite">
        <span className="relative flex h-2 w-2" aria-hidden>
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary/50 motion-reduce:animate-none" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-primary" />
        </span>
        <p className="font-serif text-lg">正在写第一节</p>
        <p className="text-sm text-muted-foreground">
          章节写完一节就会出现在这里，不用等全文跑完。
        </p>
      </div>
    );
  }
  return (
    <div className="flex flex-col items-center gap-2 py-20 text-center">
      <p className="font-serif text-lg">还没有正文</p>
      <p className="text-sm text-muted-foreground">
        先在
        <Link
          href={projectHref(projectId, 'outline')}
          className="mx-1 underline underline-offset-4"
        >
          大纲编辑器
        </Link>
        生成大纲，再点「重新生成全文」。
      </p>
      <p className="text-xs text-muted-foreground">
        也可以在项目概览点「跑通全管线」，一次跑到 PDF。
      </p>
    </div>
  );
}
