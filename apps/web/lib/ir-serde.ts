import type { IRBlock, IRFigure, IRList, IRParagraph, IRRun, IRTextMark, SectionIR } from './types';

/**
 * PaperIR ⇄ Tiptap 文档的双向转换。
 *
 * 存在的理由：编辑器此前只认 `paragraph` 一种块。`normalizeBlocks` 对每个块只读
 * `block.runs`，公式/图/表/算法/todo 没有 runs，于是退化成空段落；保存时
 * `blocks: paragraphs.map(toIRParagraph)` 又把**整个** blocks 数组覆写掉。
 * 后端 `PUT /sections/{key}` 是整体替换（routers/writing.py），没有合并保护。
 * 也就是说：只要写作器开始产出结构化块（设计 §4.4.2 要求的 booktabs 表格、
 * \includegraphics 图、\todo{} 占位），用户改任意一段就会把它们全部抹掉。
 *
 * 这里的契约是**无损往返**：
 * - 段落与列表展开成可编辑内容（文本 + 强调 / 引用 chip / 行内公式）；
 * - 其余块（含未来新增的未知类型）整体塞进 `irBlock` 原子节点的 payload 里原样带回。
 *
 * 关于富文本：`TextRun.marks` 与 `ListBlock` 是 IR 里的**结构化**表示，
 * 渲染器据此确定性展开 \textbf{} / \emph{} / itemize / enumerate。
 * 编辑器因此可以提供加粗、斜体与列表而不丢数据，也不需要 LLM 书写任何 LaTeX。
 */

export interface TiptapMark {
  type: string;
}

export interface TiptapNode {
  type: string;
  attrs?: Record<string, unknown>;
  content?: TiptapNode[];
  marks?: TiptapMark[];
  text?: string;
}

export interface TiptapDoc {
  type: 'doc';
  content: TiptapNode[];
}

export const CITE_NODE = 'citeChip';
export const MATH_NODE = 'mathInline';
export const IR_BLOCK_NODE = 'irBlock';
export const FIGURE_NODE = 'figureBlock';
export const XREF_NODE = 'figureXref';

/** IR mark 名 ⇄ Tiptap mark 名。 */
const MARK_TO_TIPTAP: Record<IRTextMark, string> = { bold: 'bold', italic: 'italic' };
const TIPTAP_TO_MARK: Record<string, IRTextMark> = { bold: 'bold', italic: 'italic' };

export function isParagraph(block: IRBlock): block is IRParagraph {
  return block.type === 'paragraph';
}

export function isList(block: IRBlock): block is IRList {
  return block.type === 'list';
}

export function isFigure(block: IRBlock): block is IRFigure {
  return block.type === 'figure';
}

/** 承载 run 的块（段落与列表），其余一律按结构化块透传。 */
function hasRuns(block: IRBlock): block is IRParagraph | IRList {
  return isParagraph(block) || isList(block);
}

/**
 * 规范化 IR，使 `ir → tiptap → ir` 成为幂等。
 *
 * 必须抹平的差异：
 * - 相邻且强调相同的 text run 在 Tiptap 里会合并成一个 text 节点（渲染等价）；
 * - 空 text run 在 Tiptap 里无法表示（ProseMirror 不允许空文本节点）；
 * - 空段落必须给出一个可放光标的位置，而它在 IR 里没有意义。
 *
 * 不先规范化，dirty 检测会在用户什么都没改时就亮起「未保存」。
 */
export function normalizeSectionIR(section: SectionIR): SectionIR {
  const blocks: IRBlock[] = [];
  for (const block of section.blocks ?? []) {
    if (isList(block)) {
      const items = (block.items ?? [])
        .map((item) => ({ runs: normalizeRuns(item.runs ?? []) }))
        .filter((item) => item.runs.length > 0);
      if (items.length === 0) continue;
      blocks.push({ type: 'list', ordered: Boolean(block.ordered), items });
      continue;
    }
    if (!isParagraph(block)) {
      blocks.push(block);
      continue;
    }
    const runs = normalizeRuns(block.runs ?? []);
    // 空段落不落库：它不携带内容，留着只会让往返不幂等。
    if (runs.length === 0) continue;
    blocks.push({ ...block, type: 'paragraph', runs });
  }
  return {
    ...section,
    blocks,
    citation_warnings: section.citation_warnings ?? [],
  };
}

/** marks 排序 + 去重，保证同样的强调组合有唯一表示。 */
function normalizeMarks(marks: IRTextMark[] | undefined): IRTextMark[] {
  if (!marks || marks.length === 0) return [];
  const order: IRTextMark[] = ['bold', 'italic'];
  return order.filter((m) => marks.includes(m));
}

function sameMarks(a: IRTextMark[], b: IRTextMark[]): boolean {
  return a.length === b.length && a.every((m, i) => m === b[i]);
}

function normalizeRuns(runs: IRRun[]): IRRun[] {
  const out: IRRun[] = [];
  for (const run of runs) {
    if (run.t === 'text') {
      if (!run.v) continue;
      const marks = normalizeMarks(run.marks);
      const prev = out[out.length - 1];
      // 只有强调完全相同的相邻文本才合并——否则会把加粗涂到普通文字上。
      if (prev && prev.t === 'text' && sameMarks(normalizeMarks(prev.marks), marks)) {
        out[out.length - 1] = marks.length
          ? { t: 'text', v: prev.v + run.v, marks }
          : { t: 'text', v: prev.v + run.v };
        continue;
      }
      out.push(marks.length ? { t: 'text', v: run.v, marks } : { t: 'text', v: run.v });
      continue;
    }
    if (run.t === 'cite') {
      // 去重但保序：同一段落里重复插入同一个 key 没有意义。
      const keys = Array.from(new Set(run.keys ?? []));
      if (keys.length === 0) continue;
      const evidenceIds = Array.from(new Set(run.evidence_ids ?? []));
      out.push({
        t: 'cite',
        keys,
        ...(evidenceIds.length > 0 ? { evidence_ids: evidenceIds } : {}),
      });
      continue;
    }
    if (run.t === 'grounding') {
      const sourceRefs = Array.from(new Set(run.source_refs ?? [])).filter(Boolean);
      if (sourceRefs.length > 0) out.push({ t: 'grounding', source_refs: sourceRefs });
      continue;
    }
    if (run.t === 'math_inline') {
      if (!run.v) continue;
      out.push({ t: 'math_inline', v: run.v });
      continue;
    }
    if (run.t === 'xref' && run.target) {
      out.push({ t: 'xref', target: run.target, kind: run.kind ?? 'figure' });
    }
  }
  return out;
}

export function irToTiptap(section: SectionIR): TiptapDoc {
  const blocks = section.blocks ?? [];
  const content = blocks.map(blockToNode);
  // ProseMirror 的 doc 至少要有一个块节点，否则编辑器无法获得光标位置。
  if (content.length === 0) content.push({ type: 'paragraph' });
  return { type: 'doc', content };
}

function runsToNodes(runs: IRRun[]): TiptapNode[] {
  // Grounding remains in normalized IR but has no editor node: it is restored only when
  // the corresponding visible paragraph is unchanged.
  return normalizeRuns(runs)
    .filter((run) => run.t !== 'grounding')
    .map(runToNode);
}

function blockToNode(block: IRBlock): TiptapNode {
  if (isParagraph(block)) {
    const content = runsToNodes(block.runs ?? []);
    return content.length > 0 ? { type: 'paragraph', content } : { type: 'paragraph' };
  }
  if (isList(block)) {
    return {
      type: block.ordered ? 'orderedList' : 'bulletList',
      content: (block.items ?? []).map((item) => ({
        type: 'listItem',
        content: [{ type: 'paragraph', content: runsToNodes(item.runs ?? []) }],
      })),
    };
  }
  if (isFigure(block)) {
    return { type: FIGURE_NODE, attrs: { payload: block } };
  }
  // 结构化块整体透传：编辑器只读渲染，保存时原样写回。
  return { type: IR_BLOCK_NODE, attrs: { payload: block } };
}

function runToNode(run: IRRun): TiptapNode {
  if (run.t === 'text') {
    const marks = normalizeMarks(run.marks).map((m) => ({ type: MARK_TO_TIPTAP[m] }));
    return marks.length > 0
      ? { type: 'text', text: run.v, marks }
      : { type: 'text', text: run.v };
  }
  if (run.t === 'cite') {
    return {
      type: CITE_NODE,
      attrs: { keys: run.keys, evidenceIds: run.evidence_ids ?? [] },
    };
  }
  if (run.t === 'xref') {
    return { type: XREF_NODE, attrs: { target: run.target, kind: run.kind } };
  }
  if (run.t === 'math_inline') {
    return { type: MATH_NODE, attrs: { latex: run.v } };
  }
  // runsToNodes filters audit-only grounding before this point.
  return { type: 'text', text: '' };
}

export function tiptapToIR(doc: TiptapDoc | undefined, base: SectionIR): SectionIR {
  const nodes = doc?.content ?? [];
  const blocks: IRBlock[] = [];
  for (const node of nodes) {
    const block = nodeToBlock(node);
    if (block) blocks.push(block);
  }
  const stances = (base.blocks ?? [])
    .filter(isParagraph)
    .map((block) => block.stance_summary);
  let paragraphIndex = 0;
  const withStances = blocks.map((block) => {
    if (!isParagraph(block)) return block;
    const stance = stances[paragraphIndex++];
    return stance ? { ...block, stance_summary: stance } : block;
  });
  return normalizeSectionIR({
    ...base,
    blocks: restoreUnchangedGrounding(withStances, base.blocks ?? []),
  });
}

function restoreUnchangedGrounding(blocks: IRBlock[], baseBlocks: IRBlock[]): IRBlock[] {
  const signature = (block: IRParagraph) =>
    (block.runs ?? [])
      .filter((run) => run.t !== 'grounding')
      .map((run) =>
        run.t === 'text'
          ? `t:${run.v}`
          : run.t === 'cite'
            ? `c:${run.keys.join(',')}:${(run.evidence_ids ?? []).join(',')}`
            : JSON.stringify(run),
      )
      .join('|');
  return blocks.map((block, index) => {
    const previous = baseBlocks[index];
    if (!isParagraph(block) || !previous || !isParagraph(previous)) return block;
    const groundings = (previous.runs ?? []).filter((run) => run.t === 'grounding');
    if (groundings.length === 0 || signature(block) !== signature(previous)) return block;
    // Exact content/citation match: retain the original interleaving so every
    // grounding remains attached to its sentence.
    return previous;
  });
}

function nodesToRuns(nodes: TiptapNode[] | undefined): IRRun[] {
  const runs: IRRun[] = [];
  for (const child of nodes ?? []) {
    const run = nodeToRun(child);
    if (run) runs.push(run);
  }
  return normalizeRuns(runs);
}

function nodeToBlock(node: TiptapNode): IRBlock | null {
  if (node.type === FIGURE_NODE) {
    return (node.attrs?.payload ?? null) as IRFigure | null;
  }
  if (node.type === IR_BLOCK_NODE) {
    const payload = node.attrs?.payload;
    // payload 是从后端原样带下来的对象，直接写回。
    return (payload ?? null) as IRBlock | null;
  }
  if (node.type === 'paragraph') {
    return { type: 'paragraph', runs: nodesToRuns(node.content) };
  }
  if (node.type === 'bulletList' || node.type === 'orderedList') {
    const items = (node.content ?? []).map((li) => ({
      // listItem 内部是一个或多个 paragraph；扁平化成一串 run。
      runs: (li.content ?? []).flatMap((child) => nodesToRuns(child.content)),
    }));
    return { type: 'list', ordered: node.type === 'orderedList', items };
  }
  return null;
}

function nodeToRun(node: TiptapNode): IRRun | null {
  if (node.type === 'text') {
    if (!node.text) return null;
    const marks: IRTextMark[] = [];
    for (const mark of node.marks ?? []) {
      const mapped = TIPTAP_TO_MARK[mark.type];
      if (mapped && !marks.includes(mapped)) marks.push(mapped);
    }
    const normalized = normalizeMarks(marks);
    return normalized.length > 0
      ? { t: 'text', v: node.text, marks: normalized }
      : { t: 'text', v: node.text };
  }
  if (node.type === CITE_NODE) {
    const keys = (node.attrs?.keys as string[] | undefined) ?? [];
    const evidenceIds = (node.attrs?.evidenceIds as string[] | undefined) ?? [];
    return keys.length > 0
      ? {
          t: 'cite',
          keys,
          ...(evidenceIds.length > 0 ? { evidence_ids: evidenceIds } : {}),
        }
      : null;
  }
  if (node.type === MATH_NODE) {
    const latex = String(node.attrs?.latex ?? '');
    return latex ? { t: 'math_inline', v: latex } : null;
  }
  if (node.type === XREF_NODE) {
    const target = String(node.attrs?.target ?? '');
    return target ? { t: 'xref', target, kind: node.attrs?.kind === 'equation' ? 'equation' : 'figure' } : null;
  }
  return null;
}

/** 章节内出现的全部引用 key，保序去重。列表项里的引用同样计入。 */
export function collectCiteKeys(section: SectionIR): string[] {
  const keys: string[] = [];
  for (const block of section.blocks ?? []) {
    if (!hasRuns(block)) continue;
    const runGroups = isParagraph(block)
      ? [block.runs ?? []]
      : (block.items ?? []).map((item) => item.runs ?? []);
    for (const runs of runGroups) {
      for (const run of runs) {
        if (run.t === 'cite') keys.push(...run.keys);
      }
    }
  }
  return Array.from(new Set(keys));
}

/** 中文按字计、西文按词计——与 worker 的 count_words 口径一致。 */
export function countWords(section: SectionIR): number {
  const text = sectionPlainText(section);
  const cjk = text.match(/[一-鿿]/g)?.length ?? 0;
  const latin = text.match(/[A-Za-z][A-Za-z'-]*/g)?.length ?? 0;
  return cjk + latin;
}

/** 章节正文纯文本，供选区润色与定位使用。 */
export function sectionPlainText(section: SectionIR): string {
  const parts: string[] = [];
  for (const block of section.blocks ?? []) {
    if (isParagraph(block)) {
      parts.push(
        (block.runs ?? []).map((run) => (run.t === 'text' ? run.v : '')).join(''),
      );
      continue;
    }
    if (isList(block)) {
      for (const item of block.items ?? []) {
        parts.push((item.runs ?? []).map((run) => (run.t === 'text' ? run.v : '')).join(''));
      }
    }
  }
  return parts.filter(Boolean).join('\n\n');
}
