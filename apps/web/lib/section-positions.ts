import type { SectionIR } from './types';

/**
 * 把章节的 block 序列翻成人能选的插入位置。
 *
 * 此前界面直接让用户填 `block_index`——一个 IR 数组下标。屏幕上写着「位置 6」，
 * 而用户根本无从知道第 6 个 block 是哪里：他看到的是段落和图，不是数组。
 * 这里改成「章节开头 / 第 2 段之后 / 图 3 之后 / 章节末尾」这类描述，
 * 下标退回成实现细节。
 */
export interface InsertPosition {
  /** 提交给 approve 接口的 block_index。 */
  index: number;
  label: string;
}

interface BlockLike {
  type?: string;
  caption?: string;
}

export function insertPositions(section: SectionIR | undefined): InsertPosition[] {
  const blocks = (section?.blocks ?? []) as unknown as BlockLike[];
  if (blocks.length === 0) {
    return [{ index: 0, label: '章节开头（本节暂无内容）' }];
  }

  const positions: InsertPosition[] = [{ index: 0, label: '章节开头' }];
  let paragraphs = 0;
  let figures = 0;
  let tables = 0;

  blocks.forEach((block, index) => {
    let anchor: string;
    switch (block.type) {
      case 'paragraph':
        paragraphs += 1;
        anchor = `第 ${paragraphs} 段之后`;
        break;
      case 'figure':
        figures += 1;
        anchor = `图 ${figures} 之后`;
        break;
      case 'table':
        tables += 1;
        anchor = `表 ${tables} 之后`;
        break;
      case 'list':
        anchor = '列表之后';
        break;
      case 'equation':
        anchor = '公式之后';
        break;
      case 'todo':
        anchor = '待补充占位之后';
        break;
      default:
        anchor = `第 ${index + 1} 块之后`;
    }
    positions.push({ index: index + 1, label: anchor });
  });

  // 末尾那一项换成更直白的说法，替掉「第 N 段之后」。
  positions[positions.length - 1] = {
    index: blocks.length,
    label: '章节末尾',
  };
  return positions;
}
