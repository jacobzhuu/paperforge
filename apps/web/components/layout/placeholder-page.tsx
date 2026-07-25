import * as React from 'react';
import { Construction } from 'lucide-react';
import { PageHeader } from './page-header';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';

interface PlaceholderPageProps {
  title: string;
  description: string;
  milestone?: string;
  features: string[];
}

export function PlaceholderPage({ title, description, milestone, features }: PlaceholderPageProps) {
  return (
    <div className="space-y-6">
      <PageHeader
        title={title}
        description={description}
        actions={milestone ? <Badge variant="secondary">{milestone}</Badge> : undefined}
      />
      <Card>
        <CardContent className="flex flex-col items-center gap-5 py-14">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted">
            <Construction className="h-6 w-6 text-muted-foreground" />
          </div>
          <div className="text-center">
            <p className="font-medium">页面开发中</p>
            <p className="text-sm text-muted-foreground">该页在后续里程碑落地。规划中的能力：</p>
          </div>
          <ul className="w-full max-w-md space-y-2">
            {features.map((f) => (
              <li key={f} className="flex items-start gap-2 text-sm">
                <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
                <span>{f}</span>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
