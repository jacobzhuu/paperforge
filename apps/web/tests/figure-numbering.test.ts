import { describe, expect, it } from 'vitest';
import { buildFigureNumbering } from '@/lib/figure-numbering';
import type { PaperSection } from '@/lib/types';

function section(key: string, orderNo: number, figures: string[]): PaperSection {
  return {
    section_key: key,
    title: key,
    order_no: orderNo,
    status: 'generated',
    cite_keys: [],
    body_ir: {
      key,
      level: 1,
      title: key,
      blocks: figures.map((ref) => ({
        type: 'figure',
        asset_ref: ref,
        label: `fig:${ref}`,
        caption: `${ref} 的图注`,
        alt_text: '',
      })),
      citation_warnings: [],
    },
    citation_warnings: [],
    word_count: 0,
  } as unknown as PaperSection;
}

describe('全文图编号', () => {
  it('按章节顺序连续编号，而不是按接口返回顺序', () => {
    // 故意乱序传入：接口不保证按 order_no 返回。
    const numbering = buildFigureNumbering([
      section('results', 2, ['va_c']),
      section('intro', 1, ['va_a', 'va_b']),
    ]);
    expect(numbering.byAssetRef).toEqual({ va_a: 1, va_b: 2, va_c: 3 });
    expect(numbering.byLabel['fig:va_c']).toBe(3);
  });

  it('图注随编号一起给出，供引用芯片 hover 显示', () => {
    const numbering = buildFigureNumbering([section('intro', 1, ['va_a'])]);
    expect(numbering.captions['fig:va_a']).toBe('va_a 的图注');
  });

  it('没有图时返回空表而不是抛错', () => {
    expect(buildFigureNumbering([]).byLabel).toEqual({});
    expect(buildFigureNumbering([section('intro', 1, [])]).byAssetRef).toEqual({});
  });
});
