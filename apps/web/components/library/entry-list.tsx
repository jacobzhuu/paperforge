'use client';

import * as React from 'react';
import { Quote } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { ADDED_VIA_LABEL, LIBRARY_ACTION } from '@/lib/labels';
import { sectionsCiting, type CiteKeyUsage } from '@/lib/citation-usage';
import type { LibraryEntry } from '@/lib/types';
import { cn } from '@/lib/utils';

/**
 * 固定行高，虚拟化的前提。也让扫读有稳定的节奏。
 *
 * 76 → 92：每行多了一句「这篇为什么在这儿」（被引用于哪几章 / 排序理由 / 来源）。
 * 三行都常驻而不是按需增高，是为了保住定高虚拟化——变高行要么维护偏移表，
 * 要么每次滚动都测量，对一个几十行的组件不划算。
 */
const ROW_HEIGHT = 92;
/** 视口上下各多渲染几行，滚动时不至于露白。 */
const OVERSCAN = 6;

/**
 * 虚拟化文献列表。
 *
 * 此前是一张把**全部**条目一次性渲染出来的 `<table>`：实测一个项目有 330 条
 * （284 候选 + 46 已入库），页面高度 20 558px、DOM 里 330 个 `<tr>`，
 * 而筛选 Tabs、搜索框与右栏统统随页面滚走。分诊 284 条候选意味着
 * 在两万像素里逐条点击，且判断依据（标题）被 `line-clamp-1` 截断。
 *
 * 这里只渲染视口内的行，滚动容器自己有高度，筛选与批量操作条因此能常驻。
 * 刻意不引入虚拟列表依赖——固定行高的场景几十行代码就够。
 */
export function EntryList({
  entries,
  selectedIds,
  activeId,
  usage,
  onToggleOne,
  onOpen,
  emptyHint,
}: {
  entries: LibraryEntry[];
  selectedIds: Set<string>;
  activeId?: string | null;
  /** cite-key → 引用它的章节标题，见 lib/citation-usage.ts。 */
  usage?: CiteKeyUsage;
  onToggleOne: (entry: LibraryEntry, index: number, shiftKey: boolean) => void;
  onOpen: (entry: LibraryEntry) => void;
  emptyHint: React.ReactNode;
}) {
  const containerRef = React.useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = React.useState(0);
  const [viewportHeight, setViewportHeight] = React.useState(600);

  React.useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const measure = () => setViewportHeight(el.clientHeight);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // 筛选条件变化后回到顶部，否则会停在一个空白的滚动位置。
  React.useEffect(() => {
    containerRef.current?.scrollTo({ top: 0 });
    setScrollTop(0);
  }, [entries.length]);

  const total = entries.length;
  const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const end = Math.min(total, Math.ceil((scrollTop + viewportHeight) / ROW_HEIGHT) + OVERSCAN);
  const visible = entries.slice(start, end);

  if (total === 0) {
    return (
      <div className="rounded-xl border border-dashed py-16 text-center text-sm text-muted-foreground">
        {emptyHint}
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
      className="scrollbar-thin max-h-[calc(100vh-18rem)] min-h-80 overflow-y-auto border-y"
      role="listbox"
      aria-label="研究语境文献列表"
      aria-multiselectable
    >
      <div style={{ height: total * ROW_HEIGHT, position: 'relative' }}>
        <div style={{ transform: `translateY(${start * ROW_HEIGHT}px)` }}>
          {visible.map((entry, i) => {
            const index = start + i;
            return (
              <EntryRow
                key={entry.id}
                entry={entry}
                index={index}
                selected={selectedIds.has(entry.id)}
                active={entry.id === activeId}
                citedIn={usage ? sectionsCiting(usage, entry.bibtex_key) : []}
                onToggle={(shiftKey) => onToggleOne(entry, index, shiftKey)}
                onOpen={() => onOpen(entry)}
              />
            );
          })}
        </div>
      </div>
    </div>
  );
}

function EntryRow({
  entry,
  index,
  selected,
  active,
  citedIn,
  onToggle,
  onOpen,
}: {
  entry: LibraryEntry;
  index: number;
  selected: boolean;
  active: boolean;
  citedIn: string[];
  onToggle: (shiftKey: boolean) => void;
  onOpen: () => void;
}) {
  const work = entry.work;
  const meta = [
    work.authors.slice(0, 3).join(', ') + (work.authors.length > 3 ? ' 等' : ''),
    work.publication_year ?? '—',
    work.venue_name,
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <div
      style={{ height: ROW_HEIGHT }}
      role="option"
      aria-selected={selected}
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          onOpen();
        }
        if (e.key === ' ') {
          e.preventDefault();
          onToggle(e.shiftKey);
        }
      }}
      className={cn(
        'flex cursor-pointer items-center gap-3 border-b px-3 transition-colors last:border-b-0',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring',
        active ? 'bg-accent/60' : selected ? 'bg-secondary/50' : 'hover:bg-muted/50',
      )}
    >
      <div
        onClick={(e) => {
          e.stopPropagation();
          onToggle((e as React.MouseEvent).shiftKey);
        }}
        className="shrink-0"
      >
        <Checkbox
          checked={entry.status === 'selected'}
          onCheckedChange={() => {
            /* 由外层 onClick 处理，以便拿到 shiftKey */
          }}
          aria-label={`${entry.status === 'selected' ? LIBRARY_ACTION.deselect : LIBRARY_ACTION.select}：${work.canonical_title}`}
        />
      </div>

      <div className="min-w-0 flex-1">
        {/* 标题是分诊时唯一的判断依据，给两行而不是一行。 */}
        <div className="flex items-start gap-2">
          <span className="line-clamp-2 text-sm font-medium leading-snug">
            {work.canonical_title}
          </span>
          {work.is_retracted && (
            <Badge variant="destructive" className="shrink-0">
              撤稿
            </Badge>
          )}
        </div>
        <div className="mt-0.5 line-clamp-1 text-xs text-muted-foreground">{meta}</div>
        <Provenance entry={entry} citedIn={citedIn} />
      </div>

      <span className="sr-only">第 {index + 1} 条</span>
    </div>
  );
}

/**
 * 「这篇为什么在这儿」——每行的第三句（docs/ui-design.md §3.6）。
 *
 * 三级回退，按对用户的价值排序：
 *  1. 已被正文引用 → 说出引用在哪几章。这是最强的答案：它已经在你的论文里了。
 *  2. 还没被引用但有排序理由 → 说出检索器为什么把它排上来。此前 `rank_reason`
 *     只在抽屉里，得逐条点开才看得到，等于没有。
 *  3. 都没有 → 退回来源（检索 / 雪球 / 导入）。
 *
 * 原来这一列的位置是「来源」独占一列固定 80px；把它降级成回退项之后，
 * 标题能多拿一截宽度——分诊时标题才是判断依据。
 */
function Provenance({ entry, citedIn }: { entry: LibraryEntry; citedIn: string[] }) {
  if (citedIn.length > 0) {
    // 章节多时只列前三章，其余折成「等 N 章」，避免一行被章节名撑爆。
    const shown = citedIn.slice(0, 3).join('、');
    const rest = citedIn.length - 3;
    return (
      <div className="mt-1 flex items-center gap-1.5 text-xs text-foreground">
        <Quote className="h-3 w-3 shrink-0 text-muted-foreground" />
        <span className="truncate">
          被引用于 {shown}
          {rest > 0 && ` 等 ${citedIn.length} 章`}
        </span>
      </div>
    );
  }
  if (entry.rank_reason) {
    return (
      <div className="mt-1 line-clamp-1 text-xs text-muted-foreground">
        {entry.rank_reason}
      </div>
    );
  }
  return (
    <div className="mt-1 text-xs text-muted-foreground">
      来自{ADDED_VIA_LABEL[entry.added_via]}
    </div>
  );
}
