import { describe, expect, it } from 'vitest';
import { insertPositions } from '@/lib/section-positions';
import type { SectionIR } from '@/lib/types';

function section(blocks: unknown[]): SectionIR {
  return { key: 's', level: 1, title: 's', blocks, citation_warnings: [] } as unknown as SectionIR;
}

describe('插入位置', () => {
  it('把 block 下标翻成人能读的锚点', () => {
    const positions = insertPositions(
      section([
        { type: 'paragraph', runs: [] },
        { type: 'paragraph', runs: [] },
        { type: 'figure', asset_ref: 'va_a' },
      ]),
    );
    expect(positions.map((p) => p.label)).toEqual([
      '章节开头',
      '第 1 段之后',
      '第 2 段之后',
      '章节末尾',
    ]);
    // 下标退回实现细节，但提交给接口的仍是它。
    expect(positions.map((p) => p.index)).toEqual([0, 1, 2, 3]);
  });

  it('段落、图、表各自独立编号——用户按看到的东西定位', () => {
    const labels = insertPositions(
      section([
        { type: 'figure', asset_ref: 'va_a' },
        { type: 'table', source: {} },
        { type: 'paragraph', runs: [] },
      ]),
    ).map((p) => p.label);
    expect(labels).toContain('图 1 之后');
    expect(labels).toContain('表 1 之后');
    expect(labels[labels.length - 1]).toBe('章节末尾');
  });

  it('空章节只给一个开头位置，并说明本节还没内容', () => {
    const positions = insertPositions(section([]));
    expect(positions).toHaveLength(1);
    expect(positions[0].index).toBe(0);
    expect(positions[0].label).toMatch(/暂无内容/);
  });

  it('章节缺失时不抛错', () => {
    expect(insertPositions(undefined)).toHaveLength(1);
  });
});
