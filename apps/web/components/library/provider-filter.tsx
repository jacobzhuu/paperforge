'use client';

import { CheckboxIndicator } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import {
  SOURCE_CAPABILITIES,
  type SourceCapabilityId,
  providersFromCapabilities,
} from '@/lib/sourceCapabilities';

export function ProviderFilter({
  selected,
  onChange,
}: {
  selected: SourceCapabilityId[];
  onChange: (ids: SourceCapabilityId[]) => void;
}) {
  const toggle = (id: SourceCapabilityId) =>
    onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);

  const providers = providersFromCapabilities(selected);

  return (
    <section>
      <header className="pb-3">
        <h3 className="text-body">检索源能力</h3>
      </header>
      <div className="space-y-2">
        {SOURCE_CAPABILITIES.map((cap) => {
          const active = selected.includes(cap.id);
          return (
            <button
              key={cap.id}
              type="button"
              role="checkbox"
              aria-checked={active}
              onClick={() => toggle(cap.id)}
              className={cn(
                'flex w-full items-start gap-2 rounded-md border px-3 py-2 text-left transition-colors',
                active ? 'border-primary/50 bg-accent/40' : 'hover:bg-accent/20',
              )}
            >
              {/* 整行已是 checkbox 控件，方框只做展示——避免 button 嵌套 button。 */}
              <CheckboxIndicator checked={active} className="mt-0.5" />
              <div className="min-w-0">
                <div className="text-body font-medium">{cap.label}</div>
                <div className="text-meta text-muted-foreground">{cap.description}</div>
              </div>
            </button>
          );
        })}
        <div className="flex flex-wrap gap-1 border-t pt-3">
          {providers.length === 0 ? (
            <span className="text-meta text-muted-foreground">未选择检索源</span>
          ) : (
            providers.map((p) => (
              // 最小字号统一到 12px（text-meta）；此前这里是 text-[10px]。
              <Badge key={p} variant="secondary" className="font-mono text-meta">
                {p}
              </Badge>
            ))
          )}
        </div>
      </div>
    </section>
  );
}
