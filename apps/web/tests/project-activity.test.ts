import { describe, expect, it } from 'vitest';
import { projectActivity } from '@/lib/project-activity';
import { makeProject } from './helpers';

describe('项目继续操作提示', () => {
  it('需要补充时优先引导补充，不显示旧的下一步', () => {
    const project = makeProject({ intake: { version: 1, status: 'needs_input' } });
    expect(projectActivity(project)).toBe('补充研究方向');
  });
  it('需求理解失败明确提供重试方向', () => {
    expect(projectActivity(makeProject({ intake: { version: 1, status: 'failed' } }))).toBe('重试需求理解');
  });
  it('规划完成且无正文时引导查看规划，有正文时继续研究', () => {
    const project = makeProject({ intake: { version: 1, status: 'ready' }, section_count: 0 });
    expect(projectActivity(project)).toBe('查看研究规划');
    expect(projectActivity({ ...project, section_count: 1 })).toBe('继续研究');
  });
});
