'use client';

import { CheckCircle2, XCircle, Loader2 } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import type { SearchRun } from '@/lib/types';

export function SearchStats({ runs }: { runs: SearchRun[] }) {
  const totalHits = runs.reduce((s, r) => s + r.hit_count, 0);
  const totalRetrieved = runs.reduce((s, r) => s + r.retrieved_count, 0);
  const failed = runs.filter((r) => r.status === 'failed').length;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm">检索统计</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-3 gap-2 text-center">
          <Stat label="命中" value={totalHits.toLocaleString()} />
          <Stat label="召回" value={totalRetrieved.toLocaleString()} />
          <Stat label="失败源" value={String(failed)} tone={failed ? 'warn' : undefined} />
        </div>
        <ul className="space-y-1.5 border-t pt-3">
          {runs.map((r) => (
            <li key={r.id} className="flex items-center gap-2 text-xs">
              {r.status === 'succeeded' ? (
                <CheckCircle2 className="h-3.5 w-3.5 text-success" />
              ) : r.status === 'partial' ? (
                <CheckCircle2 className="h-3.5 w-3.5 text-warning" />
              ) : r.status === 'failed' ? (
                <XCircle className="h-3.5 w-3.5 text-destructive" />
              ) : (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
              )}
              <span className="font-medium">{r.provider}</span>
              <span className="ml-auto text-muted-foreground">
                {r.status === 'failed' ? r.error : `${r.retrieved_count}/${r.hit_count}`}
              </span>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'warn' }) {
  return (
    <div>
      <div className={tone === 'warn' ? 'text-lg font-semibold text-warning-foreground' : 'text-lg font-semibold'}>
        {value}
      </div>
      <div className="text-[11px] text-muted-foreground">{label}</div>
    </div>
  );
}
