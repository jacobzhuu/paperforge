'use client';

import * as React from 'react';
import Link from 'next/link';
import { Plus, BookOpen, FlaskConical, Library, FileText } from 'lucide-react';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { DataSourceBanner } from '@/components/data-source-banner';
import { NewProjectWizard } from '@/components/projects/new-project-wizard';
import { listProjects } from '@/lib/api';
import type { DataSource, Project } from '@/lib/types';
import {
  LANGUAGE_LABEL,
  PAPER_TYPE_LABEL,
  STATUS_LABEL,
  STATUS_VARIANT,
  WRITING_MODE_LABEL,
} from '@/lib/labels';
import { cn, formatDate } from '@/lib/utils';

export default function ProjectsPage() {
  const [projects, setProjects] = React.useState<Project[]>([]);
  const [source, setSource] = React.useState<DataSource>('live');
  const [note, setNote] = React.useState<string | undefined>();
  const [loading, setLoading] = React.useState(true);
  const [wizardOpen, setWizardOpen] = React.useState(false);

  React.useEffect(() => {
    let alive = true;
    listProjects().then((res) => {
      if (!alive) return;
      setProjects(res.data);
      setSource(res.source);
      setNote(res.note);
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="space-y-6">
      <PageHeader
        title="项目"
        description="综述与研究型论文项目工作区"
        actions={
          <Button onClick={() => setWizardOpen(true)}>
            <Plus className="h-4 w-4" /> 新建项目
          </Button>
        }
      />

      <DataSourceBanner source={source} note={note} />

      {loading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-40 animate-pulse rounded-xl border bg-muted/40" />
          ))}
        </div>
      ) : projects.length === 0 ? (
        <EmptyState onCreate={() => setWizardOpen(true)} />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {projects.map((p) => (
            <ProjectCard key={p.id} project={p} />
          ))}
        </div>
      )}

      <NewProjectWizard open={wizardOpen} onClose={() => setWizardOpen(false)} />
    </div>
  );
}

function ProjectCard({ project }: { project: Project }) {
  const Icon = project.paper_type === 'review' ? BookOpen : FlaskConical;
  return (
    <Link href={`/library?project=${project.id}`} className="group">
      <Card className="h-full transition-shadow group-hover:shadow-md">
        <CardHeader className="space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Icon className="h-4 w-4" />
              {PAPER_TYPE_LABEL[project.paper_type]}
            </div>
            <Badge variant={STATUS_VARIANT[project.status]}>{STATUS_LABEL[project.status]}</Badge>
          </div>
          <CardTitle className="line-clamp-2 text-base leading-snug">{project.title}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {project.topic && (
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

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-4 rounded-xl border border-dashed py-20')}>
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted">
        <FileText className="h-6 w-6 text-muted-foreground" />
      </div>
      <div className="text-center">
        <p className="font-medium">还没有项目</p>
        <p className="text-sm text-muted-foreground">创建第一个综述或研究型论文项目开始。</p>
      </div>
      <Button onClick={onCreate}>
        <Plus className="h-4 w-4" /> 新建项目
      </Button>
    </div>
  );
}
