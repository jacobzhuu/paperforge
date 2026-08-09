'use client';

import * as React from 'react';
import { API_BASE } from '@/lib/api';

/**
 * 全文 Markdown 预览。
 *
 * 此前是一个 `<pre>{markdown}</pre>`——后端 `paper_ir/markdown.py` 会把六种块
 * 渲染成带标题、表格、`$$` 公式与图片的结构化 Markdown，前端却当纯文本吐出，
 * 「预览」名不副实。
 *
 * 这里做的是一个**克制的**结构化渲染：标题、段落、列表、表格、代码/公式块、
 * 引用块、水平线。刻意不引入 markdown 解析库与 KaTeX——预览只需要能通读，
 * 真正的排版由 LaTeX 渲染器负责，PDF 才是权威呈现。
 */
export function MarkdownPreview({ markdown }: { markdown: string }) {
  const blocks = React.useMemo(() => parseBlocks(markdown), [markdown]);

  if (!markdown.trim()) {
    return (
      <section>
        <div className="py-16 text-center text-body text-muted-foreground">
          暂无预览。Markdown 预览在正文生成后自动产出。
        </div>
      </section>
    );
  }

  return (
    <section>
      <div className="max-h-[calc(100vh-16rem)] overflow-y-auto py-6 scrollbar-thin">
        {/* pf-paper：全文预览是「读论文」的界面，走衬线；编辑器不加，见 globals.css。 */}
        <div className="pf-prose pf-paper">
          {blocks.map((block, index) => (
            <Block key={index} block={block} />
          ))}
        </div>
      </div>
    </section>
  );
}

type ParsedBlock =
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'paragraph'; text: string }
  | { kind: 'list'; ordered: boolean; items: string[] }
  | { kind: 'table'; header: string[]; rows: string[][] }
  | { kind: 'fence'; text: string; lang: string }
  | { kind: 'quote'; text: string }
  | { kind: 'image'; alt: string; src: string }
  | { kind: 'rule' };

function parseBlocks(markdown: string): ParsedBlock[] {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const blocks: ParsedBlock[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    // 围栏代码 / 公式块
    const fence = /^```(\w*)\s*$/.exec(line);
    if (fence) {
      const lang = fence[1] ?? '';
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1;
      blocks.push({ kind: 'fence', text: body.join('\n'), lang });
      continue;
    }

    // $$ 公式块
    if (/^\$\$\s*$/.test(line)) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^\$\$\s*$/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1;
      blocks.push({ kind: 'fence', text: body.join('\n'), lang: 'math' });
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      blocks.push({ kind: 'rule' });
      i += 1;
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({ kind: 'heading', level: heading[1].length, text: heading[2].trim() });
      i += 1;
      continue;
    }

    const image = /^!\[([^\]]*)\]\(([^)]+)\)\s*$/.exec(line);
    if (image) {
      blocks.push({ kind: 'image', alt: image[1], src: image[2] });
      i += 1;
      continue;
    }

    // 表格：`| a | b |` 后跟分隔行
    if (line.trim().startsWith('|') && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const header = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      blocks.push({ kind: 'table', header, rows });
      continue;
    }

    if (/^>\s?/.test(line)) {
      const body: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        body.push(lines[i].replace(/^>\s?/, ''));
        i += 1;
      }
      blocks.push({ kind: 'quote', text: body.join(' ') });
      continue;
    }

    const bullet = /^\s*([-*+]|\d+\.)\s+/.exec(line);
    if (bullet) {
      const ordered = /\d/.test(bullet[1]);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, ''));
        i += 1;
      }
      blocks.push({ kind: 'list', ordered, items });
      continue;
    }

    // 普通段落：吃到下一个空行或结构行为止
    const body: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,6}\s|```|\$\$|>\s?|\s*([-*+]|\d+\.)\s)/.test(lines[i]) &&
      !lines[i].trim().startsWith('|')
    ) {
      body.push(lines[i].trim());
      i += 1;
    }
    if (body.length > 0) blocks.push({ kind: 'paragraph', text: body.join(' ') });
    else i += 1;
  }

  return blocks;
}

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
}

function Block({ block }: { block: ParsedBlock }) {
  switch (block.kind) {
    case 'heading': {
      const size =
        block.level === 1
          ? 'text-2xl'
          : block.level === 2
            ? 'text-xl'
            : block.level === 3
              ? 'text-lg'
              : 'text-base';
      return (
        <p className={`mb-2 mt-6 font-semibold tracking-tight first:mt-0 ${size}`}>
          <Inline text={block.text} />
        </p>
      );
    }
    case 'paragraph':
      return (
        <p className="mb-4 leading-[1.75]">
          <Inline text={block.text} />
        </p>
      );
    case 'list':
      return block.ordered ? (
        <ol className="mb-4 list-decimal space-y-1 pl-6 leading-[1.75]">
          {block.items.map((item, i) => (
            <li key={i}>
              <Inline text={item} />
            </li>
          ))}
        </ol>
      ) : (
        <ul className="mb-4 list-disc space-y-1 pl-6 leading-[1.75]">
          {block.items.map((item, i) => (
            <li key={i}>
              <Inline text={item} />
            </li>
          ))}
        </ul>
      );
    case 'table':
      return (
        <div className="mb-4 overflow-x-auto">
          <table className="w-full border-collapse text-body">
            <thead>
              <tr className="border-b">
                {block.header.map((cell, i) => (
                  <th key={i} className="px-3 py-2 text-left font-medium">
                    {cell}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, i) => (
                <tr key={i} className="border-b last:border-0">
                  {row.map((cell, j) => (
                    <td key={j} className="px-3 py-1.5 tabular-nums">
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case 'fence':
      return (
        <pre className="mb-4 overflow-x-auto rounded-md border bg-muted/40 p-3 font-mono text-meta leading-relaxed scrollbar-thin">
          {block.lang === 'math' && (
            <span className="mb-1 block text-meta text-muted-foreground">公式（LaTeX 源）</span>
          )}
          {block.text}
        </pre>
      );
    case 'quote':
      return (
        <blockquote className="mb-4 border-l-2 border-primary/40 pl-3 text-muted-foreground">
          <Inline text={block.text} />
        </blockquote>
      );
    case 'image': {
      const src = block.src.startsWith('/api/') ? `${API_BASE}${block.src}` : block.src;
      return (
        <figure className="mb-4 rounded-lg border bg-white p-3">
          {/* eslint-disable-next-line @next/next/no-img-element -- protected API rendition URL */}
          <img src={src} alt={block.alt} className="mx-auto max-h-[28rem] w-full object-contain" />
        </figure>
      );
    }
    case 'rule':
      return <hr className="my-6" />;
  }
}

/** 行内标记：粗体、斜体、行内代码、行内公式。 */
function Inline({ text }: { text: string }) {
  const parts = React.useMemo(
    () => text.split(/(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\$[^$]+\$)/g).filter(Boolean),
    [text],
  );

  return (
    <>
      {parts.map((part, i) => {
        if (part.startsWith('**') && part.endsWith('**')) {
          return <strong key={i}>{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith('*') && part.endsWith('*') && part.length > 2) {
          return <em key={i}>{part.slice(1, -1)}</em>;
        }
        if (part.startsWith('`') && part.endsWith('`')) {
          return (
            <code key={i} className="rounded bg-muted px-1 py-0.5 font-mono text-meta">
              {part.slice(1, -1)}
            </code>
          );
        }
        if (part.startsWith('$') && part.endsWith('$') && part.length > 2) {
          return (
            <span
              key={i}
              className="rounded border bg-muted px-1 font-mono text-meta"
              title="行内公式（LaTeX 源）"
            >
              {part.slice(1, -1)}
            </span>
          );
        }
        return <React.Fragment key={i}>{part}</React.Fragment>;
      })}
    </>
  );
}
