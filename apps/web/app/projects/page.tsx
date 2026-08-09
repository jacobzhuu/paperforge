'use client';

import * as React from 'react';
import Link from 'next/link';
import {
  Plus,
  BookOpen,
  ChevronDown,
  FlaskConical,
  Library,
  FileText,
  Search,
  Trash2,
  Undo2,
  X,
} from 'lucide-react';
import { PageContainer } from '@/components/layout/page-container';
import { PageHeader } from '@/components/layout/page-header';
import { Button, buttonVariants } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { StatusBadge, type StatusState } from '@/components/ui/status';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { EmptyState } from '@/components/ui/empty-state';
import { DataSourceBanner } from '@/components/data-source-banner';
import { LoadState } from '@/components/layout/load-state';
import { Dialog } from '@/components/ui/dialog';
import { useToast } from '@/components/ui/toast';
import { deleteProject, listDeletedProjects, listProjects, restoreProject } from '@/lib/api';
import { describeError } from '@/lib/errors';
import type { DataSource, PaperType, Project } from '@/lib/types';
import { LANGUAGE_LABEL, PAPER_TYPE_LABEL, WRITING_MODE_LABEL } from '@/lib/labels';
import { cn, formatDate } from '@/lib/utils';

type TypeFilter = 'all' | PaperType;
type SortKey = 'updated' | 'created' | 'title';

/**
 * 项目进度档位。
 *
 * **不读 `project.status`**：`set_project_status` 在全仓库没有调用方，
 * 所有项目永远是 'draft'——那个徽章此前对 46 篇文献 7 章正文的项目
 * 也显示「草稿」。这里改用后端确实会更新的两个计数推断。
 */
function progressOf(p: Project): {
  label: string;
  state: StatusState;
} {
  const state = p.attention_summary?.readiness.state;
  if (state === 'pass') return { label: '投稿检查通过', state: 'done' };
  if (state === 'fail') return { label: '需要处理', state: 'failed' };
  if (state === 'warn' || state === 'stale') return { label: state === 'stale' ? '检查已过期' : '有提醒', state: 'degraded' };
  if (state === 'unknown') return { label: '状态未知', state: 'idle' };
  const lib = p.library_count ?? 0;
  const sec = p.section_count ?? 0;
  if (sec > 0) return { label: '有正文', state: 'done' };
  if (lib > 0) return { label: '文献已入库', state: 'running' };
  return { label: '空项目', state: 'idle' };
}

export default function ProjectsPage() {
  const [projects, setProjects] = React.useState<Project[]>([]);
  const [source, setSource] = React.useState<DataSource>('live');
  const [note, setNote] = React.useState<string | undefined>();
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [reloadToken, setReloadToken] = React.useState(0);

  const [query, setQuery] = React.useState('');
  const [typeFilter, setTypeFilter] = React.useState<TypeFilter>('all');
  const [sortKey, setSortKey] = React.useState<SortKey>('updated');

  const [deleted, setDeleted] = React.useState<Project[]>([]);
  const [pendingDelete, setPendingDelete] = React.useState<Project | null>(null);
  const [deleting, setDeleting] = React.useState(false);
  const { toast } = useToast();

  React.useEffect(() => {
    let alive = true;
    listProjects()
      .then((res) => {
        if (!alive) return;
        setProjects(res.data);
        setSource(res.source);
        setNote(res.note);
        setLoadError(null);
        setLoading(false);
      })
      .catch((err) => {
        // 没有这个 catch 时，一次 500 会让三张骨架卡永远地 pulse 下去。
        if (!alive) return;
        setLoadError(describeError(err));
        setLoading(false);
      });
    // 回收站单独拉：它为空是常态，失败也不该影响主列表。
    listDeletedProjects()
      .then((res) => {
        if (alive) setDeleted(res.data);
      })
      .catch(() => {
        /* 回收站拉不到就不显示，主列表照常 */
      });
    return () => {
      alive = false;
    };
  }, [reloadToken]);

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await deleteProject(pendingDelete.id);
      setPendingDelete(null);
      setReloadToken((t) => t + 1);
      toast({
        title: '项目已移入回收站',
        description: `「${pendingDelete.title}」可从回收站恢复；正在运行的任务已一并取消。`,
      });
    } catch (err) {
      toast({ title: '删除失败', description: describeError(err), variant: 'error' });
    } finally {
      setDeleting(false);
    }
  };

  const restore = async (project: Project) => {
    try {
      await restoreProject(project.id);
      setReloadToken((t) => t + 1);
      toast({ title: '项目已恢复', description: `「${project.title}」已回到项目列表。` });
    } catch (err) {
      toast({ title: '恢复失败', description: describeError(err), variant: 'error' });
    }
  };

  const visible = React.useMemo(() => {
    let list = projects;
    if (typeFilter !== 'all') list = list.filter((p) => p.paper_type === typeFilter);
    const q = query.trim().toLowerCase();
    if (q) {
      list = list.filter(
        (p) => p.title.toLowerCase().includes(q) || (p.topic ?? '').toLowerCase().includes(q),
      );
    }
    return [...list].sort((a, b) => {
      if (sortKey === 'title') return a.title.localeCompare(b.title, 'zh');
      const key = sortKey === 'created' ? 'created_at' : 'updated_at';
      return Date.parse(b[key] ?? '') - Date.parse(a[key] ?? '');
    });
  }, [projects, query, typeFilter, sortKey]);

  const filtering = query.trim() !== '' || typeFilter !== 'all';

  return (
    <PageContainer>
      <div className="space-y-6">
        <PageHeader
          title="项目"
          description="综述与研究型论文项目工作区"
          actions={
            <Link href="/" className={buttonVariants()}>
              <Plus className="h-4 w-4" /> 新建论文
            </Link>
          }
        />

        <DataSourceBanner source={source} note={note} />

        <LoadState
          loading={false}
          error={loadError}
          onRetry={() => {
            setLoading(true);
            setReloadToken((t) => t + 1);
          }}
        >
          {loading ? (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {[0, 1, 2].map((i) => (
                <div key={i} className="space-y-4 rounded-lg border p-5" aria-hidden="true">
                  <div className="flex justify-between">
                    <Skeleton className="h-4 w-20" />
                    <Skeleton className="h-5 w-14" />
                  </div>
                  <Skeleton className="h-6 w-4/5" />
                  <Skeleton className="h-4 w-full" />
                  <div className="flex gap-4 border-t pt-3">
                    <Skeleton className="h-4 w-20" />
                    <Skeleton className="h-4 w-16" />
                  </div>
                </div>
              ))}
            </div>
          ) : projects.length === 0 ? (
            <FirstRunState />
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <div className="relative min-w-56 flex-1">
                  <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="搜索题目或主题"
                    aria-label="搜索项目"
                    className="pl-8"
                  />
                </div>
                <Select
                  value={typeFilter}
                  onChange={(e) => setTypeFilter(e.target.value as TypeFilter)}
                  aria-label="按类型筛选"
                  className="w-36"
                >
                  <option value="all">全部类型</option>
                  <option value="review">综述论文</option>
                  <option value="original">研究型论文</option>
                </Select>
                <Select
                  value={sortKey}
                  onChange={(e) => setSortKey(e.target.value as SortKey)}
                  aria-label="排序方式"
                  className="w-36"
                >
                  <option value="updated">最近更新</option>
                  <option value="created">最近创建</option>
                  <option value="title">按题目</option>
                </Select>
                {filtering && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setQuery('');
                      setTypeFilter('all');
                    }}
                  >
                    <X className="h-3.5 w-3.5" /> 清除筛选
                  </Button>
                )}
              </div>

              {visible.length === 0 ? (
                <EmptyState
                  title="没有匹配的项目"
                  description="调整关键词或清除类型筛选后再试。"
                  action={
                    <Button
                      variant="outline"
                      onClick={() => {
                        setQuery('');
                        setTypeFilter('all');
                      }}
                    >
                      清除筛选
                    </Button>
                  }
                />
              ) : (
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {visible.map((p) => (
                    <ProjectCard key={p.id} project={p} onDelete={() => setPendingDelete(p)} />
                  ))}
                </div>
              )}
            </>
          )}
        </LoadState>

        {!loading && <RecycleBin projects={deleted} onRestore={restore} />}

        <Dialog
          open={pendingDelete !== null}
          onClose={() => setPendingDelete(null)}
          title={`删除「${pendingDelete?.title ?? ''}」？`}
          description="项目会被移入回收站，文献、大纲与正文都保留，随时可以恢复。项目里正在运行的任务会一并取消。保留期过后由管理员彻底清理。"
          footer={
            <>
              <Button variant="outline" onClick={() => setPendingDelete(null)}>
                取消
              </Button>
              <Button variant="destructive" disabled={deleting} onClick={confirmDelete}>
                {deleting ? '删除中…' : '移入回收站'}
              </Button>
            </>
          }
        />
      </div>
    </PageContainer>
  );
}

function ProjectCard({ project, onDelete }: { project: Project; onDelete: () => void }) {
  const Icon = project.paper_type === 'review' ? BookOpen : FlaskConical;
  const progress = progressOf(project);
  // topic 与 title 常常一字不差（实测 14 个项目中多数如此），重复渲染只是噪音。
  const showTopic = project.topic && project.topic.trim() !== project.title.trim();
  const summary = project.attention_summary;

  return (
    /*
     * stretched link：整张卡片可点，但卡片上还要放一个删除菜单。此前整张卡是
     * 一个 <Link>，任何塞进去的按钮都会先触发导航。改成链接绝对定位铺满卡片、
     * 交互控件用 relative z-10 浮在它上面——菜单可点，其余地方照旧整片可点。
     */
    <Card className="group relative h-full transition-shadow hover:shadow-md">
      <Link
        href={`/projects/${project.id}`}
        aria-label={project.title}
        className="absolute inset-0 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      />
      <CardHeader className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Icon className="h-4 w-4" />
            {PAPER_TYPE_LABEL[project.paper_type]}
          </div>
          <div className="flex items-center gap-1">
            <StatusBadge state={progress.state} label={progress.label} />
            {/* 只有「删除」一个动作，收进菜单反而多一次点击。相对定位 + z-10
                让它浮在铺满卡片的链接之上，点它不会顺带跳转。 */}
            <button
              type="button"
              onClick={onDelete}
              aria-label={`删除项目：${project.title}`}
              className="relative z-10 flex h-11 w-11 items-center justify-center rounded text-muted-foreground opacity-0 transition-opacity hover:bg-accent hover:text-destructive focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring group-hover:opacity-100"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
        {/* 论文题目是卡片上最重要的东西，给它 serif + 更大的字号。 */}
        <CardTitle className="line-clamp-2 font-serif text-lg leading-snug">
          {project.title}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {showTopic && (
          <p className="line-clamp-1 text-xs text-muted-foreground">{project.topic}</p>
        )}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span className="inline-flex items-center gap-1">
            <Library className="h-3.5 w-3.5" /> {project.library_count ?? 0} 篇文献
          </span>
          <span className="inline-flex items-center gap-1">
            <FileText className="h-3.5 w-3.5" /> {project.section_count ?? 0} 章节
          </span>
        </div>
        {summary && (
          <div className="rounded-md bg-muted/50 px-3 py-2 text-xs">
            {summary.active_job ? (
              <p className="font-medium text-primary">正在运行：{summary.active_job.stage}</p>
            ) : (
              <p className="font-medium">下一步：{summary.readiness.nextAction}</p>
            )}
            <p className="mt-1 text-muted-foreground">
              {summary.manuscript.wordCount != null ? `${summary.manuscript.wordCount.toLocaleString()} 字正文` : '尚无正文'}
              {summary.readiness.attentionCount != null && summary.readiness.attentionCount > 0
                ? ` · ${summary.readiness.attentionCount} 项需关注`
                : ''}
              {summary.latest_pdf ? ` · 最新 PDF${summary.latest_pdf.stale ? ' 已过期' : ''}` : ''}
            </p>
          </div>
        )}
        <div className="flex items-center justify-between border-t pt-3 text-xs text-muted-foreground">
          <span>
            {LANGUAGE_LABEL[project.language]} · {WRITING_MODE_LABEL[project.writing_mode]}
          </span>
          <span>{formatDate(project.updated_at)}</span>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * 回收站。删除是软删除，这里是唯一的反悔入口——没有它，「可恢复」等于不存在。
 * 保留期过后由 `paperforge-admin purge-projects` 真删并回收对象存储。
 */
function RecycleBin({
  projects,
  onRestore,
}: {
  projects: Project[];
  onRestore: (project: Project) => void;
}) {
  const [open, setOpen] = React.useState(false);
  if (projects.length === 0) return null;

  return (
    <div className="rounded-lg border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-4 py-3 text-sm text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Trash2 className="h-4 w-4" />
        回收站（{projects.length}）
        <ChevronDown className={cn('ml-auto h-4 w-4 transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <ul className="divide-y border-t">
          {projects.map((p) => (
            <li key={p.id} className="flex items-center gap-3 px-4 py-2.5 text-sm">
              <span className="min-w-0 flex-1">
                <span className="block truncate">{p.title}</span>
                <span className="text-xs text-muted-foreground">
                  删除于 {formatDate(p.deleted_at)}
                </span>
              </span>
              <Button variant="outline" size="sm" onClick={() => onRestore(p)}>
                <Undo2 className="h-3.5 w-3.5" /> 恢复
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** 首次运行：与其给一个空框，不如按两条管线各给一个入口。 */
function FirstRunState() {
  return (
    <div className="space-y-4 rounded-lg border border-dashed p-8">
      <div className="text-center">
        <p className="font-medium">还没有项目</p>
        <p className="mt-1 text-sm text-muted-foreground">
          PaperForge 从题目或素材出发，产出引用真实的论文初稿。选一条管线开始。
        </p>
      </div>
      <div className="mx-auto grid max-w-2xl gap-3 sm:grid-cols-2">
        <StarterLink
          icon={<BookOpen className="h-5 w-5" />}
          title="综述论文"
          desc="我有一个题目：检索 → 文献库 → 大纲 → 分节写作 → LaTeX/PDF"
          href="/?type=review"
        />
        <StarterLink
          icon={<FlaskConical className="h-5 w-5" />}
          title="研究型论文"
          desc="我有实验素材：素材摄取 → 相关工作 → IMRaD → 数字一致性 → LaTeX/PDF"
          href="/?type=original"
        />
      </div>
    </div>
  );
}

function StarterLink({
  icon,
  title,
  desc,
  href,
}: {
  icon: React.ReactNode;
  title: string;
  desc: string;
  href: string;
}) {
  return (
    <Link
      href={href}
      className={cn(
        'flex flex-col gap-2 rounded-lg border p-4 text-left transition-colors',
        'hover:border-primary/50 hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
      )}
    >
      <div className="flex items-center gap-2">
        {icon}
        <span className="font-medium">{title}</span>
      </div>
      <p className="text-xs leading-relaxed text-muted-foreground">{desc}</p>
    </Link>
  );
}
