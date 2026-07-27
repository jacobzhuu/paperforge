import { describe, expect, it } from 'vitest';
import {
  pipelineSteps,
  stepForStage,
  stepFromPathname,
  workbenchSteps,
} from '@/lib/pipeline';

describe('管线步骤', () => {
  it('综述论文有视觉步骤但没有素材步骤', () => {
    const ids = pipelineSteps('review').map((step) => step.id);
    expect(ids).toContain('visuals');
    // 综述论文没有实验数据要上传——素材步骤对它没有意义。
    expect(ids).not.toContain('assets');
  });

  it('原创论文同时有素材与视觉', () => {
    const ids = pipelineSteps('original').map((step) => step.id);
    expect(ids).toContain('assets');
    expect(ids).toContain('visuals');
  });

  it('两类论文的视觉都排在写作之后、导出之前', () => {
    for (const type of ['review', 'original'] as const) {
      const ids = pipelineSteps(type).map((step) => step.id);
      expect(ids.indexOf('visuals')).toBeGreaterThan(ids.indexOf('write'));
      expect(ids.indexOf('visuals')).toBeLessThan(ids.indexOf('export'));
    }
  });

  it('视觉阶段名映射到视觉步骤——任务在跑时导航要亮起来', () => {
    expect(stepForStage('visual_plan')).toBe('visuals');
    expect(stepForStage('visual_generate')).toBe('visuals');
  });

  it('/visuals 路径能被识别成视觉步骤', () => {
    expect(stepFromPathname('/projects/p1/visuals', 'p1')).toBe('visuals');
  });

  it('上一步 / 下一步串得起来：写作的下一步是视觉', () => {
    const steps = workbenchSteps('review');
    const writeIndex = steps.findIndex((step) => step.id === 'write');
    expect(steps[writeIndex + 1]?.id).toBe('visuals');
  });
});
