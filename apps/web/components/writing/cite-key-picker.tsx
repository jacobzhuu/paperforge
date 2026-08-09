'use client';

import * as React from 'react';
import { Check, Search } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

/**
 * 引用键选择器：取值**只能**来自写作白名单（R1/R2 的前端保障，设计 §4.4.3）。
 * 组件不接受自由输入——物理上无法手写出一个幻觉引用。
 */
export function CiteKeyPicker({
  whitelist,
  selected,
  onChange,
  compact = false,
}: {
  whitelist: string[];
  selected: string[];
  onChange: (keys: string[]) => void;
  compact?: boolean;
}) {
  const [query, setQuery] = React.useState('');
  const selectedSet = React.useMemo(() => new Set(selected), [selected]);

  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = q ? whitelist.filter((key) => key.toLowerCase().includes(q)) : whitelist;
    return list.slice(0, compact ? 40 : 200);
  }, [whitelist, query, compact]);

  const toggle = (key: string) => {
    if (selectedSet.has(key)) {
      onChange(selected.filter((k) => k !== key));
    } else {
      onChange([...selected, key]);
    }
  };

  if (whitelist.length === 0) {
    return (
      <p className="rounded-md bg-muted/40 px-2 py-1.5 text-xs text-muted-foreground">
        写作白名单为空：先在文献工作台勾选入库文献（需通过核验并分配引用键）。
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <div className="relative">
        <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="过滤引用键"
          className="pl-8 md:h-9"
        />
      </div>
      <div
        className={cn(
          'flex flex-wrap gap-1 overflow-y-auto rounded-md border p-2',
          compact ? 'max-h-28' : 'max-h-40',
        )}
      >
        {filtered.map((key) => {
          const active = selectedSet.has(key);
          return (
            <button
              key={key}
              type="button"
              onClick={() => toggle(key)}
              aria-pressed={active}
              className="rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Badge
                variant={active ? 'success' : 'outline'}
                className={cn('cursor-pointer font-mono text-xs', active && 'pr-1.5')}
              >
                {key}
                {active && <Check className="ml-0.5 h-3 w-3" />}
              </Badge>
            </button>
          );
        })}
        {filtered.length === 0 && (
          <span className="text-xs text-muted-foreground">无匹配引用键</span>
        )}
      </div>
    </div>
  );
}
