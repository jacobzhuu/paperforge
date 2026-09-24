import { describe, expect, it } from 'vitest';
import { irToTiptap, normalizeSectionIR, tiptapToIR } from '@/lib/ir-serde';
import type { SectionIR } from '@/lib/types';

describe('PaperIR 证据绑定往返', () => {
  it('编辑器往返不会丢 evidence_ids 与段落综合立场', () => {
    const section: SectionIR = {
      key: 'q1',
      level: 1,
      title: 'Question one',
      blocks: [
        {
          type: 'paragraph',
          stance_summary: 'conflicting',
          runs: [
            { t: 'text', v: 'The studies conflict.' },
            {
              t: 'cite',
              keys: ['a2024', 'b2025'],
              evidence_ids: ['e-1', 'e-2'],
            },
          ],
        },
      ],
      citation_warnings: [],
    };
    const roundTrip = tiptapToIR(irToTiptap(section), section);
    expect(roundTrip).toEqual(normalizeSectionIR(section));
  });

  it('未修改句子时保留不可见的素材绑定', () => {
    const section: SectionIR = {
      key: 's4',
      level: 1,
      title: 'Results',
      blocks: [
        {
          type: 'paragraph',
          runs: [
            { t: 'text', v: 'The recorded accuracy was 92.5%.' },
            { t: 'grounding', source_refs: ['ua_12345678'] },
          ],
        },
      ],
      citation_warnings: [],
    };

    const editor = irToTiptap(section);
    expect(JSON.stringify(editor)).not.toContain('ua_12345678');
    expect(tiptapToIR(editor, section)).toEqual(normalizeSectionIR(section));
  });

  it('句子被修改后不沿用旧素材绑定', () => {
    const section: SectionIR = {
      key: 's4',
      level: 1,
      title: 'Results',
      blocks: [
        {
          type: 'paragraph',
          runs: [
            { t: 'text', v: 'The recorded accuracy was 92.5%.' },
            { t: 'grounding', source_refs: ['ua_12345678'] },
          ],
        },
      ],
      citation_warnings: [],
    };
    const editor = irToTiptap(section);
    editor.content[0].content![0].text = 'The accuracy was excellent.';

    const saved = tiptapToIR(editor, section);
    expect(saved.blocks[0].type).toBe('paragraph');
    if (saved.blocks[0].type === 'paragraph') {
      expect(saved.blocks[0].runs.some((run) => run.t === 'grounding')).toBe(false);
    }
  });
});

it('公式、来源与公式引用在编辑保存后完整保留', () => {
  const section: SectionIR = {
    key: 'methods', title: 'Methods', level: 1, citation_warnings: [],
    blocks: [
      { type: 'paragraph', runs: [
        { t: 'text', v: 'The loss is ' }, { t: 'math_inline', v: 'L=x^2' },
        { t: 'xref', kind: 'equation', target: 'eq:loss' },
        { t: 'cite', keys: ['a'], evidence_ids: ['e1'] },
      ] },
      { type: 'equation', latex: 'L=x^2', label: 'eq:loss', source_ids: ['e1'],
        source_latex: 'L=x^2', source_context: 'x is residual.', explanation: 'x is residual.' },
    ],
  };
  expect(tiptapToIR(irToTiptap(section), section)).toEqual(normalizeSectionIR(section));
});
