import type { Project } from './types';
import { stageLabel } from './labels';

/** Same resume hint on the home page and project list, using server state only. */
export function projectActivity(project: Project): string {
  if (project.intake?.status === 'needs_input') return '补充研究方向';
  if (project.attention_summary?.active_job) return `正在${stageLabel(project.attention_summary.active_job.stage)}`;
  if (project.intake?.status === 'running') return '查看需求理解进度';
  if (project.intake?.status === 'failed') return '重试需求理解';
  if (project.intake?.status === 'pending') return '开始需求理解';
  if (project.intake?.status === 'ready' && !project.attention_summary?.manuscript.sectionCount && !project.section_count) return '查看研究规划';
  return project.attention_summary?.readiness.nextAction || '继续研究';
}
