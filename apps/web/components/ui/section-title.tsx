import * as React from 'react';
import { cn } from '@/lib/utils';

export function SectionTitle({
  children,
  className,
  ...props
}: React.HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h2
      className={cn(
        'text-meta font-semibold uppercase tracking-wider text-muted-foreground',
        className,
      )}
      {...props}
    >
      {children}
    </h2>
  );
}
