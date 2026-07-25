'use client';

import * as React from 'react';
import { EditorContent, useEditor } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import { Loader2, Quote, Sparkles, X } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { CiteKeyPicker } from '@/components/writing/cite-key-picker';
import { refineText } from '@/lib/api';
import type { IRParagraph, IRRun, RefineAction, SectionIR, SoftCheckFinding } from '@/lib/types';

const REFINE_ACTIONS: { action: RefineAction; label: string }[] = [
  { action: 'polish', label: '润色' },
  { action: 'expand', label: '扩写' },
  { action: 'shorten', label: '缩写' },
  { action: 'academic_tone', label: '学术语气' },
];

/**
 * 章节编辑器：Tiptap 负责正文富文本，引用是**段落级原子 chip**。
 *
 * 设计 §4.5：cite 是 IR 里的原子节点而非正文字符串——所以正文里根本没有
 * 可以手写引用的地方，chip 只能从白名单选（§4.4.3 R2 的前端保障）。
 */
export function SectionEditor({
  section,
  whitelist,
  onChange,
  projectId,
  softChecks = [],
}: {
  section: SectionIR;
  whitelist: string[];
  onChange: (next: SectionIR) => void;
  projectId?: string;
  softChecks?: SoftCheckFinding[];
}) {
  const paragraphs = React.useMemo(() => normalizeBlocks(section.blocks), [section.blocks]);
  // 语义软校验只出徽章，不删引用（设计 §4.4.3 可选软校验）。
  const weakKeys = React.useMemo(
    () => new Set(softChecks.filter((f) => f.weak).map((f) => f.cite_key)),
    [softChecks],
  );

  const updateParagraph = (index: number, patch: { text?: string; keys?: string[] }) => {
    const next = paragraphs.map((p, i) =>
      i === index ? { text: patch.text ?? p.text, keys: patch.keys ?? p.keys } : p,
    );
    onChange({ ...section, blocks: next.map(toIRParagraph) });
  };

  const addParagraph = () => {
    onChange({
      ...section,
      blocks: [...paragraphs, { text: '', keys: [] }].map(toIRParagraph),
    });
  };

  const removeParagraph = (index: number) => {
    onChange({
      ...section,
      blocks: paragraphs.filter((_, i) => i !== index).map(toIRParagraph),
    });
  };

  return (
    <div className="space-y-4">
      {section.citation_warnings?.length > 0 && (
        <div className="space-y-1 rounded-md border border-warning/50 bg-warning/10 p-3 text-xs">
          {section.citation_warnings.map((warning, index) => (
            <p key={index}>
              ⚠️ {warning.message}
              <span className="ml-1 font-mono">{warning.rejected_keys.join(', ')}</span>
            </p>
          ))}
        </div>
      )}

      {paragraphs.map((paragraph, index) => (
        <ParagraphEditor
          key={index}
          index={index}
          text={paragraph.text}
          keys={paragraph.keys}
          whitelist={whitelist}
          weakKeys={weakKeys}
          projectId={projectId}
          sectionKey={section.key}
          onTextChange={(text) => updateParagraph(index, { text })}
          onKeysChange={(keys) => updateParagraph(index, { keys })}
          onRemove={() => removeParagraph(index)}
        />
      ))}

      <Button variant="outline" size="sm" onClick={addParagraph}>
        新增段落
      </Button>
    </div>
  );
}

function ParagraphEditor({
  index,
  text,
  keys,
  whitelist,
  weakKeys,
  projectId,
  sectionKey,
  onTextChange,
  onKeysChange,
  onRemove,
}: {
  index: number;
  text: string;
  keys: string[];
  whitelist: string[];
  weakKeys: Set<string>;
  projectId?: string;
  sectionKey: string;
  onTextChange: (text: string) => void;
  onKeysChange: (keys: string[]) => void;
  onRemove: () => void;
}) {
  const [picking, setPicking] = React.useState(false);
  const [refining, setRefining] = React.useState<RefineAction | null>(null);
  const [refineNote, setRefineNote] = React.useState<string | null>(null);
  const editor = useEditor({
    extensions: [StarterKit.configure({ heading: false })],
    content: `<p>${escapeHtml(text)}</p>`,
    editorProps: {
      attributes: {
        class:
          'prose prose-sm max-w-none min-h-[80px] rounded-md border bg-background px-3 py-2 ' +
          'focus:outline-none focus:ring-1 focus:ring-ring',
      },
    },
    immediatelyRender: false,
    onBlur: ({ editor: instance }) => onTextChange(instance.getText().trim()),
  });

  React.useEffect(() => {
    if (editor && editor.getText().trim() !== text) {
      editor.commands.setContent(`<p>${escapeHtml(text)}</p>`);
    }
    // 只在外部内容变化时同步，避免打断输入。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>段落 {index + 1}</span>
        <button onClick={onRemove} className="hover:text-destructive" aria-label="删除段落">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      <EditorContent editor={editor} />

      <div className="flex flex-wrap items-center gap-1.5">
        {keys.map((key) => (
          <Badge
            key={key}
            variant={weakKeys.has(key) ? 'warning' : 'success'}
            className="font-mono text-[11px]"
            title={weakKeys.has(key) ? '语义软校验：与该处论述相关性偏低' : undefined}
          >
            {key}
            <button
              className="ml-1"
              aria-label={`移除引用 ${key}`}
              onClick={() => onKeysChange(keys.filter((k) => k !== key))}
            >
              <X className="h-3 w-3" />
            </button>
          </Badge>
        ))}
        <Button variant="ghost" size="sm" onClick={() => setPicking((v) => !v)}>
          <Quote className="h-3.5 w-3.5" /> 插入引用
        </Button>

        {projectId && (
          <span className="ml-auto flex items-center gap-1">
            {REFINE_ACTIONS.map(({ action, label }) => (
              <Button
                key={action}
                variant="ghost"
                size="sm"
                disabled={!!refining || !text.trim()}
                onClick={async () => {
                  setRefining(action);
                  setRefineNote(null);
                  const result = await refineText(projectId, sectionKey, action, text);
                  setRefining(null);
                  if (!result.data) {
                    setRefineNote('后端不可用：润色未执行');
                    return;
                  }
                  if (result.data.changed) onTextChange(result.data.refined);
                  setRefineNote(result.data.note ?? null);
                }}
              >
                {refining === action ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Sparkles className="h-3.5 w-3.5" />
                )}
                {label}
              </Button>
            ))}
          </span>
        )}
      </div>

      {refineNote && <p className="text-xs text-warning">{refineNote}</p>}

      {picking && (
        <CiteKeyPicker
          whitelist={whitelist}
          selected={keys}
          onChange={onKeysChange}
          compact
        />
      )}
    </div>
  );
}

type SimpleParagraph = { text: string; keys: string[] };

function normalizeBlocks(blocks: IRParagraph[] | undefined): SimpleParagraph[] {
  if (!blocks || blocks.length === 0) return [{ text: '', keys: [] }];
  return blocks.map((block) => {
    const runs = block.runs ?? [];
    const text = runs
      .filter((run): run is Extract<IRRun, { t: 'text' }> => run.t === 'text')
      .map((run) => run.v)
      .join('');
    const keys = runs
      .filter((run): run is Extract<IRRun, { t: 'cite' }> => run.t === 'cite')
      .flatMap((run) => run.keys);
    return { text, keys };
  });
}

function toIRParagraph(paragraph: SimpleParagraph): IRParagraph {
  const runs: IRRun[] = [{ t: 'text', v: paragraph.text }];
  if (paragraph.keys.length > 0) runs.push({ t: 'cite', keys: paragraph.keys });
  return { type: 'paragraph', runs };
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
