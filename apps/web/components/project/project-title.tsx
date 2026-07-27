'use client';

import * as React from 'react';
import { Check, Loader2, Pencil, X } from 'lucide-react';
import { updateProject } from '@/lib/api';
import { describeError } from '@/lib/errors';
import { useToast } from '@/components/ui/toast';
import { cn } from '@/lib/utils';

/**
 * 可就地改名的论文题目。
 *
 * 项目建出来之后题目此前是只读的——创建向导是全仓库唯一的写入口。首页改成
 * 「一句话研究意图」起步之后这就是硬伤：题目是从那句话推导出来的，不可改
 * 等于把用户锁死在一个凑合的题目上（docs/ui-design.md §3.2）。
 *
 * 就地编辑而不是弹窗：题目是论文的一部分，改它应该像改正文一样，
 * 不该是「打开一个设置对话框」（原则 01 Paper is the interface）。
 */
export function ProjectTitle({
  projectId,
  title,
  onRenamed,
}: {
  projectId: string;
  title: string;
  /** 落库成功后把新题目回灌给项目上下文。 */
  onRenamed: (title: string) => void;
}) {
  const { toast } = useToast();
  const [editing, setEditing] = React.useState(false);
  const [value, setValue] = React.useState(title);
  const [saving, setSaving] = React.useState(false);
  const inputRef = React.useRef<HTMLInputElement>(null);

  // 外部题目变了（切项目、任务刷新）时同步，但不要打断正在编辑的输入。
  React.useEffect(() => {
    if (!editing) setValue(title);
  }, [title, editing]);

  React.useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  const cancel = () => {
    setValue(title);
    setEditing(false);
  };

  const commit = async () => {
    const next = value.trim();
    if (!next) {
      toast({ title: '题目不能为空', variant: 'error' });
      inputRef.current?.focus();
      return;
    }
    if (next === title) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      const updated = await updateProject(projectId, { title: next });
      onRenamed(updated.title);
      setEditing(false);
      toast({ title: '题目已更新', variant: 'success' });
    } catch (err) {
      // 失败时**保持在编辑态**：把用户刚敲的题目丢回只读视图等于让他白打一遍。
      toast({ title: '改名未保存', description: describeError(err), variant: 'error' });
    } finally {
      setSaving(false);
    }
  };

  if (!editing) {
    return (
      <div className="group flex min-w-0 items-start gap-1.5">
        <h1 className="font-serif text-xl font-semibold leading-snug tracking-tight">{title}</h1>
        <button
          type="button"
          onClick={() => setEditing(true)}
          aria-label="修改论文题目"
          className={cn(
            'flex h-11 w-11 shrink-0 items-center justify-center rounded text-muted-foreground transition-opacity transition-colors',
            'hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            // 常驻但压暗，hover/聚焦时补满。
            //
            // 不用 `opacity-0 group-hover:opacity-100`：那样在触屏上没有 hover 态，
            // 改名入口直接不可达；而且 opacity:0 的控件会被无障碍树过滤掉
            // （实测读页面时这个按钮整个不出现）。压到 40% 已经足够安静。
            'opacity-40 group-hover:opacity-100 focus-visible:opacity-100',
          )}
        >
          <Pencil className="h-3.5 w-3.5" />
        </button>
      </div>
    );
  }

  return (
    <div className="flex min-w-0 flex-1 items-center gap-1.5">
      <input
        ref={inputRef}
        value={value}
        autoFocus
        disabled={saving}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') void commit();
          if (e.key === 'Escape') cancel();
        }}
        aria-label="论文题目"
        className="min-w-0 flex-1 rounded-md border bg-background px-2 py-1 font-serif text-xl font-semibold leading-snug tracking-tight focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
      />
      <button
        type="button"
        onClick={() => void commit()}
        disabled={saving}
        aria-label="保存题目"
        className="shrink-0 rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
      </button>
      <button
        type="button"
        onClick={cancel}
        disabled={saving}
        aria-label="取消"
        className="shrink-0 rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
