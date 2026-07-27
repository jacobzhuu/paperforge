import { Node, mergeAttributes } from '@tiptap/core';
import { CITE_NODE } from '@/lib/ir-serde';

/**
 * 行内引用 chip：不可编辑的原子节点。
 *
 * 设计 §4.5：cite 是 IR 里的**原子节点**而不是正文里的字符串。做成 atom 之后
 * 正文里根本没有可以手写引用的位置，Backspace 整体删除，光标也不会掉进 key 内部——
 * 这是 R2 在前端的物理保障（取值仍只能来自白名单选择器）。
 *
 * 此前引用不在正文里，而是段落**下方**的一条 chip 带，且保存时全部塞到段落末尾，
 * 引用与它支撑的那句话在视觉上完全脱钩。
 */
export interface CiteChipOptions {
  /** 语义软校验判定为弱相关的 key，渲染成琥珀色。 */
  weakKeys: Set<string>;
}

export const CiteChip = Node.create<CiteChipOptions>({
  name: CITE_NODE,
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,

  addOptions() {
    return { weakKeys: new Set<string>() };
  },

  addAttributes() {
    return {
      keys: {
        default: [] as string[],
        parseHTML: (element) => {
          const raw = element.getAttribute('data-keys') ?? '';
          return raw ? raw.split(',') : [];
        },
        renderHTML: (attributes) => ({
          'data-keys': ((attributes.keys as string[]) ?? []).join(','),
        }),
      },
    };
  },

  parseHTML() {
    return [{ tag: `span[data-type="${CITE_NODE}"]` }];
  },

  renderHTML({ node, HTMLAttributes }) {
    const keys = (node.attrs.keys as string[]) ?? [];
    const weak = keys.some((key) => this.options.weakKeys.has(key));
    return [
      'span',
      mergeAttributes(HTMLAttributes, {
        'data-type': CITE_NODE,
        class: [
          'pf-cite',
          'mx-0.5 inline-flex items-center gap-1 rounded border px-1.5 py-0.5',
          'align-baseline font-mono text-xs leading-none',
          weak
            ? 'border-warning/50 bg-warning/15 text-warning-foreground'
            : 'border-success/50 bg-success/15 text-success-strong',
        ].join(' '),
        title: weak
          ? `语义软校验：与该处论述相关性偏低（${keys.join(', ')}）`
          : keys.join(', '),
        contenteditable: 'false',
      }),
      keys.join('; '),
    ];
  },
});
