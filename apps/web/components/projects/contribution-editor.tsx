'use client';

import { Plus, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';

export function ContributionEditor({
  points,
  onChange,
}: {
  points: string[];
  onChange: (points: string[]) => void;
}) {
  return (
    <div className="space-y-2 sm:col-span-2">
      <Label>贡献点（研究型论文）</Label>
      <p className="text-meta text-muted-foreground">
        写下方法、数据或结论上最重要的新意；可留空，之后仍能在项目中补充。
      </p>
      {points.map((point, i) => (
        <div key={i} className="flex gap-2">
          <Textarea
            value={point}
            onChange={(event) =>
              onChange(points.map((current, index) => (index === i ? event.target.value : current)))
            }
            placeholder={`贡献点 ${i + 1}`}
            className="min-h-11"
          />
          {points.length > 1 && (
            <Button
              variant="ghost"
              size="icon"
              onClick={() => onChange(points.filter((_, index) => index !== i))}
              aria-label={`删除贡献点 ${i + 1}`}
            >
              <X />
            </Button>
          )}
        </div>
      ))}
      <Button variant="outline" size="sm" onClick={() => onChange([...points, ''])}>
        <Plus /> 添加贡献点
      </Button>
    </div>
  );
}
