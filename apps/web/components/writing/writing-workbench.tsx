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
import { ActionMenu } from '@/components/ui/action-menu';
import { Button, buttonVariants } from '@/components/ui/button';
import { Callout } from '@/components/ui/callout';
import { Dialog } from '@/components/ui/dialog';
import { Drawer } from '@/components/ui/drawer';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useToast } from '@/components/ui/toast';
import { LoadState } from '@/components/layout/load-state';
import { ModuleError } from '@/components/layout/module-error';
import { WorkbenchHeader } from '@/components/project/workbench-header';
import { WorkbenchFooterNav } from '@/components/project/workbench-footer-nav';
import {
  useJobEvent,
  useJobFinished,
  useProjectActions,
  useProjectActivity,
  useProjectData,
} from '@/components/project/project-context';
import type { RefineRequest } from '@/components/writing/section-editor';
// 视觉能力全部来自共用模块：写作台不再维护第二套卡片与编辑器。
import { VisualCard } from '@/components/visuals/visual-card';
import { Skeleton } from '@/components/ui/skeleton';
import {
  groupVisualsByLineage,
  useVisuals,
  type VisualsController,
} from '@/components/visuals/use-visuals';
import {
  generateQuality,
  generateSections,
  getCitationAudit,
  getEvidenceUnits,
  getMarkdownPreview,
  getNumLint,
  getOutline,
  getQuality,
  listSections,
  repairQuality,
  rebuildDraft,
  refineText,
  suggestVisuals,
  updateSection,
  rewriteSectionCandidate,
  acceptSectionRewrite,
} from '@/lib/api';
import type {
  CitationAudit,
  EvidenceUnit,
  MarkdownPreview as MarkdownPreviewData,
  NumLintReport,
  PaperSection,
  QualityReport,
  QualityProfile,
  ReviewStyle,
  RefineAction,
  SectionIR,
  VisualAsset,
} from '@/lib/types';
import { buildFigureNumbering } from '@/lib/figure-numbering';
import { countWords, normalizeSectionIR, sectionPlainText } from '@/lib/ir-serde';
import { describeError, isSectionChanged } from '@/lib/errors';
import { projectHref } from '@/lib/pipeline';
import { useAsyncModule } from '@/lib/useAsyncModule';
import { cn } from '@/lib/utils';

/*
 * Tiptap、全文预览、校验和视觉编辑器都很重，但一次只会使用其中一部分。
 * 拆成独立 chunk，首次进入写作台先交付章节树和操作区。
 */
const SectionEditor = React.lazy(() =>
  import('@/components/writing/section-editor').then((module) => ({
    default: module.SectionEditor,
  })),
);
const DiffPreviewDialog = React.lazy(() =>
  import('@/components/writing/diff-preview-dialog').then((module) => ({
    default: module.DiffPreviewDialog,
  })),
);
const ValidationPanel = React.lazy(() =>
  import('@/components/writing/validation-panel').then((module) => ({
    default: module.ValidationPanel,
  })),
);
const MarkdownPreview = React.lazy(() =>
  import('@/components/writing/markdown-preview').then((module) => ({
    default: module.MarkdownPreview,
  })),
);
const VisualEditorDrawer = React.lazy(() =>
  import('@/components/visuals/visual-editor-drawer').then((module) => ({
    default: module.VisualEditorDrawer,
  })),
);
const AIGenerationDialog = React.lazy(() =>
  import('@/components/visuals/ai-generation-dialog').then((module) => ({
    default: module.AIGenerationDialog,
  })),
);

function DeferredModuleFallback({ className = 'h-48' }: { className?: string }) {
  return <Skeleton className={className} aria-label="正在加载模块" />;
}

type View = 'editor' | 'preview';

const SECTION_STATUS: Record<PaperSection['status'], { label: string; variant: 'muted' | 'secondary' | 'success' }> = {
  generated: { label: '已生成', variant: 'muted' },
  edited: { label: '已编辑', variant: 'secondary' },
  approved: { label: '已定稿', variant: 'success' },
  // 写作降级留下的缺口：这一节没有正文，只有一句说明。必须和「已生成」看得出区别，
  // 否则用户要读完整节才发现它是空的。
  needs_rewrite: { label: '待重写', variant: 'secondary' },
};

/** 本地草稿键：崩溃或误关标签页后能恢复。 */
function draftKey(projectId: string, sectionKey: string): string {
  return `paperforge:draft:${projectId}:${sectionKey}`;
}

export function WritingWorkbench() {
  const { projectId, paperType, whitelist, reload: reloadProject } = useProjectData();
  const { busy, startJob } = useProjectActions();
  const { runningStage } = useProjectActivity();
  const { toast } = useToast();
  /** 正文正在被逐节写出——章节树底部给一行提示，说明列表还会继续变长。 */
  const writing = runningStage === 'write';

  const [sections, setSections] = React.useState<PaperSection[]>([]);
  const [activeKey, setActiveKey] = React.useState<string | null>(null);
  const [view, setView] = React.useState<View>('editor');
  const [qualityProfile, setQualityProfile] = React.useState<QualityProfile>('scholarly');

  const [draft, setDraft] = React.useState<SectionIR | null>(null);
  const [pristine, setPristine] = React.useState<string>('');
  const [saving, setSaving] = React.useState(false);
  const [savedAt, setSavedAt] = React.useState<Date | null>(null);
  const [pendingSwitch, setPendingSwitch] = React.useState<string | null>(null);
  const [confirmRegenerate, setConfirmRegenerate] = React.useState(false);
  const [rewriteOpen, setRewriteOpen] = React.useState(false);
  const [rewriteInstruction, setRewriteInstruction] = React.useState('');
  const [rewriteLoading, setRewriteLoading] = React.useState(false);
  const [focusMode, setFocusMode] = React.useState(false);
  const [panelOpen, setPanelOpen] = React.useState(false);
  /** 共用的视觉编辑抽屉与生成确认框——点「调整」不再被迫跳去别的页面。 */
  const [editingVisual, setEditingVisual] = React.useState<VisualAsset | null>(null);
  const [confirmingVisual, setConfirmingVisual] = React.useState<VisualAsset | null>(null);
  /** 递增即强制 Tiptap 重挂载（放弃草稿、服务端回填后用）。 */
  const [editorRevision, setEditorRevision] = React.useState(0);

  const [refining, setRefining] = React.useState<RefineAction | null>(null);
  const [diff, setDiff] = React.useState<{
    original: string;
    refined: string;
    note?: string | null;
    apply: (text: string) => void;
  } | null>(null);

  /*
   * 七个请求此前挤在同一个 `Promise.all` 里：`listVisuals` 或 `getRuntimeSettings`
   * 任意一个 500，整页进入 loadError——正文编辑器一起打不开，哪怕 `listSections`
   * 早就成功返回了。现在章节是唯一的硬依赖，其余六项各自独立降级。
   */
  const [sectionsLoading, setSectionsLoading] = React.useState(true);
  const [sectionsError, setSectionsError] = React.useState<string | null>(null);
  const [sectionsToken, setSectionsToken] = React.useState(0);
  const reloadSections = React.useCallback(() => setSectionsToken((t) => t + 1), []);

  React.useEffect(() => {
    if (!projectId) {
      setSectionsLoading(false);
      return;
    }
    const controller = new AbortController();
    setSectionsError(null);
    setSectionsLoading(true);
    listSections(projectId, controller.signal)
      .then(({ data }) => {
        if (controller.signal.aborted) return;
        setSections(data);
        setActiveKey((current) => current ?? data[0]?.section_key ?? null);
        setSectionsLoading(false);
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setSectionsError(describeError(err));
        setSectionsLoading(false);
      });
    return () => {
      controller.abort();
    };
  }, [projectId, sectionsToken]);

  const auditModule = useAsyncModule<CitationAudit | undefined>(
    () => getCitationAudit(projectId).then((r) => r.data),
    undefined,
    [projectId],
  );
  const previewModule = useAsyncModule<MarkdownPreviewData | undefined>(
    () => getMarkdownPreview(projectId).then((r) => r.data),
    undefined,
    [projectId],
  );
  const qualityModule = useAsyncModule<QualityReport | undefined>(
    () => getQuality(projectId, qualityProfile).then((r) => r.data),
    undefined,
    [projectId, qualityProfile],
  );
  const numlintModule = useAsyncModule<NumLintReport | undefined>(
    () =>
      paperType === 'original'
        ? getNumLint(projectId).then((r) => r.data)
        : Promise.resolve(undefined),
    undefined,
    [projectId, paperType],
  );
  const evidenceModule = useAsyncModule<EvidenceUnit[]>(
    () => getEvidenceUnits(projectId).then((r) => r.data),
    [],
    [projectId],
  );
  const outlineModule = useAsyncModule(
    () => getOutline(projectId).then((r) => r.data),
    undefined,
    [projectId],
  );

  const audit = auditModule.data;
  const preview = previewModule.data;
  const quality = qualityModule.data;
  const numlint = numlintModule.data;
  const evidenceUnits = evidenceModule.data;

  const visualsModule = useVisuals(projectId);
  const { visuals } = visualsModule;

  const runReload = React.useCallback(() => {
    reloadSections();
    auditModule.reload();
    previewModule.reload();
    qualityModule.reload();
    numlintModule.reload();
    evidenceModule.reload();
    outlineModule.reload();
    visualsModule.reload();
  }, [
    reloadSections,
    auditModule,
    previewModule,
    qualityModule,
    numlintModule,
    evidenceModule,
    outlineModule,
    visualsModule,
  ]);

  const runReloadRef = React.useRef(runReload);
  React.useEffect(() => {
    runReloadRef.current = runReload;
  });
  useJobFinished(React.useCallback(() => runReloadRef.current(), []));

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
        setSectionsLoading(false);
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

  /** 返回是否保存成功——批准插图前要先确认草稿已落盘。 */
  const save = async (): Promise<boolean> => {
    if (!draft || !active || !projectId) return false;
    setSaving(true);
    try {
      const result = await updateSection(
        projectId,
        active.section_key,
        draft,
        draft.title,
        // 乐观并发的基础版本：服务端已变化时返回 409，而不是让这次保存
        // 把别处（典型是视觉批准）刚写进来的内容盖掉。
        active.updated_at ?? null,
      );
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
        return true;
      }
      toast({
        title: '本节未保存',
        description: '后端不可用。修改已留在本地草稿里，恢复后再点保存。',
        variant: 'error',
      });
      return false;
    } catch (err) {
      // 409 是「有人先改了」，不是普通失败：草稿完好，用户需要的是先看新版本。
      if (isSectionChanged(err)) {
        toast({
          title: '本节已在别处更新',
          description: '这一节在服务端有更新（比如刚插入了一张图）。请点「重新载入本节」查看最新内容后再合并你的修改。',
          variant: 'error',
        });
        return false;
      }
      // finally 是关键：写在 await 之后时，一次 500 会让保存按钮永久禁用，
      // 用户再也没有办法把稿子存下来。
      toast({ title: '本节未能保存', description: describeError(err), variant: 'error' });
      return false;
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

  const createRewriteCandidate = async () => {
    if (!active || !draft || rewriteInstruction.trim().length < 3) return;
    const savedImmediatelyBeforeRewrite = dirty;
    if (savedImmediatelyBeforeRewrite && !(await save())) return;
    setRewriteLoading(true);
    try {
      const candidate = await rewriteSectionCandidate(
        projectId,
        active.section_key,
        rewriteInstruction.trim(),
        savedImmediatelyBeforeRewrite ? undefined : active.updated_at,
      );
      setRewriteOpen(false);
      setDiff({
        original: sectionPlainText(candidate.original_body_ir),
        refined: sectionPlainText(candidate.candidate_body_ir),
        note: candidate.note ?? '候选已通过引用、数字、素材引用与结构守恒检查；接受后会创建新的可恢复文档版本。',
        apply: () => {
          void acceptSectionRewrite(
            projectId,
            active.section_key,
            candidate.candidate_body_ir,
            savedImmediatelyBeforeRewrite ? undefined : active.updated_at,
          ).then((accepted) => {
            toast({ title: `已创建文稿 v${accepted.document_version}`, description: '单章重写已接受并切换为当前文稿。', variant: 'success' });
            reloadSections();
            previewModule.reload();
            qualityModule.reload();
            reloadProject();
          }).catch((error) => {
            toast({ title: '重写候选未能接受', description: describeError(error), variant: 'error' });
          });
        },
      });
    } catch (error) {
      toast({ title: '本章重写失败', description: describeError(error), variant: 'error' });
    } finally {
      setRewriteLoading(false);
    }
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

  const rebuildFromLatestEvidence = async () => {
    try {
      const started = await rebuildDraft(projectId);
      startJob(started.data, '后端不可用：无法按最新证据重建草稿');
    } catch (err) {
      toast({
        title: '草稿重建未能启动',
        description: describeError(err),
        variant: 'error',
      });
    }
  };

  const runQuality = async (
    selectedProfile: QualityProfile,
    reviewStyle: ReviewStyle,
  ) => {
    setQualityProfile(selectedProfile);
    try {
      const started = await generateQuality(projectId, {
        quality_profile: selectedProfile,
        review_style: reviewStyle,
      });
      startJob(started.data, '后端不可用：无法生成质量报告');
    } catch (err) {
      toast({ title: '质量报告未能启动', description: describeError(err), variant: 'error' });
    }
  };

  const runQualityRepair = async (
    selectedProfile: QualityProfile,
    reviewStyle: ReviewStyle,
  ) => {
    setQualityProfile(selectedProfile);
    try {
      const started = await repairQuality(projectId, {
        quality_profile: selectedProfile,
        review_style: reviewStyle,
      });
      startJob(started.data, '后端不可用：无法启动质量修复');
    } catch (err) {
      toast({ title: '自动修复未能启动', description: describeError(err), variant: 'error' });
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

  /** AI 生图必须先过确认框；确定性图表没有外部调用也不计费，直接生成。 */
  const requestVisualGeneration = (visual: VisualAsset) => {
    if (visual.kind === 'ai_image') setConfirmingVisual(visual);
    else void visualsModule.generate(visual);
  };

  /**
   * 批准并插入。
   *
   * 三件事必须按这个顺序做，少一步就会把刚插入的图弄丢：
   *
   * 1. 先保存当前章节的未保存草稿——否则它就是一份「插图之前」的正文；
   * 2. 带上章节的 `updated_at` 做乐观并发，服务端已变化时返回 409 而不是覆盖；
   * 3. 成功后**用服务端的新 IR 重写本地草稿**。这一步最容易被漏掉：草稿恢复
   *    effect 以 `active.updated_at` 为依赖，批准后的 reload 必然触发它；
   *    localStorage 里那份不含 FigureBlock 的草稿会被当成「未保存修改」回填，
   *    下一次保存就把图删了。
   */
  const approveVisualIntoPaper = async (
    visual: VisualAsset,
    sectionKey: string,
    blockIndex: number,
  ) => {
    if (dirty && active?.section_key === sectionKey) {
      const saved = await save();
      if (!saved) {
        toast({
          title: '未插入图片',
          description: '本节还有未保存的修改且保存失败。先处理保存冲突，再批准插图。',
          variant: 'error',
        });
        return;
      }
    }
    const section = sections.find((item) => item.section_key === sectionKey);
    const body = section?.body_ir as SectionIR | undefined;
    try {
      await visualsModule.approve(
        visual,
        sectionKey,
        Math.max(0, Math.min(blockIndex, body?.blocks?.length ?? 0)),
        section?.updated_at ?? null,
      );
      // 本地草稿必须先失效，再让 reload 触发草稿恢复 effect。
      window.localStorage.removeItem(draftKey(projectId, sectionKey));
      toast({ title: '图片已插入论文', variant: 'success' });
      reloadSections();
      previewModule.reload();
      setEditorRevision((revision) => revision + 1);
    } catch (err) {
      toast({ title: '图片未能插入', description: describeError(err), variant: 'error' });
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

  /** 全文图编号由所有章节按顺序算出——单节编辑器自己看不到全文顺序。 */
  const figureNumbering = React.useMemo(() => buildFigureNumbering(sections), [sections]);

  /** 正文里点「替换」→ 打开共用编辑器，改完自动生成新版本。 */
  const replaceFigure = React.useCallback(
    (assetRef: string) => {
      const target = visuals.find((visual) => visual.asset_ref === assetRef);
      if (target) setEditingVisual(target);
      else
        toast({
          title: '找不到这张图的视觉资产',
          description: '它可能已被删除。可以直接删掉正文里的图块。',
          variant: 'error',
        });
    },
    [visuals, toast],
  );

  // 入口只表达“是否有待处理项”。不同作用域的数量不可相加，否则会让用户误以为
  // 右栏里存在一个统一的问题清单。
  const hasValidationIssues =
    (audit?.hallucinated_cite_keys?.length ?? 0) > 0 ||
    Boolean(paperType === 'original' && numlint && !numlint.consistent) ||
    (quality?.blockers?.length ?? 0) > 0;
  const qualityRunning = Boolean(
    runningStage &&
      ['quality', 'repair_search', 'repair_ingest', 'quality_repair', 'quality_recheck'].includes(
        runningStage,
      ),
  );

  // 抽屉与右栏共用同一个面板实例定义。
  const validationPanel = (
    <React.Suspense fallback={<DeferredModuleFallback className="h-72" />}>
      <ValidationPanel
        activeSection={active}
        activeDraft={draft}
        audit={audit}
        quality={quality}
        numlint={numlint}
        auditState={auditModule}
        qualityState={qualityModule}
        numlintState={numlintModule}
        showNumbers={paperType === 'original'}
        onGenerateQuality={runQuality}
        onRepairQuality={runQualityRepair}
        busy={busy}
        qualityRunning={qualityRunning}
        onJumpToSection={(key) => {
          requestSwitch(key);
          setPanelOpen(false);
        }}
      />
    </React.Suspense>
  );

  return (
    <div className="space-y-4">
      <WorkbenchHeader
        title="写作工作台"
        description={`共 ${sections.length} 节 · ${totalWords.toLocaleString()} 字`}
        actions={
          <>
            <SaveStatus dirty={dirty} saving={saving} savedAt={savedAt} />
            <Button
              variant="outline"
              onClick={() => void save()}
              disabled={!draft || !dirty}
              loading={saving}
              loadingLabel="保存中…"
            >
              <Save className="h-4 w-4" />
              保存本节
            </Button>
            <ActionMenu
              label="文稿操作"
              items={[
                {
                  label: '放弃草稿',
                  icon: X,
                  description: '恢复本节上次保存的内容',
                  onSelect: discardDraft,
                  disabled: !dirty,
                },
                {
                  label: '重写本章',
                  icon: PenLine,
                  description: '先生成候选并预览，接受后创建新文档版本',
                  onSelect: () => setRewriteOpen(true),
                  disabled: !active || busy,
                },
                {
                  label: '重新生成全文',
                  icon: PenLine,
                  description: '生成新文档版本并切换，旧版本与导出文件保留',
                  onSelect: () => setConfirmRegenerate(true),
                  disabled: !projectId || busy,
                },
              ]}
            />
          </>
        }
      />

      {outlineModule.data?.stale && (
        <Callout variant="warning" className="flex flex-wrap items-center justify-between gap-3">
          <span>证据矩阵与综合判定已更新，当前正文仍基于旧大纲。</span>
          <Button
            size="sm"
            disabled={busy || dirty}
            onClick={() => void rebuildFromLatestEvidence()}
          >
            <RefreshCw className="h-4 w-4" />
            按最新证据重建草稿
          </Button>
        </Callout>
      )}

      {/* 视觉列表失败不能让正文编辑器消失：它只是正文旁边的一条辅助信息。 */}
      {!focusMode && (
        <>
          <ModuleError
            label="视觉建议"
            error={visualsModule.error}
            onRetry={visualsModule.reload}
          />
          {!visualsModule.error && (
            <VisualSuggestionsPanel
              projectId={projectId}
              controller={visualsModule}
              sections={sections}
              activeSectionKey={activeKey}
              busy={busy}
              onSuggest={startVisualSuggestions}
              onApprove={approveVisualIntoPaper}
              onEdit={setEditingVisual}
              onRequestGenerate={requestVisualGeneration}
            />
          )}
        </>
      )}

      <LoadState
        loading={sectionsLoading}
        error={sectionsError}
        onRetry={reloadSections}
        skeletonClassName="h-96"
      >
        {sections.length === 0 ? (
          <EmptyState projectId={projectId} writing={writing} />
        ) : (
          <div
            className={cn(
              'grid gap-6 xl:grid-cols-[12rem,minmax(0,1fr)]',
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
                    <TabsTrigger value="preview">全文预览{dirty ? '（上次保存）' : ''}</TabsTrigger>
                  </TabsList>
                </Tabs>
                <div className="flex items-center gap-2">
                  {view === 'editor' && draft && (
                    <span className="text-xs tabular-nums text-muted-foreground">
                      本节 {liveWords.toLocaleString()} 字
                    </span>
                  )}
                  {/* 窄屏（<2xl）没有右栏的位置，校验面板收进抽屉。 */}
                  {!focusMode && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setPanelOpen(true)}
                      className="2xl:hidden"
                    >
                      <ShieldCheck className="h-3.5 w-3.5" /> 校验
                      {hasValidationIssues && (
                        <span
                          className="ml-1 h-2 w-2 rounded-full bg-warning"
                          aria-label="有待处理的校验问题"
                        />
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
                  <React.Suspense fallback={<DeferredModuleFallback className="h-[32rem]" />}>
                    <SectionEditor
                      key={`${active?.section_key}:${editorRevision}`}
                      section={draft}
                      whitelist={whitelist}
                      onChange={setDraft}
                      softChecks={quality?.soft_check ?? []}
                      onRefine={onRefine}
                      refining={refining}
                      visuals={visuals}
                      numbering={figureNumbering}
                      onReplaceFigure={replaceFigure}
                      evidenceUnits={evidenceUnits}
                    />
                  </React.Suspense>
                </div>
              )}

              {view === 'preview' && (
                <div className="mx-auto w-full max-w-[78ch] space-y-3">
                  {dirty && (
                    <Callout variant="warning" className="flex flex-wrap items-center justify-between gap-3">
                      <span>预览基于上次保存版本，不包含当前章节的未保存修改。</span>
                      <Button size="sm" loading={saving} onClick={async () => {
                        if (await save()) previewModule.reload();
                      }}>
                        <Save className="h-3.5 w-3.5" /> 保存并刷新预览
                      </Button>
                    </Callout>
                  )}
                  <ModuleError label="全文预览" error={previewModule.error} onRetry={previewModule.reload} />
                  {!previewModule.error && (
                    <React.Suspense fallback={<DeferredModuleFallback className="h-[32rem]" />}>
                      <MarkdownPreview markdown={preview?.markdown ?? ''} />
                    </React.Suspense>
                  )}
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
        description="当前章节检查与整篇投稿质量"
        className="max-w-md"
      >
        {validationPanel}
      </Drawer>

      <Dialog
        open={pendingSwitch !== null}
        onClose={() => setPendingSwitch(null)}
        title="当前章节有未保存修改"
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
        open={rewriteOpen}
        onClose={() => setRewriteOpen(false)}
        title="重写当前章节"
        description="说明希望如何改写。系统只生成候选，引用、数字、证据与图表必须保持守恒；预览并接受后才会创建新文档版本。"
        footer={
          <>
            <Button variant="outline" onClick={() => setRewriteOpen(false)}>取消</Button>
            <Button loading={rewriteLoading} disabled={rewriteInstruction.trim().length < 3} onClick={createRewriteCandidate}>生成重写候选</Button>
          </>
        }
      >
        <label className="space-y-1.5 text-sm">
          <span className="font-medium">重写要求</span>
          <textarea
            value={rewriteInstruction}
            onChange={(event) => setRewriteInstruction(event.target.value)}
            rows={5}
            placeholder="例如：强化方法与结果之间的逻辑衔接，压缩背景描述，保留所有数字与引用。"
            className="w-full rounded-md border bg-background px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
      </Dialog>

      <Dialog
        open={confirmRegenerate}
        onClose={() => setConfirmRegenerate(false)}
        title="生成新的全文版本？"
        description="系统会按当前大纲生成新的文档版本并切换为当前文稿；旧文档版本和已导出文件仍会保留。"
        footer={
          <>
            <Button variant="outline" onClick={() => setConfirmRegenerate(false)}>
              取消
            </Button>
            <Button variant="destructive" onClick={startWriting}>
              生成新版本并切换
            </Button>
          </>
        }
      >
        <Callout variant="warning" className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning-strong" />
          <div className="space-y-1">
            <p>
              <span className="font-medium">全部 {sections.length} 节</span>
              会按当前大纲生成到新的文档版本，完成后切换为当前文稿。
            </p>
            <p className="text-xs text-muted-foreground">
              已导出的产物不受影响，仍可在导出中心下载。
            </p>
          </div>
        </Callout>
      </Dialog>

      <React.Suspense fallback={null}>
        {diff && (
          <DiffPreviewDialog
            open
            original={diff.original}
            refined={diff.refined}
            note={diff.note}
            onCancel={() => setDiff(null)}
            onAccept={() => {
              diff.apply(diff.refined);
              setDiff(null);
            }}
          />
        )}

        {/* 与视觉工作台同一个编辑器：在写作台点「调整」直接就地改，不用跳页。 */}
        {editingVisual && (
          <VisualEditorDrawer
            open
            onClose={() => setEditingVisual(null)}
            projectId={projectId}
            visual={editingVisual}
            assets={[]}
            aiGenerationAvailable={visualsModule.aiGenerationAvailable}
            targetSectionKey={activeKey}
            capabilities={visualsModule.capabilities}
            onSubmit={(payload) => {
              void visualsModule.createRevision(editingVisual, payload);
            }}
          />
        )}

        {confirmingVisual && (
          <AIGenerationDialog
            visual={confirmingVisual}
            capabilities={visualsModule.capabilities}
            onCancel={() => setConfirmingVisual(null)}
            onConfirm={(visual) => {
              setConfirmingVisual(null);
              void visualsModule.generate(visual);
            }}
          />
        )}
      </React.Suspense>

      <WorkbenchFooterNav current="write" />
    </div>
  );
}

/**
 * 写作台的视觉入口——**轻量**。
 *
 * 写作台不再维护第二套视觉业务组件：卡片、编辑器、动作全部来自
 * `components/visuals/`，与视觉工作台是同一份实现。这里只回答两个问题：
 * 「还有几条待处理」和「当前这一节有什么建议」，其余交给工作台。
 */
function VisualSuggestionsPanel({
  projectId,
  controller,
  sections,
  activeSectionKey,
  busy,
  onSuggest,
  onApprove,
  onEdit,
  onRequestGenerate,
}: {
  projectId: string;
  controller: VisualsController;
  sections: PaperSection[];
  activeSectionKey: string | null;
  busy: boolean;
  onSuggest: () => void;
  onApprove: (visual: VisualAsset, sectionKey: string, blockIndex: number) => void;
  onEdit: (visual: VisualAsset) => void;
  onRequestGenerate: (visual: VisualAsset) => void;
}) {
  const groups = React.useMemo(
    () => groupVisualsByLineage(controller.visuals),
    [controller.visuals],
  );
  const pending = groups.filter((group) => group.latest.review_status === 'pending');
  // 当前这一节的建议排在前面：写到哪一节，就先看哪一节的图。
  const relevant = pending
    .filter((group) => group.latest.target_section_key === activeSectionKey)
    .slice(0, 2);
  const shown = relevant.length > 0 ? relevant : pending.slice(0, 2);
  const [expanded, setExpanded] = React.useState(false);

  return (
    <section className="rounded-lg border bg-card/60 p-3" aria-label="视觉建议">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <button
          type="button"
          onClick={() => pending.length > 0 && setExpanded((value) => !value)}
          aria-expanded={pending.length > 0 ? expanded : undefined}
          className="min-w-0 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <p className="flex items-center gap-2 text-sm font-medium">
            <ImageIcon className="h-4 w-4 text-primary" /> 视觉建议
            {pending.length > 0 && <Badge variant="secondary">{pending.length}</Badge>}
            {pending.length > 0 && <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', expanded && 'rotate-180')} />}
          </p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {pending.length > 0 ? `${pending.length} 条待处理；展开后可逐条查看。` : '暂无待处理建议，可随时重新分析全文。'}
          </p>
        </button>
        <div className="flex items-center gap-2">
          <Link
            href={projectHref(projectId, 'visuals')}
            className={buttonVariants({ variant: 'ghost', size: 'sm' })}
          >
            打开视觉工作台 →
          </Link>
          <Button variant="outline" size="sm" onClick={onSuggest} disabled={busy}>
            {busy ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            分析全文
          </Button>
        </div>
      </div>
      {expanded && shown.length > 0 && (
        <div className="mt-3 grid gap-3 xl:grid-cols-2">
          {shown.map((group) => (
            <VisualCard
              key={group.rootId}
              group={group}
              projectId={projectId}
              sections={sections}
              activeSectionKey={activeSectionKey}
              job={controller.jobFor(group.latest.id)}
              aiGenerationAvailable={controller.aiGenerationAvailable}
              aiJobRunning={controller.aiJobRunning}
                  deterministicSlotsFull={controller.deterministicSlotsFull}
              compact
              actions={{
                onEdit,
                onGenerate: onRequestGenerate,
                onRevision: (visual) => void controller.createRevision(visual),
                onApprove,
                onReject: (visual) => void controller.reject(visual),
              }}
            />
          ))}
        </div>
      )}
    </section>
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
        className="flex min-h-11 w-full items-center justify-between gap-2 rounded-md border px-3 py-2 text-body transition-colors hover:bg-accent/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring xl:hidden"
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
          'space-y-0.5 xl:sticky xl:top-4 xl:block xl:max-h-[calc(100vh-2rem)] xl:self-start xl:overflow-y-auto scrollbar-thin',
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
