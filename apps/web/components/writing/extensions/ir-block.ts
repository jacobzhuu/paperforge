import { Node, mergeAttributes } from '@tiptap/core';
import { FIGURE_NODE, IR_BLOCK_NODE, MATH_NODE, XREF_NODE } from '@/lib/ir-serde';

interface BlockPayload {
  type?: string;
  latex?: string;
  caption?: string;
  text?: string;
  asset_ref?: string;
  label?: string | null;
  alt_text?: string;
  width?: 'column' | 'full';
  source?: { kind?: string; ref?: string | null };
}

export const FigureBlockNode = Node.create<{
  previewUrls: Record<string, string>;
  aiAssetRefs: Set<string>;
}>({
  name: FIGURE_NODE,
  group: 'block',
  atom: true,
  selectable: true,

  addOptions() {
    return { previewUrls: {}, aiAssetRefs: new Set<string>() };
  },

  addAttributes() {
    return { payload: { default: null, rendered: false } };
  },

  parseHTML() {
    return [{ tag: `figure[data-type="${FIGURE_NODE}"]` }];
  },

  renderHTML({ node, HTMLAttributes }) {
    const payload = (node.attrs.payload ?? {}) as BlockPayload;
    const ref = payload.asset_ref ?? '';
    const src = this.options.previewUrls[ref];
    const ai = this.options.aiAssetRefs.has(ref);
    const children: unknown[] = [];
    if (src) {
      children.push([
        'img',
        {
          src,
          alt: payload.alt_text || payload.caption || '',
          class: 'max-h-80 w-full rounded-md bg-white object-contain',
        },
      ]);
    } else {
      children.push([
        'div',
        { class: 'flex min-h-24 items-center justify-center rounded-md bg-muted text-xs text-muted-foreground' },
        `图片预览暂不可用 · ${ref || '未指定素材'}`,
      ]);
    }
    children.push([
      'figcaption',
      { class: 'mt-2 text-center text-sm text-muted-foreground' },
      `${payload.caption || '未填写图注'}${ai ? ' · AI 插图' : ''}`,
    ]);
    return [
      'figure',
      mergeAttributes(HTMLAttributes, {
        'data-type': FIGURE_NODE,
        contenteditable: 'false',
        class: 'pf-figure-block my-4 rounded-lg border bg-card p-3',
      }),
      ...children,
    ];
  },
});

export const FigureXref = Node.create({
  name: XREF_NODE,
  group: 'inline',
  inline: true,
  atom: true,

  addAttributes() {
    return {
      target: { default: '' },
      kind: { default: 'figure' },
    };
  },

  parseHTML() {
    return [{ tag: `span[data-type="${XREF_NODE}"]` }];
  },

  renderHTML({ node, HTMLAttributes }) {
    const target = String(node.attrs.target ?? '');
    return [
      'span',
      mergeAttributes(HTMLAttributes, {
        'data-type': XREF_NODE,
        contenteditable: 'false',
        class: 'mx-0.5 rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-xs text-primary',
        title: target,
      }),
      `图引用 · ${target}`,
    ];
  },
});

const BLOCK_LABEL: Record<string, string> = {
  equation: '公式',
  figure: '图',
  table: '表',
  algorithm: '算法',
  todo: '待补充',
};

function describe(payload: BlockPayload): string {
  switch (payload.type) {
    case 'equation':
    case 'algorithm':
      return payload.latex ?? '';
    case 'figure':
      return `${payload.asset_ref ?? '未指定素材'}${payload.caption ? ` — ${payload.caption}` : ''}`;
    case 'table':
      return `${payload.source?.ref ?? '未指定素材'}${payload.caption ? ` — ${payload.caption}` : ''}`;
    case 'todo':
      return payload.text ?? '待补充实验数据';
    default:
      return JSON.stringify(payload);
  }
}

/**
 * 结构化块的只读透传节点。
 *
 * PaperIR 有六种块（schema.py 的 `Block` 联合），编辑器只能编辑其中的 paragraph。
 * 其余五种——以及将来新增的任何类型——整体塞进 `payload` 属性原样带回，
 * 保证编辑任意一段都不会把它们抹掉。
 *
 * 尤其是 `todo`：它是「不编造实验数据」这条产品红线的载体
 * （无素材时渲染 `\todo{待补充实验数据}`），此前编辑器结构上无法表达它。
 */
export const IrBlock = Node.create({
  name: IR_BLOCK_NODE,
  group: 'block',
  atom: true,
  selectable: true,
  draggable: false,

  addAttributes() {
    return {
      payload: {
        default: null,
        // payload 只在内存与 JSON 之间流动，不参与 HTML 序列化的往返。
        rendered: false,
      },
    };
  },

  parseHTML() {
    return [{ tag: `div[data-type="${IR_BLOCK_NODE}"]` }];
  },

  renderHTML({ node, HTMLAttributes }) {
    const payload = (node.attrs.payload ?? {}) as BlockPayload;
    const kind = payload.type ?? 'unknown';
    const isTodo = kind === 'todo';
    const label = BLOCK_LABEL[kind] ?? kind;

    return [
      'div',
      mergeAttributes(HTMLAttributes, {
        'data-type': IR_BLOCK_NODE,
        contenteditable: 'false',
        class: [
          'pf-ir-block my-3 rounded-md border-l-4 px-3 py-2 text-sm',
          isTodo
            ? 'border-l-warning border border-dashed border-warning/50 bg-warning/10'
            : 'border-l-primary/60 border bg-muted/40',
        ].join(' '),
      }),
      [
        'div',
        { class: 'mb-1 text-xs font-medium text-muted-foreground' },
        isTodo ? `${label} · 系统不会编造实验数值` : `${label} · 由渲染器确定性生成`,
      ],
      [
        'div',
        { class: 'whitespace-pre-wrap break-words font-mono text-xs' },
        describe(payload),
      ],
    ];
  },
});

/**
 * 行内公式的只读原子节点。
 *
 * 仓库里没有 KaTeX（也不为此新增依赖：写作管线目前不产出 math_inline）。
 * 这里按源码样式呈现——**能看见、能保存、不丢失**优先于渲染得好看。
 */
export const MathInline = Node.create({
  name: MATH_NODE,
  group: 'inline',
  inline: true,
  atom: true,

  addAttributes() {
    return {
      latex: {
        default: '',
        parseHTML: (element) => element.getAttribute('data-latex') ?? '',
        renderHTML: (attributes) => ({ 'data-latex': String(attributes.latex ?? '') }),
      },
    };
  },

  parseHTML() {
    return [{ tag: `span[data-type="${MATH_NODE}"]` }];
  },

  renderHTML({ node, HTMLAttributes }) {
    const latex = String(node.attrs.latex ?? '');
    return [
      'span',
      mergeAttributes(HTMLAttributes, {
        'data-type': MATH_NODE,
        contenteditable: 'false',
        class:
          'mx-0.5 rounded border border-border bg-muted px-1 py-0.5 align-baseline font-mono text-xs',
        title: `行内公式：${latex}`,
      }),
      latex,
    ];
  },
});
