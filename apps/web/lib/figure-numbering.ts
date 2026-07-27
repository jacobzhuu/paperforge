import type { PaperSection, SectionIR } from './types';

/**
 * 全文图编号。
 *
 * 图引用在 IR 里存的是**稳定 label**（`fig:va_xxx`），因为编号会随着插入、删除、
 * 调序不断变化——把「图 1」写死进正文，插一张图就要手工改一遍全文。
 *
 * 但用户读正文时需要看到「图 1」。这里按章节顺序、块顺序扫一遍全文，
 * 建立 `label → 编号` 与 `asset_ref → 编号` 两张表供编辑器渲染用；**保存时
 * 仍然只写 label**，编号只活在显示层。
 */
export interface FigureNumbering {
  byLabel: Record<string, number>;
  byAssetRef: Record<string, number>;
  /** 编号 → 图注，供引用芯片 hover 时显示。 */
  captions: Record<string, string>;
}

interface FigureLike {
  type?: string;
  asset_ref?: string;
  label?: string | null;
  caption?: string;
}

export function buildFigureNumbering(sections: PaperSection[]): FigureNumbering {
  const byLabel: Record<string, number> = {};
  const byAssetRef: Record<string, number> = {};
  const captions: Record<string, string> = {};
  let next = 1;

  // 按 order_no 排序：sections 数组的顺序来自接口，不保证等于正文顺序。
  const ordered = [...sections].sort((a, b) => (a.order_no ?? 0) - (b.order_no ?? 0));
  for (const section of ordered) {
    const body = section.body_ir as SectionIR | undefined;
    for (const block of body?.blocks ?? []) {
      const figure = block as unknown as FigureLike;
      if (figure.type !== 'figure') continue;
      const number = next++;
      if (figure.label) {
        byLabel[figure.label] = number;
        captions[figure.label] = figure.caption ?? '';
      }
      if (figure.asset_ref) {
        byAssetRef[figure.asset_ref] = number;
        captions[figure.asset_ref] = figure.caption ?? '';
      }
    }
  }
  return { byLabel, byAssetRef, captions };
}

export const EMPTY_NUMBERING: FigureNumbering = { byLabel: {}, byAssetRef: {}, captions: {} };
