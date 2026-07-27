'use client';

import * as React from 'react';
import {
  ArrowUpDown,
  Download,
  FileText,
  Loader2,
  Network,
  Rocket,
  Search,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ActionMenu } from '@/components/ui/action-menu';
import { Dialog } from '@/components/ui/dialog';
import { useToast } from '@/components/ui/toast';
import { LoadState } from '@/components/layout/load-state';
import { WorkbenchHeader } from '@/components/project/workbench-header';
import { WorkbenchFooterNav } from '@/components/project/workbench-footer-nav';
import { useJobFinished, useProject } from '@/components/project/project-context';
import { CardDrawer } from './card-drawer';
import { ImportDialog } from './import-dialog';
import { SearchStats } from './search-stats';
import { ProviderFilter } from './provider-filter';
import { EntryList } from './entry-list';
import {
  deleteLibraryEntry,
  generateAll,
  generateCards,
  importReferences,
  listLibrary,
  listSearchRuns,
  listSections,
  selectEntries,
  startIngest,
  startSearch,
  startSnowball,
} from '@/lib/api';
import type { Job, LibraryEntry, LibraryEntryStatus, SearchRun } from '@/lib/types';
import { buildCiteKeyUsage, sectionsCiting, type CiteKeyUsage } from '@/lib/citation-usage';
import type { SourceCapabilityId } from '@/lib/sourceCapabilities';
import { providersFromCapabilities } from '@/lib/sourceCapabilities';
import { LIBRARY_ACTION } from '@/lib/labels';
import { describeError } from '@/lib/errors';

type StatusFilter = 'all' | 'cited' | LibraryEntryStatus;

const STATUS_TABS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'cited', label: '正文引用' },
  { value: 'candidate', label: LIBRARY_ACTION.candidate },
  { value: 'selected', label: LIBRARY_ACTION.selected },
  { value: 'excluded', label: LIBRARY_ACTION.excluded },
];

export function LibraryWorkbench() {
  const { projectId, project, busy, startJob, reload: reloadProject } = useProject();
  const { toast } = useToast();

  const [entries, setEntries] = React.useState<LibraryEntry[]>([]);
  const [runs, setRuns] = React.useState<SearchRun[]>([]);
  /** cite-key → 引用它的章节，用于每行的「被引用于 …」（ui-design.md §3.6）。 */
  const [usage, setUsage] = React.useState<CiteKeyUsage>(() => new Map());
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);

  const [statusFilter, setStatusFilter] = React.useState<StatusFilter>('all');
  const [query, setQuery] = React.useState('');
  const [sortDesc, setSortDesc] = React.useState(true);
  const [capabilities, setCapabilities] = React.useState<SourceCapabilityId[]>([
    'scholarly_indexes',
    'cs_preprints',
    'oa_fulltext',
  ]);

  const [active, setActive] = React.useState<LibraryEntry | null>(null);
  const [importOpen, setImportOpen] = React.useState(false);
  const [checked, setChecked] = React.useState<Set<string>>(new Set());
  const [lastIndex, setLastIndex] = React.useState<number | null>(null);
  const [bulkBusy, setBulkBusy] = React.useState(false);
  const [confirmDelete, setConfirmDelete] = React.useState(false);

  const reload = React.useCallback(async () => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    const [lib, sr, sections] = await Promise.all([
      listLibrary(projectId),
      // 检索统计属于右侧 Inspector；它失败时主列表仍应可用。
      listSearchRuns(projectId).catch(() => null),
      // 正文还没生成时这里是空数组，Provenance 会自动退回排序理由/来源，
      // 所以不需要为「还没写正文」单独分支。章节反查只是增强信息：即使它
      // 暂时 500，也不能把检索与分诊主界面一起打进错误态。
      listSections(projectId).catch(() => null),
    ]);
    setEntries(lib.data);
    setRuns(sr?.data ?? []);
    setUsage(sections ? buildCiteKeyUsage(sections.data) : new Map());
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

  const filtered = React.useMemo(() => {
    let list = entries;
    if (statusFilter === 'cited') {
      list = list.filter((entry) => sectionsCiting(usage, entry.bibtex_key).length > 0);
    } else if (statusFilter !== 'all') {
      list = list.filter((entry) => entry.status === statusFilter);
    }
    if (query.trim()) {
      const q = query.toLowerCase();
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
  }, [entries, statusFilter, query, sortDesc, usage]);

  const selectedCount = entries.filter((e) => e.status === 'selected').length;
  const citedCount = React.useMemo(
    () => entries.filter((entry) => sectionsCiting(usage, entry.bibtex_key).length > 0).length,
    [entries, usage],
  );

  /** 局部更新，不再整表重拉。 */
  const applyStatus = (ids: Set<string>, status: LibraryEntryStatus) => {
    setEntries((prev) => prev.map((e) => (ids.has(e.id) ? { ...e, status } : e)));
    setActive((a) => (a && ids.has(a.id) ? { ...a, status } : a));
  };

  /**
   * 单条勾选。
   *
   * 此前每点一次都要 `runReload()` 重拉 4 个端点（含 330 条列表）并整表重渲染。
   * 现在只做乐观更新 + 单次 POST，失败才回滚；白名单交给 provider 在任务/批量
   * 提交后统一刷新。
   */
  const toggleOne = async (entry: LibraryEntry, index: number, shiftKey: boolean) => {
    if (shiftKey && lastIndex !== null) {
      const [from, to] = lastIndex < index ? [lastIndex, index] : [index, lastIndex];
      const range = filtered.slice(from, to + 1).map((e) => e.id);
      setChecked((prev) => {
        const next = new Set(prev);
        range.forEach((id) => next.add(id));
        return next;
      });
      setLastIndex(index);
      return;
    }
    setLastIndex(index);

    const previousStatus = entry.status;
    const nextStatus: LibraryEntryStatus = previousStatus === 'selected' ? 'candidate' : 'selected';
    const ids = new Set([entry.id]);
    applyStatus(ids, nextStatus);
    try {
      await selectEntries(projectId, [entry.work.id], nextStatus);
      // 白名单变了（R1），项目层需要知道。
      reloadProject();
    } catch (err) {
      // 这个勾选决定 R1 引用白名单，失败却不回滚等于让界面对文献库状态撒谎。
      applyStatus(ids, previousStatus);
      toast({ title: '入库状态未能保存', description: describeError(err), variant: 'error' });
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
        title: `${targets.length} 条已${status === 'selected' ? '入库' : status === 'excluded' ? '排除' : '设为候选'}`,
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
  ) => {
    try {
      const started = await action();
      startJob(started.data, fallbackMessage);
    } catch (err) {
      toast({ title: failTitle, description: describeError(err), variant: 'error' });
    }
  };

  const handleImport = async (kind: 'doi' | 'bibtex', payload: string) => {
    const body =
      kind === 'doi'
        ? { dois: payload.split('\n').map((line) => line.trim()).filter(Boolean) }
        : { bibtex: payload };
    await runAction(
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

  const checkedInView = filtered.filter((e) => checked.has(e.id)).length;
  const allChecked = filtered.length > 0 && checkedInView === filtered.length;

  return (
    <div className="space-y-4">
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
                  label: LIBRARY_ACTION.import,
                  icon: Download,
                  description: '经 R1 反查核验后入库',
                  onSelect: () => setImportOpen(true),
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
            <Button onClick={triggerSearch} disabled={!projectId || busy}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
              {LIBRARY_ACTION.search}
            </Button>
          </>
        }
      />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr),20rem]">
        <div className="min-w-0 space-y-3">
          {/* 筛选与批量操作常驻：此前它们随页面滚走，滚到第 200 行就再也够不到。 */}
          <div className="space-y-3 border-b pb-3">
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
                  className="h-8 w-48"
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
              selectedIds={checked}
              activeId={active?.id}
              usage={usage}
              onToggleOne={toggleOne}
              onOpen={setActive}
              emptyHint={
                entries.length === 0
                  ? '文献库为空。点右上角「触发检索」从五个学术源检索并入库。'
                  : statusFilter === 'cited'
                    ? '正文尚未引用文献；生成或编辑章节后，这里会按引用位置自动汇总。'
                  : '当前筛选条件下无匹配文献'
              }
            />
          </LoadState>
        </div>

        <aside className="space-y-4 lg:sticky lg:top-4 lg:max-h-[calc(100vh-2rem)] lg:self-start lg:overflow-y-auto scrollbar-thin">
          <SearchStats runs={runs} />
          <ProviderFilter selected={capabilities} onChange={setCapabilities} />
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">引用真实性</CardTitle>
            </CardHeader>
            <CardContent className="space-y-1.5 text-xs text-muted-foreground">
              <p>R1 入库核验：仅 selected + 已核验 + 有 key + 未撤稿进入写作白名单。</p>
              <p>R2 写作约束：cite-key 越权自动重写/移除。</p>
              <p>R3 参考文献确定性生成：key 入库时持久化，导出只消费。</p>
            </CardContent>
          </Card>
        </aside>
      </div>

      <CardDrawer
        entry={active}
        open={!!active}
        citedIn={sectionsCiting(usage, active?.bibtex_key)}
        onClose={() => setActive(null)}
        onToggleSelect={(entry) => toggleOne(entry, -1, false)}
      />
      <ImportDialog open={importOpen} onClose={() => setImportOpen(false)} onImport={handleImport} />

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
