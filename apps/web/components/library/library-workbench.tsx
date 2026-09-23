'use client';

import * as React from 'react';
import {
  ArrowUpDown,
  FileCode2,
  FileText,
  Link,
  Loader2,
  Network,
  Rocket,
  Search,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  Upload,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ActionMenu } from '@/components/ui/action-menu';
import { Dialog } from '@/components/ui/dialog';
import { Drawer } from '@/components/ui/drawer';
import { useToast } from '@/components/ui/toast';
import { LoadState } from '@/components/layout/load-state';
import { WorkbenchHeader } from '@/components/project/workbench-header';
import { WorkbenchFooterNav } from '@/components/project/workbench-footer-nav';
import {
  useJobFinished,
  useProjectActions,
  useProjectData,
} from '@/components/project/project-context';
import { PdfMatchDialog, PdfUploadQueue } from './pdf-upload-queue';
import { SearchStats } from './search-stats';
import { WebResearchPanel } from './web-research-panel';
import { ProviderFilter } from './provider-filter';
import { EntryList } from './entry-list';
import { UtilizationSummary, entryRole } from './utilization-status';
import {
  confirmPdfUpload,
  deleteLibraryEntry,
  generateAll,
  generateCards,
  importReferences,
  listLibrary,
  listPdfUploads,
  listSearchRuns,
  listSections,
  rejectPdfUpload,
  retryPdfUpload,
  selectEntries,
  startIngest,
  startSearch,
  startSnowball,
  updateLibraryEntry,
  uploadLibraryPdf,
} from '@/lib/api';
import type {
  Job,
  LibraryEntry,
  LibraryEntryStatus,
  LibraryPdfUpload,
  LibraryPdfUploadResult,
  LiteratureRole,
  SearchRun,
} from '@/lib/types';
import { buildCiteKeyUsage, sectionsCiting, type CiteKeyUsage } from '@/lib/citation-usage';
import type { SourceCapabilityId } from '@/lib/sourceCapabilities';
import { providersFromCapabilities } from '@/lib/sourceCapabilities';
import { LIBRARY_ACTION } from '@/lib/labels';
import { describeError } from '@/lib/errors';

/** 详情与导入流程只在用户主动打开后下载，主列表首屏不携带这些表单代码。 */
const CardDrawer = React.lazy(() =>
  import('./card-drawer').then((module) => ({ default: module.CardDrawer })),
);
const ImportDialog = React.lazy(() =>
  import('./import-dialog').then((module) => ({ default: module.ImportDialog })),
);
const PdfUploadDialog = React.lazy(() =>
  import('./pdf-upload-dialog').then((module) => ({ default: module.PdfUploadDialog })),
);

type StatusFilter = 'all' | 'cited' | LibraryEntryStatus;

const STATUS_TABS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'cited', label: '正文引用' },
  { value: 'candidate', label: LIBRARY_ACTION.candidate },
  { value: 'candidate_uncertain', label: LIBRARY_ACTION.uncertain },
  { value: 'selected', label: LIBRARY_ACTION.selected },
  { value: 'excluded', label: LIBRARY_ACTION.excluded },
];

export function LibraryWorkbench() {
  const { projectId, project, reload: reloadProject } = useProjectData();
  const { busy, startJob } = useProjectActions();
  const { toast } = useToast();

  const [entries, setEntries] = React.useState<LibraryEntry[]>([]);
  const [runs, setRuns] = React.useState<SearchRun[]>([]);
  const [pdfUploads, setPdfUploads] = React.useState<LibraryPdfUpload[]>([]);
  /** cite-key → 引用它的章节，用于每行的「被引用于 …」（ui-design.md §3.6）。 */
  const [usage, setUsage] = React.useState<CiteKeyUsage>(() => new Map());
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);

  const [statusFilter, setStatusFilter] = React.useState<StatusFilter>('all');
  const [query, setQuery] = React.useState('');
  const deferredQuery = React.useDeferredValue(query);
  const [sortDesc, setSortDesc] = React.useState(true);
  const [mobileFiltersOpen, setMobileFiltersOpen] = React.useState(false);
  const [capabilities, setCapabilities] = React.useState<SourceCapabilityId[]>([
    'scholarly_indexes',
    'cs_preprints',
    'oa_fulltext',
  ]);

  const [active, setActive] = React.useState<LibraryEntry | null>(null);
  const [importOpen, setImportOpen] = React.useState(false);
  const [importTab, setImportTab] = React.useState<'doi' | 'bibtex'>('doi');
  const [pdfUploadOpen, setPdfUploadOpen] = React.useState(false);
  const [activePdfUpload, setActivePdfUpload] = React.useState<LibraryPdfUpload | null>(null);
  const [pdfBusyId, setPdfBusyId] = React.useState<string | null>(null);
  const [checked, setChecked] = React.useState<Set<string>>(new Set());
  const [lastIndex, setLastIndex] = React.useState<number | null>(null);
  const [bulkBusy, setBulkBusy] = React.useState(false);
  const [confirmDelete, setConfirmDelete] = React.useState(false);

  const reload = React.useCallback(async () => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    const [lib, sr, sections, uploads] = await Promise.all([
      listLibrary(projectId),
      // 检索统计属于右侧 Inspector；它失败时主列表仍应可用。
      listSearchRuns(projectId).catch(() => null),
      // 正文还没生成时这里是空数组，Provenance 会自动退回排序理由/来源，
      // 所以不需要为「还没写正文」单独分支。章节反查只是增强信息：即使它
      // 暂时 500，也不能把检索与分诊主界面一起打进错误态。
      listSections(projectId).catch(() => null),
      // PDF 队列是增强模块：暂时拉不到不能拖垮文献列表。
      listPdfUploads(projectId).catch(() => null),
    ]);
    setEntries(lib.data);
    setActive((current) =>
      current ? lib.data.find((entry) => entry.id === current.id) ?? null : null,
    );
    setRuns(sr?.data ?? []);
    setUsage(sections ? buildCiteKeyUsage(sections.data) : new Map());
    if (uploads) setPdfUploads(uploads.data);
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

  const hasProcessingPdf = pdfUploads.some((upload) =>
    ['matching', 'parsing', 'extracting'].includes(upload.status),
  );
  React.useEffect(() => {
    if (!projectId || !hasProcessingPdf) return;
    let timer: number | null = null;
    let inFlight = false;

    const poll = () => {
      if (document.hidden || inFlight) return;
      inFlight = true;
      void listPdfUploads(projectId)
        .then(({ data }) => {
          setPdfUploads(data);
          setActivePdfUpload((current) =>
            current ? data.find((upload) => upload.id === current.id) ?? current : null,
          );
        })
        .catch(() => {
          /* 下一轮继续；队列轮询失败不影响主列表。 */
        })
        .finally(() => {
          inFlight = false;
        });
    };
    const schedule = () => {
      if (timer !== null) window.clearInterval(timer);
      timer = document.hidden ? null : window.setInterval(poll, 3000);
    };
    const onVisibilityChange = () => {
      if (!document.hidden) poll();
      schedule();
    };

    document.addEventListener('visibilitychange', onVisibilityChange);
    schedule();
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange);
      if (timer !== null) window.clearInterval(timer);
    };
  }, [projectId, hasProcessingPdf]);

  const filtered = React.useMemo(() => {
    let list = entries;
    if (statusFilter === 'cited') {
      list = list.filter((entry) => sectionsCiting(usage, entry.bibtex_key).length > 0);
    } else if (statusFilter !== 'all') {
      list = list.filter((entry) => entry.status === statusFilter);
    }
    if (deferredQuery.trim()) {
      const q = deferredQuery.toLowerCase();
      list = list.filter(
        (e) =>
          e.work.canonical_title.toLowerCase().includes(q) ||
          e.work.authors.some((a) => a.toLowerCase().includes(q)) ||
          e.work.venue_name?.toLowerCase().includes(q) ||
          sectionsCiting(usage, e.bibtex_key).some((title) => title.toLowerCase().includes(q)),
      );
    }
    return [...list].sort((a, b) =>
      sortDesc ? b.relevance_score - a.relevance_score : a.relevance_score - b.relevance_score,
    );
  }, [entries, statusFilter, deferredQuery, sortDesc, usage]);

  const selectedCount = React.useMemo(
    () => entries.filter((e) => e.status === 'selected').length,
    [entries],
  );
  const citedCount = React.useMemo(
    () => entries.filter((entry) => sectionsCiting(usage, entry.bibtex_key).length > 0).length,
    [entries, usage],
  );
  const activePdfMatchedEntry = activePdfUpload?.matched_work
    ? entries.find((entry) => entry.work.id === activePdfUpload.matched_work?.id)
    : undefined;

  /** 局部更新，不再整表重拉。 */
  const applyStatus = (ids: Set<string>, status: LibraryEntryStatus) => {
    setEntries((prev) => prev.map((e) => (ids.has(e.id) ? { ...e, status } : e)));
    setActive((a) => (a && ids.has(a.id) ? { ...a, status } : a));
  };

  /** 行首复选框只服务批量操作；Shift 连选也只修改这份临时状态。 */
  const toggleBulk = (entry: LibraryEntry, index: number, shiftKey: boolean) => {
    setChecked((previous) => {
      const next = new Set(previous);
      const shouldAdd = !previous.has(entry.id);
      if (shiftKey && lastIndex !== null) {
        const [from, to] = lastIndex < index ? [lastIndex, index] : [index, lastIndex];
        for (const item of filtered.slice(from, to + 1)) {
          if (shouldAdd) next.add(item.id);
          else next.delete(item.id);
        }
      } else if (shouldAdd) {
        next.add(entry.id);
      } else {
        next.delete(entry.id);
      }
      return next;
    });
    setLastIndex(index);
  };

  /** “纳入写作”是持久状态，不再借用批量复选框的视觉或键盘语义。 */
  const toggleIncluded = async (entry: LibraryEntry) => {
    const previousStatus = entry.status;
    const nextStatus: LibraryEntryStatus = previousStatus === 'selected' ? 'candidate' : 'selected';
    const ids = new Set([entry.id]);
    applyStatus(ids, nextStatus);
    try {
      const result = await selectEntries(projectId, [entry.work.id], nextStatus);
      const updated = result.data.find((item) => item.id === entry.id);
      if (updated) {
        setEntries((previous) =>
          previous.map((item) => (item.id === updated.id ? updated : item)),
        );
        setActive((current) => (current?.id === updated.id ? updated : current));
      }
      // 白名单变了（R1），项目层需要知道。
      reloadProject();
    } catch (err) {
      // 这个勾选决定 R1 引用白名单，失败却不回滚等于让界面对文献库状态撒谎。
      applyStatus(ids, previousStatus);
      toast({ title: '纳入状态未能保存', description: describeError(err), variant: 'error' });
    }
  };

  const toggleRole = async (entry: LibraryEntry) => {
    const previousRole = entryRole(entry);
    const nextRole: LiteratureRole = previousRole === 'core' ? 'general' : 'core';
    const applyRole = (role: LiteratureRole) => {
      setEntries((previous) =>
        previous.map((item) =>
          item.id === entry.id ? { ...item, literature_role: role } : item,
        ),
      );
      setActive((current) =>
        current?.id === entry.id ? { ...current, literature_role: role } : current,
      );
    };
    applyRole(nextRole);
    try {
      const updated = await updateLibraryEntry(projectId, entry.id, {
        literature_role: nextRole,
      });
      setEntries((previous) =>
        previous.map((item) => (item.id === updated.id ? updated : item)),
      );
      setActive((current) => (current?.id === updated.id ? updated : current));
    } catch (error) {
      applyRole(previousRole);
      toast({
        title: '核心文献标记未能保存',
        description: describeError(error),
        variant: 'error',
      });
    }
  };

  /** 批量入库/排除：`selectEntries` 本来就收数组，一次请求即可。 */
  const bulkStatus = async (status: LibraryEntryStatus) => {
    const targets = filtered.filter((e) => checked.has(e.id));
    if (targets.length === 0) return;
    setBulkBusy(true);
    const previous = new Map(targets.map((e) => [e.id, e.status]));
    applyStatus(new Set(targets.map((e) => e.id)), status);
    try {
      await selectEntries(
        projectId,
        targets.map((e) => e.work.id),
        status,
      );
      toast({
        title: `${targets.length} 条已${status === 'selected' ? '纳入写作' : status === 'excluded' ? '排除' : '设为候选'}`,
        variant: 'success',
      });
      setChecked(new Set());
      reloadProject();
      runReload();
    } catch (err) {
      setEntries((prev) =>
        prev.map((e) => (previous.has(e.id) ? { ...e, status: previous.get(e.id)! } : e)),
      );
      toast({ title: '批量操作失败', description: describeError(err), variant: 'error' });
    } finally {
      setBulkBusy(false);
    }
  };

  /** 后端 DELETE /library/entries/{id} 一直存在，此前界面没有入口。 */
  const bulkDelete = async () => {
    setConfirmDelete(false);
    const targets = filtered.filter((e) => checked.has(e.id));
    if (targets.length === 0) return;
    setBulkBusy(true);
    let ok = 0;
    const failures: string[] = [];
    for (const entry of targets) {
      try {
        await deleteLibraryEntry(projectId, entry.id);
        ok += 1;
      } catch (err) {
        failures.push(describeError(err));
      }
    }
    setBulkBusy(false);
    setChecked(new Set());
    if (ok > 0) toast({ title: `已移除 ${ok} 条文献`, variant: 'success' });
    if (failures.length > 0) {
      toast({ title: `${failures.length} 条未能移除`, description: failures[0], variant: 'error' });
    }
    runReload();
    reloadProject();
  };

  const runAction = async (
    action: () => Promise<{ data: Job | undefined }>,
    fallbackMessage: string,
    failTitle: string,
  ): Promise<boolean> => {
    try {
      const started = await action();
      startJob(started.data, fallbackMessage);
      return Boolean(started.data);
    } catch (err) {
      toast({ title: failTitle, description: describeError(err), variant: 'error' });
      return false;
    }
  };

  const handleImport = async (kind: 'doi' | 'bibtex', payload: string) => {
    const body =
      kind === 'doi'
        ? { dois: payload.split('\n').map((line) => line.trim()).filter(Boolean) }
        : { bibtex: payload };
    return runAction(
      () => importReferences(projectId, body),
      '后端不可用：导入需要 R1 反查核验，未写入任何文献',
      '导入未能启动',
    );
  };

  const triggerSearch = () =>
    runAction(
      () =>
        startSearch(projectId, {
          providers: providersFromCapabilities(capabilities),
          regenerateScope: !project?.topic,
        }),
      '后端不可用：无法触发检索',
      '检索未能启动',
    );

  const upsertPdfUpload = (upload: LibraryPdfUpload) => {
    setPdfUploads((previous) => {
      const exists = previous.some((item) => item.id === upload.id);
      return exists
        ? previous.map((item) => (item.id === upload.id ? upload : item))
        : [upload, ...previous];
    });
  };

  const handlePdfUploaded = (result: LibraryPdfUploadResult) => {
    upsertPdfUpload(result.upload);
    if (result.job) {
      startJob(result.job, '后端不可用：PDF 元数据匹配未能启动');
    }
    if (result.upload.status === 'needs_confirmation') {
      setActivePdfUpload(result.upload);
    }
  };

  const handlePdfConfirm = async (upload: LibraryPdfUpload, role: LiteratureRole) => {
    setPdfBusyId(upload.id);
    try {
      const result = await confirmPdfUpload(projectId, upload.id, role);
      upsertPdfUpload(result.upload);
      if (result.job) {
        startJob(result.job, '后端不可用：PDF 全文解析未能启动');
      }
      setActivePdfUpload(null);
      toast({
        title: '匹配已确认',
        description: '私有原文已绑定，正在解析全文并提取卡片与证据。',
        variant: 'success',
      });
      runReload();
      reloadProject();
    } catch (error) {
      toast({
        title: '匹配确认失败',
        description: describeError(error),
        variant: 'error',
      });
    } finally {
      setPdfBusyId(null);
    }
  };

  const handlePdfReject = async (upload: LibraryPdfUpload) => {
    setPdfBusyId(upload.id);
    try {
      await rejectPdfUpload(projectId, upload.id);
      setPdfUploads((previous) => previous.filter((item) => item.id !== upload.id));
      setActivePdfUpload((current) => (current?.id === upload.id ? null : current));
      toast({ title: '已拒绝并移除该 PDF', variant: 'success' });
    } catch (error) {
      toast({
        title: 'PDF 未能移除',
        description: describeError(error),
        variant: 'error',
      });
    } finally {
      setPdfBusyId(null);
    }
  };

  const handlePdfRetry = async (upload: LibraryPdfUpload) => {
    const rematching = upload.status === 'match_failed';
    setPdfBusyId(upload.id);
    try {
      const result = await retryPdfUpload(projectId, upload.id);
      upsertPdfUpload(result.upload);
      if (result.job) {
        startJob(
          result.job,
          rematching
            ? '后端不可用：PDF 重新匹配未能启动'
            : '后端不可用：PDF 重新解析未能启动',
        );
      }
      toast({
        title: rematching ? '已重新开始匹配' : '已重新开始解析',
        variant: 'success',
      });
    } catch (error) {
      toast({
        title: rematching ? 'PDF 重新匹配失败' : 'PDF 重新解析失败',
        description: describeError(error),
        variant: 'error',
      });
    } finally {
      setPdfBusyId(null);
    }
  };

  const checkedInView = filtered.filter((e) => checked.has(e.id)).length;
  const allChecked = filtered.length > 0 && checkedInView === filtered.length;

  return (
    <div className="space-y-4">
      <WebResearchPanel />
      <WorkbenchHeader
        title={
          <span className="inline-flex items-baseline gap-2">
            文献
            <span className="font-sans text-sm font-normal tabular-nums text-muted-foreground">
              {entries.length}
            </span>
          </span>
        }
        description={
          citedCount > 0
            ? `${citedCount} 篇已进入正文；打开文献即可查看证据与详细元数据。`
            : '从入选理由开始理解研究语境；正文生成后，这里会显示每篇文献支撑的章节。'
        }
        actions={
          <>
            <ActionMenu
              label={LIBRARY_ACTION.add}
              disabled={!projectId}
              items={[
                {
                  label: LIBRARY_ACTION.uploadPdf,
                  icon: Upload,
                  description: '识别元数据、确认匹配后私有保存原文',
                  onSelect: () => setPdfUploadOpen(true),
                },
                {
                  label: LIBRARY_ACTION.importDoi,
                  icon: Link,
                  description: '每行一个 DOI，经 Crossref/OpenAlex 核验',
                  disabled: busy,
                  onSelect: () => {
                    setImportTab('doi');
                    setImportOpen(true);
                  },
                },
                {
                  label: LIBRARY_ACTION.importBibtex,
                  icon: FileCode2,
                  description: '粘贴 BibTeX，反查真实 scholarly_work',
                  disabled: busy,
                  onSelect: () => {
                    setImportTab('bibtex');
                    setImportOpen(true);
                  },
                },
                {
                  label: LIBRARY_ACTION.search,
                  icon: Search,
                  description: '按右侧选定的检索源添加候选文献',
                  disabled: busy,
                  onSelect: () => void triggerSearch(),
                },
              ]}
            />
            <ActionMenu
              label="处理文献"
              disabled={!projectId || busy}
              items={[
                {
                  label: LIBRARY_ACTION.snowball,
                  icon: Network,
                  description: '沿引用与被引关系扩展候选文献',
                  onSelect: () =>
                    runAction(
                      () => startSnowball(projectId, 'both'),
                      '后端不可用：无法执行雪球扩展',
                      '雪球扩展未能启动',
                    ),
                },
                {
                  label: LIBRARY_ACTION.fulltext,
                  icon: FileText,
                  description: '抓取开放获取全文并解析分块',
                  onSelect: () =>
                    runAction(
                      () => startIngest(projectId),
                      '后端不可用：无法获取 OA 全文',
                      'OA 全文获取未能启动',
                    ),
                },
                {
                  label: LIBRARY_ACTION.cards,
                  icon: Sparkles,
                  description: '从全文抽取可写作的文献卡片',
                  onSelect: () =>
                    runAction(
                      () => generateCards(projectId),
                      '后端不可用：无法生成卡片',
                      '卡片生成未能启动',
                    ),
                },
                {
                  label: LIBRARY_ACTION.runAll,
                  icon: Rocket,
                  description: '检索 → 大纲 → 写作 → 编译，失败降级不阻断',
                  onSelect: () =>
                    runAction(
                      () => generateAll(projectId),
                      '后端不可用：无法启动全管线',
                      '全管线未能启动',
                    ),
                },
              ]}
            />
          </>
        }
      />

      <PdfUploadQueue
        uploads={pdfUploads}
        busyId={pdfBusyId}
        onConfirm={setActivePdfUpload}
        onRetry={(upload) => void handlePdfRetry(upload)}
        onReject={(upload) => void handlePdfReject(upload)}
      />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr),20rem]">
        <div className="min-w-0 space-y-3">
          {/* 筛选与批量操作常驻：此前它们随页面滚走，滚到第 200 行就再也够不到。 */}
          <Button
            variant="outline"
            className="w-full justify-between md:hidden"
            onClick={() => setMobileFiltersOpen((open) => !open)}
            aria-expanded={mobileFiltersOpen}
          >
            <span className="inline-flex items-center gap-2">
              <SlidersHorizontal /> 筛选与批量操作
            </span>
            <Badge variant="secondary">{filtered.length}</Badge>
          </Button>
          <Drawer
            open={mobileFiltersOpen}
            onClose={() => setMobileFiltersOpen(false)}
            title="筛选文献"
            description={`当前显示 ${filtered.length} / ${entries.length} 条`}
            className="md:hidden"
            footer={
              <Button className="w-full" onClick={() => setMobileFiltersOpen(false)}>
                查看结果
              </Button>
            }
          >
            <div className="space-y-6">
              <Tabs
                value={statusFilter}
                onValueChange={(value) => {
                  setStatusFilter(value as StatusFilter);
                  setLastIndex(null);
                }}
              >
                <TabsList className="w-full flex-wrap">
                  {STATUS_TABS.map((tab) => (
                    <TabsTrigger key={tab.value} value={tab.value}>
                      {tab.label}
                    </TabsTrigger>
                  ))}
                </TabsList>
              </Tabs>
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="过滤标题 / 作者 / 章节"
                aria-label="过滤文献"
              />
              <Button
                variant="outline"
                className="w-full justify-start"
                onClick={() => setSortDesc((value) => !value)}
              >
                <ArrowUpDown /> 相关性{sortDesc ? '降序' : '升序'}
              </Button>
              <label className="flex min-h-11 cursor-pointer items-center gap-3 text-body">
                <input
                  type="checkbox"
                  checked={allChecked}
                  onChange={(event) =>
                    setChecked(
                      event.target.checked ? new Set(filtered.map((entry) => entry.id)) : new Set(),
                    )
                  }
                  className="h-5 w-5 rounded border-input accent-primary"
                />
                全选当前筛选结果（{filtered.length}）
              </label>
            </div>
          </Drawer>
          <div
            className="hidden space-y-3 border-b pb-3 md:block"
          >
            <div className="flex flex-wrap items-center justify-between gap-3">
              <Tabs
                value={statusFilter}
                onValueChange={(v) => {
                  setStatusFilter(v as StatusFilter);
                  setLastIndex(null);
                }}
              >
                <TabsList>
                  {STATUS_TABS.map((t) => (
                    <TabsTrigger key={t.value} value={t.value}>
                      {t.label}
                    </TabsTrigger>
                  ))}
                </TabsList>
              </Tabs>
              <div className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground">
                  {LIBRARY_ACTION.selected} {selectedCount} / {entries.length}
                </span>
                <Button variant="ghost" size="sm" onClick={() => setSortDesc((s) => !s)}>
                  <ArrowUpDown className="h-3.5 w-3.5" />
                  相关性{sortDesc ? '降序' : '升序'}
                </Button>
                <Input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="过滤标题 / 作者 / 章节"
                  aria-label="过滤文献"
                  className="w-48 md:h-9"
                />
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2 border-t pt-2">
              <label className="flex cursor-pointer items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={allChecked}
                  onChange={(e) =>
                    setChecked(e.target.checked ? new Set(filtered.map((x) => x.id)) : new Set())
                  }
                  // 外层 <label> 已经给了可访问名，但显式写出来更稳妥，
                  // 也让审计工具不必依赖 label 包裹关系去推断。
                  aria-label={`全选当前筛选结果（${filtered.length} 条）`}
                  className="h-4 w-4 rounded border-input accent-primary"
                />
                全选当前筛选结果（{filtered.length}）
              </label>
              {checkedInView > 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <Badge variant="secondary">已选 {checkedInView}</Badge>
                  <Button
                    size="sm"
                    onClick={() => bulkStatus('selected')}
                    disabled={bulkBusy}
                  >
                    {bulkBusy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                    {LIBRARY_ACTION.bulkSelect}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => bulkStatus('excluded')}
                    disabled={bulkBusy}
                  >
                    {LIBRARY_ACTION.bulkExclude}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setConfirmDelete(true)}
                    disabled={bulkBusy}
                  >
                    <Trash2 className="h-3.5 w-3.5" /> {LIBRARY_ACTION.remove}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setChecked(new Set())}>
                    <X className="h-3.5 w-3.5" /> 取消选择
                  </Button>
                </div>
              )}
              <span className="ml-auto text-xs text-muted-foreground">
                按住 Shift 点选可连选一段
              </span>
            </div>
          </div>

          <LoadState loading={loading} error={loadError} onRetry={runReload} skeletonClassName="h-96">
            <EntryList
              entries={filtered}
              bulkSelectedIds={checked}
              activeId={active?.id}
              usage={usage}
              onToggleBulk={toggleBulk}
              onToggleIncluded={(entry) => void toggleIncluded(entry)}
              onOpen={setActive}
              emptyHint={
                entries.length === 0
                  ? '文献库为空。用右上角「添加文献」上传 PDF、输入 DOI/BibTeX 或搜索添加。'
                  : statusFilter === 'cited'
                    ? '正文尚未引用文献；生成或编辑章节后，这里会按引用位置自动汇总。'
                  : '当前筛选条件下无匹配文献'
              }
            />
          </LoadState>
          {checkedInView > 0 && (
            <div className="fixed inset-x-4 bottom-4 z-30 flex items-center gap-2 overflow-x-auto rounded-lg border bg-background/95 p-2 shadow-xl backdrop-blur md:hidden">
              <Badge variant="secondary" className="shrink-0">
                已选 {checkedInView}
              </Badge>
              <Button size="sm" className="shrink-0" onClick={() => bulkStatus('selected')} disabled={bulkBusy}>
                {bulkBusy && <Loader2 className="animate-spin" />}
                {LIBRARY_ACTION.bulkSelect}
              </Button>
              <Button size="sm" variant="outline" className="shrink-0" onClick={() => bulkStatus('excluded')} disabled={bulkBusy}>
                {LIBRARY_ACTION.bulkExclude}
              </Button>
              <Button size="sm" variant="ghost" className="shrink-0" onClick={() => setConfirmDelete(true)} disabled={bulkBusy}>
                <Trash2 /> {LIBRARY_ACTION.remove}
              </Button>
              <Button size="icon" variant="ghost" className="shrink-0" onClick={() => setChecked(new Set())} aria-label="取消选择">
                <X />
              </Button>
            </div>
          )}
        </div>

        <aside className="space-y-4 lg:sticky lg:top-4 lg:max-h-[calc(100vh-2rem)] lg:self-start lg:overflow-y-auto scrollbar-thin">
          <UtilizationSummary
            entries={entries}
            citedIn={(entry) => sectionsCiting(usage, entry.bibtex_key)}
          />
          <SearchStats runs={runs} />
          <ProviderFilter selected={capabilities} onChange={setCapabilities} />
          <details className="text-meta text-muted-foreground">
            <summary className="min-h-11 cursor-pointer py-3 underline-offset-4 hover:text-foreground hover:underline">
              了解引用真实性规则 →
            </summary>
            <div className="space-y-2 border-t pt-3">
              <p>入库核验：仅已选中、已核验、有引用键且未撤稿的文献进入写作白名单。</p>
              <p>写作约束：越权引用会被自动重写或移除。</p>
              <p>导出约束：引用键在入库时持久化，参考文献由程序确定性生成。</p>
            </div>
          </details>
        </aside>
      </div>

      <React.Suspense fallback={null}>
        {active && (
          <CardDrawer
            entry={active}
            open
            citedIn={sectionsCiting(usage, active.bibtex_key)}
            onClose={() => setActive(null)}
            onToggleSelect={(entry) => toggleIncluded(entry)}
            onToggleRole={(entry) => toggleRole(entry)}
          />
        )}
        {importOpen && (
          <ImportDialog
            open
            initialTab={importTab}
            onClose={() => setImportOpen(false)}
            onImport={handleImport}
          />
        )}
        {pdfUploadOpen && (
          <PdfUploadDialog
            open
            onClose={() => setPdfUploadOpen(false)}
            onUpload={(file) => uploadLibraryPdf(projectId, file)}
            onUploaded={handlePdfUploaded}
          />
        )}
        {activePdfUpload && (
          <PdfMatchDialog
            upload={activePdfUpload}
            initialRole={activePdfMatchedEntry ? entryRole(activePdfMatchedEntry) : 'general'}
            busy={pdfBusyId === activePdfUpload.id}
            onClose={() => setActivePdfUpload(null)}
            onConfirm={handlePdfConfirm}
            onReject={handlePdfReject}
          />
        )}
      </React.Suspense>

      <Dialog
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        title={`从文献库移除 ${checkedInView} 条？`}
        description="条目会被彻底删除，不只是取消入库。已经引用了这些文献的正文会在下次引用审计里被标出。"
        footer={
          <>
            <Button variant="outline" onClick={() => setConfirmDelete(false)}>
              取消
            </Button>
            <Button variant="destructive" onClick={bulkDelete}>
              移除
            </Button>
          </>
        }
      />

      <WorkbenchFooterNav current="library" />
    </div>
  );
}
