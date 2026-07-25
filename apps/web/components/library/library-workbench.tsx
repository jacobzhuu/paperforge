'use client';

import * as React from 'react';
import { useSearchParams } from 'next/navigation';
import {
  ArrowUpDown,
  Download,
  FileText,
  Loader2,
  Network,
  Search,
  Sparkles,
} from 'lucide-react';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Progress } from '@/components/ui/progress';
import { DataSourceBanner } from '@/components/data-source-banner';
import { CardDrawer } from './card-drawer';
import { ImportDialog } from './import-dialog';
import { SearchStats } from './search-stats';
import { ProviderFilter } from './provider-filter';
import {
  generateCards,
  getProject,
  getWhitelist,
  importReferences,
  listLibrary,
  listSearchRuns,
  selectEntries,
  startIngest,
  startSearch,
  startSnowball,
  subscribeJobEvents,
} from '@/lib/api';
import type {
  DataSource,
  Job,
  JobEvent,
  LibraryEntry,
  LibraryEntryStatus,
  Project,
  SearchRun,
} from '@/lib/types';
import type { SourceCapabilityId } from '@/lib/sourceCapabilities';
import { ADDED_VIA_LABEL } from '@/lib/labels';
import { cn } from '@/lib/utils';

type StatusFilter = 'all' | LibraryEntryStatus;

const STATUS_TABS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'candidate', label: '候选' },
  { value: 'selected', label: '已入库' },
  { value: 'excluded', label: '已排除' },
];

export function LibraryWorkbench() {
  const params = useSearchParams();
  const projectId = params.get('project') ?? 'demo-review-01';

  const [project, setProject] = React.useState<Project | undefined>();
  const [entries, setEntries] = React.useState<LibraryEntry[]>([]);
  const [runs, setRuns] = React.useState<SearchRun[]>([]);
  const [source, setSource] = React.useState<DataSource>('live');
  const [note, setNote] = React.useState<string | undefined>();
  const [loading, setLoading] = React.useState(true);

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
  const [whitelist, setWhitelist] = React.useState<string[]>([]);
  const [job, setJob] = React.useState<Job | null>(null);
  const [jobStage, setJobStage] = React.useState<string | null>(null);
  const [jobMessage, setJobMessage] = React.useState<string | null>(null);

  const reload = React.useCallback(async () => {
    const [proj, lib, sr, wl] = await Promise.all([
      getProject(projectId),
      listLibrary(projectId),
      listSearchRuns(projectId),
      getWhitelist(projectId),
    ]);
    setProject(proj.data);
    setEntries(lib.data);
    setRuns(sr.data);
    setWhitelist(wl.data);
    setSource(lib.source);
    setNote(lib.note);
    setLoading(false);
  }, [projectId]);

  React.useEffect(() => {
    let alive = true;
    reload().catch(() => {
      if (alive) setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [reload]);

  /** 订阅任务进度；任务结束后刷新数据。 */
  const track = React.useCallback(
    (started: Job | undefined, fallbackMessage: string) => {
      if (!started) {
        setJobMessage(fallbackMessage);
        return;
      }
      setJob(started);
      setJobStage(started.stage ?? '排队中');
      setJobMessage(null);
      const stop = subscribeJobEvents(projectId, started.id, {
        onEvent: (event: JobEvent) => {
          setJobStage(event.stage ?? event.type);
          setJob((prev) =>
            prev
              ? {
                  ...prev,
                  progress: event.progress ?? prev.progress,
                  status: event.status ?? prev.status,
                  stage: event.stage ?? prev.stage,
                }
              : prev,
          );
          if (event.type === 'search.completed' || event.type === 'cards.completed') {
            void reload();
          }
        },
        onClose: () => {
          void reload();
          setJob(null);
          setJobStage(null);
        },
      });
      return stop;
    },
    [projectId, reload],
  );

  const toggleSelect = async (target: LibraryEntry) => {
    const nextStatus: LibraryEntryStatus = target.status === 'selected' ? 'candidate' : 'selected';
    // 乐观更新，随后以服务端返回为准（bibtex_key 由服务端在入库时分配）。
    setEntries((prev) =>
      prev.map((e) => (e.id === target.id ? { ...e, status: nextStatus } : e)),
    );
    setActive((a) => (a && a.id === target.id ? { ...a, status: nextStatus } : a));
    const result = await selectEntries(projectId, [target.work.id], nextStatus);
    if (result.source === 'live') void reload();
  };

  const handleImport = async (kind: 'doi' | 'bibtex', payload: string) => {
    const body =
      kind === 'doi'
        ? { dois: payload.split('\n').map((line) => line.trim()).filter(Boolean) }
        : { bibtex: payload };
    const started = await importReferences(projectId, body);
    track(started.data, '后端不可用：导入需要 R1 反查核验，未写入任何文献');
  };

  const triggerSearch = async () => {
    const started = await startSearch(projectId, { regenerateScope: !project?.topic });
    track(started.data, '后端不可用：无法触发检索');
  };

  const triggerCards = async () => {
    const started = await generateCards(projectId);
    track(started.data, '后端不可用：无法生成卡片');
  };

  const triggerSnowball = async () => {
    const started = await startSnowball(projectId, 'both');
    track(started.data, '后端不可用：无法执行雪球扩展');
  };

  const triggerIngest = async () => {
    const started = await startIngest(projectId);
    track(started.data, '后端不可用：无法获取 OA 全文');
  };

  const filtered = React.useMemo(() => {
    let list = entries;
    if (statusFilter !== 'all') list = list.filter((e) => e.status === statusFilter);
    if (query.trim()) {
      const q = query.toLowerCase();
      list = list.filter(
        (e) =>
          e.work.canonical_title.toLowerCase().includes(q) ||
          e.work.authors.some((a) => a.toLowerCase().includes(q)),
      );
    }
    return [...list].sort((a, b) =>
      sortDesc ? b.relevance_score - a.relevance_score : a.relevance_score - b.relevance_score,
    );
  }, [entries, statusFilter, query, sortDesc]);

  const selectedCount = entries.filter((e) => e.status === 'selected').length;

  return (
    <div className="space-y-6">
      <PageHeader
        title="文献工作台"
        description={project ? project.title : '检索、筛选、雪球扩展与入库核验'}
        actions={
          <>
            <Button variant="outline" onClick={triggerSnowball} disabled={!!job}>
              <Network className="h-4 w-4" /> 雪球扩展
            </Button>
            <Button variant="outline" onClick={triggerIngest} disabled={!!job}>
              <FileText className="h-4 w-4" /> 获取 OA 全文
            </Button>
            <Button variant="outline" onClick={triggerCards} disabled={!!job}>
              <Sparkles className="h-4 w-4" /> 生成卡片
            </Button>
            <Button variant="outline" onClick={() => setImportOpen(true)} disabled={!!job}>
              <Download className="h-4 w-4" /> 导入
            </Button>
            <Button onClick={triggerSearch} disabled={!!job}>
              {job ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
              触发检索
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
                <span className="font-medium">{STAGE_LABEL[jobStage ?? ''] ?? jobStage ?? '进行中'}</span>
                <span className="text-muted-foreground">{Math.round((job.progress ?? 0) * 100)}%</span>
              </div>
              <Progress value={Math.round((job.progress ?? 0) * 100)} className="mt-1.5" />
            </div>
          </CardContent>
        </Card>
      )}

      {jobMessage && (
        <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning-foreground">
          {jobMessage}
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[1fr,20rem]">
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Tabs value={statusFilter} onValueChange={(v) => setStatusFilter(v as StatusFilter)}>
              <TabsList>
                {STATUS_TABS.map((t) => (
                  <TabsTrigger key={t.value} value={t.value}>
                    {t.label}
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">已入库 {selectedCount}</span>
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="过滤标题 / 作者"
                className="h-8 w-48"
              />
            </div>
          </div>

          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10"></TableHead>
                  <TableHead>文献</TableHead>
                  <TableHead
                    className="w-24 cursor-pointer select-none"
                    onClick={() => setSortDesc((s) => !s)}
                  >
                    <span className="inline-flex items-center gap-1">
                      相关性 <ArrowUpDown className="h-3 w-3" />
                    </span>
                  </TableHead>
                  <TableHead className="w-24">来源</TableHead>
                  <TableHead className="w-20">状态</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {loading ? (
                  <TableRow>
                    <TableCell colSpan={5} className="py-10 text-center text-sm text-muted-foreground">
                      加载中…
                    </TableCell>
                  </TableRow>
                ) : filtered.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={5} className="py-10 text-center text-sm text-muted-foreground">
                      无匹配文献
                    </TableCell>
                  </TableRow>
                ) : (
                  filtered.map((e) => (
                    <TableRow
                      key={e.id}
                      data-state={e.status === 'selected' ? 'selected' : undefined}
                      className="cursor-pointer"
                      onClick={() => setActive(e)}
                    >
                      <TableCell onClick={(ev) => ev.stopPropagation()}>
                        <Checkbox
                          checked={e.status === 'selected'}
                          onCheckedChange={() => toggleSelect(e)}
                          aria-label="入库"
                        />
                      </TableCell>
                      <TableCell>
                        <div className="max-w-md">
                          <div className="flex items-center gap-2">
                            <span className="line-clamp-1 font-medium">{e.work.canonical_title}</span>
                            {e.work.is_retracted && (
                              <Badge variant="destructive" className="shrink-0">
                                撤稿
                              </Badge>
                            )}
                          </div>
                          <div className="line-clamp-1 text-xs text-muted-foreground">
                            {e.work.authors.join(', ')} · {e.work.publication_year ?? '—'}
                            {e.work.venue_name ? ` · ${e.work.venue_name}` : ''}
                          </div>
                        </div>
                      </TableCell>
                      <TableCell>
                        <RelevanceBar score={e.relevance_score} />
                      </TableCell>
                      <TableCell>
                        <span className="text-xs text-muted-foreground">
                          {ADDED_VIA_LABEL[e.added_via]}
                        </span>
                      </TableCell>
                      <TableCell>
                        <StatusChip status={e.status} verified={!!e.verified_at} />
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </Card>
        </div>

        <aside className="space-y-4">
          <ProviderFilter selected={capabilities} onChange={setCapabilities} />
          <SearchStats runs={runs} />
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">引用真实性</CardTitle>
            </CardHeader>
            <CardContent className="space-y-1.5 text-xs text-muted-foreground">
              <p>R1 入库核验：仅 selected + 已核验 + 有 key + 未撤稿进入写作白名单。</p>
              <p>R2 写作约束：cite-key 越权自动重写/移除。</p>
              <p>R3 参考文献确定性生成：key 入库时持久化，导出只消费。</p>
              <p className="border-t pt-1.5 text-foreground">
                当前写作白名单：<span className="tabular-nums">{whitelist.length}</span> 个 cite key
              </p>
            </CardContent>
          </Card>
        </aside>
      </div>

      <CardDrawer
        entry={active}
        open={!!active}
        onClose={() => setActive(null)}
        onToggleSelect={toggleSelect}
      />
      <ImportDialog open={importOpen} onClose={() => setImportOpen(false)} onImport={handleImport} />
    </div>
  );
}

const STAGE_LABEL: Record<string, string> = {
  scope: '生成研究范围',
  search: '五源检索与去重',
  curate: '分配引用 key',
  cards: '抽取文献卡片',
  import: '反查核验导入文献',
  import_doi: '反查核验 DOI',
  import_bibtex: '反查核验 BibTeX 条目',
  snowball: '引文雪球扩展',
  ingest: '获取并解析 OA 全文',
  done: '完成',
};

function RelevanceBar({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-10 overflow-hidden rounded-full bg-secondary">
        <div
          className={cn('h-full rounded-full', pct >= 80 ? 'bg-success' : pct >= 60 ? 'bg-primary' : 'bg-warning')}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-xs tabular-nums text-muted-foreground">{pct}</span>
    </div>
  );
}

function StatusChip({ status, verified }: { status: LibraryEntryStatus; verified: boolean }) {
  if (status === 'selected')
    return <Badge variant={verified ? 'success' : 'warning'}>{verified ? '已入库' : '待核验'}</Badge>;
  if (status === 'excluded') return <Badge variant="muted">已排除</Badge>;
  return <Badge variant="outline">候选</Badge>;
}
