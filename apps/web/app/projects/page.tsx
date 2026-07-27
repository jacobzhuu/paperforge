'use client';

import * as React from 'react';
import Link from 'next/link';
import { Plus, BookOpen, FlaskConical, Library, FileText, Search, X } from 'lucide-react';
import { PageContainer } from '@/components/layout/page-container';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { DataSourceBanner } from '@/components/data-source-banner';
import { LoadState } from '@/components/layout/load-state';
import { NewProjectWizard } from '@/components/projects/new-project-wizard';
import { listProjects } from '@/lib/api';
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
  variant: 'muted' | 'default' | 'success';
} {
  const lib = p.library_count ?? 0;
  const sec = p.section_count ?? 0;
  if (sec > 0) return { label: '有正文', variant: 'success' };
  if (lib > 0) return { label: '文献已入库', variant: 'default' };
  return { label: '空项目', variant: 'muted' };
}

export default function ProjectsPage() {
  const [projects, setProjects] = React.useState<Project[]>([]);
  const [source, setSource] = React.useState<DataSource>('live');
  const [note, setNote] = React.useState<string | undefined>();
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [wizardOpen, setWizardOpen] = React.useState(false);
  const [wizardType, setWizardType] = React.useState<PaperType | undefined>();
  const [reloadToken, setReloadToken] = React.useState(0);

  const [query, setQuery] = React.useState('');
  const [typeFilter, setTypeFilter] = React.useState<TypeFilter>('all');
  const [sortKey, setSortKey] = React.useState<SortKey>('updated');

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
    return () => {
      alive = false;
    };
  }, [reloadToken]);

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

  const openWizard = (type?: PaperType) => {
    setWizardType(type);
    setWizardOpen(true);
  };

  return (
    <PageContainer>
      <div className="space-y-6">
        <PageHeader
          title="项目"
          description="综述与研究型论文项目工作区"
          actions={
            <Button onClick={() => openWizard()}>
              <Plus className="h-4 w-4" /> 新建项目
            </Button>
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
                <Skeleton key={i} className="h-40" />
              ))}
            </div>
          ) : projects.length === 0 ? (
            <FirstRunState onCreate={openWizard} />
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
                <div className="rounded-xl border border-dashed py-16 text-center text-sm text-muted-foreground">
                  没有匹配的项目。
                </div>
              ) : (
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {visible.map((p) => (
                    <ProjectCard key={p.id} project={p} />
                  ))}
                </div>
              )}
            </>
          )}
        </LoadState>

        <NewProjectWizard
          open={wizardOpen}
          initialPaperType={wizardType}
          onClose={() => setWizardOpen(false)}
        />
      </div>
    </PageContainer>
  );
}

function ProjectCard({ project }: { project: Project }) {
  const Icon = project.paper_type === 'review' ? BookOpen : FlaskConical;
  const progress = progressOf(project);
  // topic 与 title 常常一字不差（实测 14 个项目中多数如此），重复渲染只是噪音。
  const showTopic = project.topic && project.topic.trim() !== project.title.trim();

  return (
    <Link
      href={`/projects/${project.id}`}
      className="group rounded-xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
    >
      <Card className="h-full transition-shadow group-hover:shadow-md">
        <CardHeader className="space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Icon className="h-4 w-4" />
              {PAPER_TYPE_LABEL[project.paper_type]}
            </div>
            <Badge variant={progress.variant}>{progress.label}</Badge>
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
          <div className="flex items-center justify-between border-t pt-3 text-xs text-muted-foreground">
            <span>
              {LANGUAGE_LABEL[project.language]} · {WRITING_MODE_LABEL[project.writing_mode]}
            </span>
            <span>{formatDate(project.updated_at)}</span>
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

/** 首次运行：与其给一个空框，不如按两条管线各给一个入口。 */
function FirstRunState({ onCreate }: { onCreate: (type?: PaperType) => void }) {
  return (
    <div className="space-y-4 rounded-xl border border-dashed p-8">
      <div className="text-center">
        <p className="font-medium">还没有项目</p>
        <p className="mt-1 text-sm text-muted-foreground">
          PaperForge 从题目或素材出发，产出引用真实的论文初稿。选一条管线开始。
        </p>
      </div>
      <div className="mx-auto grid max-w-2xl gap-3 sm:grid-cols-2">
        <StarterCard
          icon={<BookOpen className="h-5 w-5" />}
          title="综述论文"
          desc="我有一个题目：检索 → 文献库 → 大纲 → 分节写作 → LaTeX/PDF"
          onClick={() => onCreate('review')}
        />
        <StarterCard
          icon={<FlaskConical className="h-5 w-5" />}
          title="研究型论文"
          desc="我有实验素材：素材摄取 → 相关工作 → IMRaD → 数字一致性 → LaTeX/PDF"
          onClick={() => onCreate('original')}
        />
      </div>
    </div>
  );
}

function StarterCard({
  icon,
  title,
  desc,
  onClick,
}: {
  icon: React.ReactNode;
  title: string;
  desc: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
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
    </button>
  );
}
