'use client';

import Link from 'next/link';
import { BookOpen, ChevronLeft, FlaskConical } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { CITATION_STYLE_LABEL, LANGUAGE_LABEL, PAPER_TYPE_LABEL, VENUE_TEMPLATES, WRITING_MODE_LABEL } from '@/lib/labels';
import type { Project } from '@/lib/types';
import { ProjectTitle } from './project-title';

/**
 * 项目头。
 *
 * 刻意**不显示 `project.status`**：那个字段永远是 'draft'
 * （`set_project_status` 无调用方），显示它等于对用户撒谎。真实进度由
 * 管线导航的状态点与概览页的产物计数承担。
 */
export function ProjectHeader({
  project,
  onRenamed,
}: {
  project: Project | undefined;
  onRenamed?: (title: string) => void;
}) {
  if (!project) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-7 w-2/3" />
      </div>
    );
  }

  const Icon = project.paper_type === 'review' ? BookOpen : FlaskConical;
  const template = VENUE_TEMPLATES.find((t) => t.id === project.venue_template);

  return (
    <div className="space-y-2">
      <Link
        href="/projects"
        className="inline-flex items-center gap-1 rounded text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ChevronLeft className="h-3.5 w-3.5" /> 全部项目
      </Link>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        {/* 论文题目是产品的中心对象（ui-design.md 原则 01）：serif，且可就地改名。 */}
        <ProjectTitle
          projectId={String(project.id)}
          title={project.title}
          onRenamed={(title) => onRenamed?.(title)}
        />
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant="secondary" className="gap-1">
            <Icon className="h-3 w-3" />
            {PAPER_TYPE_LABEL[project.paper_type]}
          </Badge>
          <Badge variant="outline">{LANGUAGE_LABEL[project.language]}</Badge>
          {project.citation_style && (
            <Badge variant="outline">{CITATION_STYLE_LABEL[project.citation_style]}</Badge>
          )}
          {template && <Badge variant="outline">{template.label}</Badge>}
          <Badge variant="muted">{WRITING_MODE_LABEL[project.writing_mode]}</Badge>
        </div>
      </div>
    </div>
  );
}
