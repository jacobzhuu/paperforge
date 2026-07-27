import { describe, expect, it } from 'vitest';
import { groupVisualsByLineage } from '@/components/visuals/use-visuals';
import type { VisualAsset } from '@/lib/types';

function visual(overrides: Partial<VisualAsset> & { id: string }): VisualAsset {
  return {
    asset_ref: `va_${overrides.id}`,
    kind: 'diagram',
    generation_status: 'ready',
    review_status: 'pending',
    caption: '',
    alt_text: '',
    figure_label: `fig:va_${overrides.id}`,
    spec: {},
    renditions: {},
    input_hash: overrides.id,
    version: 1,
    ...overrides,
  } as VisualAsset;
}

describe('版本折叠', () => {
  it('沿 supersedes_id 链把同一张图的多个版本归成一组', () => {
    const groups = groupVisualsByLineage([
      visual({ id: 'v3', version: 3, supersedes_id: 'v2' }),
      visual({ id: 'v1', version: 1 }),
      visual({ id: 'v2', version: 2, supersedes_id: 'v1' }),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].rootId).toBe('v1');
    // 版本从新到旧：用户先看到最新的那一版。
    expect(groups[0].versions.map((item) => item.id)).toEqual(['v3', 'v2', 'v1']);
    expect(groups[0].latest.id).toBe('v3');
  });

  it('已批准的版本被单独标出——「正文里现在是哪一张」必须一眼可见', () => {
    const groups = groupVisualsByLineage([
      visual({ id: 'v1', version: 1, review_status: 'approved' }),
      visual({ id: 'v2', version: 2, supersedes_id: 'v1' }),
    ]);
    expect(groups[0].approved?.id).toBe('v1');
    expect(groups[0].latest.id).toBe('v2');
  });

  it('不同 lineage 不会被合并', () => {
    const groups = groupVisualsByLineage([visual({ id: 'a' }), visual({ id: 'b' })]);
    expect(groups).toHaveLength(2);
  });

  it('supersedes_id 指向已被删除的行时，该版本自成一组而不是丢失', () => {
    const groups = groupVisualsByLineage([visual({ id: 'v2', version: 2, supersedes_id: 'gone' })]);
    expect(groups).toHaveLength(1);
    expect(groups[0].latest.id).toBe('v2');
  });
});
