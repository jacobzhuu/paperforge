'use client';

import * as React from 'react';
import { EditorContent, useEditor, type Editor } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import { Bold, Image as ImageIcon, Italic, List, ListOrdered, Loader2, Quote, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { CiteKeyPicker } from '@/components/writing/cite-key-picker';
import { CiteChip } from '@/components/writing/extensions/cite-chip';
import {
  FigureBlockNode,
  FigureXref,
  IrBlock,
  MathInline,
} from '@/components/writing/extensions/ir-block';
import { irToTiptap, tiptapToIR, type TiptapDoc } from '@/lib/ir-serde';
import type { RefineAction, SectionIR, SoftCheckFinding, VisualAsset } from '@/lib/types';
import { cn } from '@/lib/utils';

const REFINE_ACTIONS: { action: RefineAction; label: string }[] = [
  { action: 'polish', label: '润色' },
  { action: 'expand', label: '扩写' },
  { action: 'shorten', label: '缩写' },
  { action: 'academic_tone', label: '学术语气' },
];

export interface RefineRequest {
  action: RefineAction;
  /** 选区纯文本；由调用方发给 refine 端点并回显 diff。 */
  text: string;
  /** 用户接受改写后写回选区。 */
  apply: (replacement: string) => void;
}

/**
 * 章节编辑器：**整节一个** Tiptap 实例。
 *
 * 此前是每个段落一个独立实例、每段一张带边框的卡片、失焦时 `getText()` 把内容
 * 压成纯文本；引用在段落下方的 chip 带里，保存时全部塞到段落末尾。结果是
 * 既不能跨段落选中与通读，引用也和它支撑的句子脱钩。
 *
 * 现在正文是一份连续文档：引用是行内原子 chip（位置即语义位置），
 * 公式/图/表/算法/todo 以只读块原样保留（见 extensions/ir-block.ts）。
 *
 * 加粗/斜体/列表由 PaperIR 的 `TextRun.marks` 与 `ListBlock` 结构化承载，
 * 渲染器据此确定性展开 \textbf{} / \emph{} / itemize / enumerate——
 * 强调是**标记**不是 LLM 写的 LaTeX，因此不触碰「自由 LaTeX 只允许出现在
 * equation/algorithm」这条边界（设计 §4.5）。
 *
 * 仍然不提供标题：章节层级由大纲决定，正文里再开标题会和大纲打架。
 */
export function SectionEditor({
  section,
  whitelist,
  onChange,
  softChecks = [],
  onRefine,
  refining,
  readOnly = false,
  visuals = [],
}: {
  section: SectionIR;
  whitelist: string[];
  onChange: (next: SectionIR) => void;
  softChecks?: SoftCheckFinding[];
  onRefine?: (request: RefineRequest) => void;
  refining?: RefineAction | null;
  readOnly?: boolean;
  visuals?: VisualAsset[];
}) {
  const [picking, setPicking] = React.useState(false);
  const [pickingFigure, setPickingFigure] = React.useState(false);
  const [selection, setSelection] = React.useState<{ top: number; left: number } | null>(null);

  const weakKeys = React.useMemo(
    () => new Set(softChecks.filter((f) => f.weak).map((f) => f.cite_key)),
    [softChecks],
  );

  // section 换了才重建文档；否则每次按键都会重置光标。
  const sectionKey = section.key;
  const latestSection = React.useRef(section);
  latestSection.current = section;

  const editor = useEditor(
    {
      extensions: [
        StarterKit.configure({
          // 关掉 IR 无法表示的东西——留着就等于承诺一个保存时会丢的功能。
          // bold / italic / bulletList / orderedList / listItem 现在 IR 有对应
          // 结构（TextRun.marks 与 ListBlock），因此保留。
          blockquote: false,
          codeBlock: false,
          code: false,
          heading: false,
          horizontalRule: false,
          hardBreak: false,
          strike: false,
        }),
        CiteChip.configure({ weakKeys }),
        MathInline,
        FigureXref,
        FigureBlockNode.configure({
          previewUrls: Object.fromEntries(
            visuals
              .filter((visual) => visual.renditions.png?.url)
              .map((visual) => [visual.asset_ref, visual.renditions.png!.url]),
          ),
          aiAssetRefs: new Set(
            visuals.filter((visual) => visual.kind === 'ai_image').map((visual) => visual.asset_ref),
          ),
        }),
        IrBlock,
      ],
      content: irToTiptap(section),
      editable: !readOnly,
      editorProps: {
        attributes: {
          class: cn(
            'pf-prose min-h-[40vh] focus:outline-none',
            readOnly && 'opacity-80',
          ),
          'aria-label': `章节正文：${section.title}`,
          role: 'textbox',
          'aria-multiline': 'true',
        },
      },
      immediatelyRender: false,
      onUpdate: ({ editor: instance }) => {
        const doc = instance.getJSON() as unknown as TiptapDoc;
        onChange(tiptapToIR(doc, latestSection.current));
      },
      onSelectionUpdate: ({ editor: instance }) => {
        setSelection(selectionAnchor(instance));
      },
      onBlur: () => setSelection(null),
    },
    [sectionKey, readOnly],
  );

  // 软校验结果晚于正文到达时刷新 chip 配色。
  React.useEffect(() => {
    if (!editor) return;
    const ext = editor.extensionManager.extensions.find((e) => e.name === CiteChip.name);
    if (ext) {
      ext.options.weakKeys = weakKeys;
      editor.view.dispatch(editor.state.tr);
    }
  }, [editor, weakKeys]);

  const insertCitation = (keys: string[]) => {
    if (!editor || keys.length === 0) return;
    editor.chain().focus().insertContent({ type: CiteChip.name, attrs: { keys } }).run();
    setPicking(false);
  };

  const approvedVisuals = visuals.filter((visual) => visual.review_status === 'approved');

  const insertFigureXref = (visual: VisualAsset) => {
    if (!editor) return;
    editor
      .chain()
      .focus()
      .insertContent({ type: FigureXref.name, attrs: { target: visual.figure_label, kind: 'figure' } })
      .run();
    setPickingFigure(false);
  };

  const selectedText = editor
    ? editor.state.doc.textBetween(editor.state.selection.from, editor.state.selection.to, ' ')
    : '';
  const hasSelection = selectedText.trim().length > 0;

  return (
    <div className="relative space-y-3">
      {section.citation_warnings?.length > 0 && (
        <div className="space-y-1 rounded-md border border-warning/50 bg-warning/10 p-3 text-xs">
          <p className="font-medium text-warning-foreground">
            R2 二次校验移除了越权引用（正文其余部分未受影响）
          </p>
          {section.citation_warnings.map((warning, index) => (
            <p key={index} className="text-muted-foreground">
              {warning.message}
              <span className="ml-1 font-mono">{warning.rejected_keys.join(', ')}</span>
            </p>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1 border-b pb-2">
        <Button
          variant="ghost"
          size="sm"
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => setPicking((v) => !v)}
          disabled={readOnly}
          aria-expanded={picking}
        >
          <Quote className="h-3.5 w-3.5" /> 插入引用
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => setPickingFigure((value) => !value)}
          disabled={readOnly || approvedVisuals.length === 0}
          aria-expanded={pickingFigure}
        >
          <ImageIcon className="h-3.5 w-3.5" /> 插入图引用
        </Button>

        <span aria-hidden className="mx-1 h-4 w-px bg-border" />

        <MarkButton
          editor={editor}
          disabled={readOnly}
          active={editor?.isActive('bold') ?? false}
          label="加粗"
          onClick={() => editor?.chain().focus().toggleBold().run()}
        >
          <Bold className="h-3.5 w-3.5" />
        </MarkButton>
        <MarkButton
          editor={editor}
          disabled={readOnly}
          active={editor?.isActive('italic') ?? false}
          label="斜体"
          onClick={() => editor?.chain().focus().toggleItalic().run()}
        >
          <Italic className="h-3.5 w-3.5" />
        </MarkButton>
        <MarkButton
          editor={editor}
          disabled={readOnly}
          active={editor?.isActive('bulletList') ?? false}
          label="无序列表"
          onClick={() => editor?.chain().focus().toggleBulletList().run()}
        >
          <List className="h-3.5 w-3.5" />
        </MarkButton>
        <MarkButton
          editor={editor}
          disabled={readOnly}
          active={editor?.isActive('orderedList') ?? false}
          label="有序列表"
          onClick={() => editor?.chain().focus().toggleOrderedList().run()}
        >
          <ListOrdered className="h-3.5 w-3.5" />
        </MarkButton>

        <span className="ml-auto text-xs text-muted-foreground">
          {hasSelection ? '选中文字后可用浮条改写' : '引用只能从写作白名单选择'}
        </span>
      </div>

      {picking && (
        <div className="rounded-md border p-2">
          <CiteKeyPicker whitelist={whitelist} selected={[]} onChange={insertCitation} compact />
        </div>
      )}

      {pickingFigure && (
        <div className="rounded-md border p-2">
          <p className="mb-2 text-xs text-muted-foreground">选择已经批准并插入论文的图</p>
          <div className="flex flex-wrap gap-1.5">
            {approvedVisuals.map((visual) => (
              <Button
                key={visual.id}
                type="button"
                variant="outline"
                size="sm"
                onClick={() => insertFigureXref(visual)}
              >
                {visual.caption || visual.title || visual.figure_label}
              </Button>
            ))}
          </div>
        </div>
      )}

      <EditorContent editor={editor} />

      {/* 选区浮动工具条：取代此前钉在每个段落底部的四个按钮。 */}
      {onRefine && hasSelection && selection && !readOnly && (
        <div
          className="absolute z-20 flex items-center gap-0.5 rounded-md border bg-popover p-1 shadow-lg animate-fade-in"
          style={{ top: Math.max(0, selection.top - 44), left: selection.left }}
          role="toolbar"
          aria-label="改写选中文字"
        >
          {REFINE_ACTIONS.map(({ action, label }) => (
            <Button
              key={action}
              variant="ghost"
              size="sm"
              disabled={!!refining}
              // 同上：保住选区，否则拿不到用户实际选中的那段文字。
              onMouseDown={(e) => e.preventDefault()}
              onClick={() =>
                onRefine({
                  action,
                  text: selectedText,
                  apply: (replacement) => {
                    if (!editor) return;
                    editor
                      .chain()
                      .focus()
                      .insertContentAt(
                        { from: editor.state.selection.from, to: editor.state.selection.to },
                        replacement,
                      )
                      .run();
                  },
                })
              }
            >
              {refining === action ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Sparkles className="h-3.5 w-3.5" />
              )}
              {label}
            </Button>
          ))}
        </div>
      )}
    </div>
  );
}

/** 排版按钮：按下态用 aria-pressed 暴露，读屏能听出「当前是否加粗」。 */
function MarkButton({
  editor,
  active,
  disabled,
  label,
  onClick,
  children,
}: {
  editor: Editor | null;
  active: boolean;
  disabled?: boolean;
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <Button
      variant="ghost"
      size="sm"
      aria-label={label}
      title={label}
      aria-pressed={active}
      disabled={disabled || !editor}
      // 关键：不 preventDefault 的话，按下按钮会把焦点从编辑器抢走并**collapse 选区**，
      // 于是「选中一段 → 点加粗」只会给下一个字符预置样式，选中的文字纹丝不动。
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      className={cn('px-2', active && 'bg-accent text-accent-foreground')}
    >
      {children}
    </Button>
  );
}

/** 选区起点相对编辑器容器的位置，用于摆放浮动工具条。 */
function selectionAnchor(editor: Editor): { top: number; left: number } | null {
  const { from, to } = editor.state.selection;
  if (from === to) return null;
  try {
    const start = editor.view.coordsAtPos(from);
    const box = editor.view.dom.getBoundingClientRect();
    return { top: start.top - box.top, left: Math.max(0, start.left - box.left) };
  } catch {
    return null;
  }
}
