import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

const calloutVariants = cva('rounded-md border px-3 py-2 text-meta', {
  variants: {
    variant: {
      info: 'border-transparent bg-muted/40 text-muted-foreground',
      warning: 'border-warning/40 bg-warning/10 text-warning-foreground',
      error: 'border-destructive/40 bg-destructive/10 text-destructive-strong',
    },
  },
  defaultVariants: { variant: 'info' },
});

export interface CalloutProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof calloutVariants> {}

export function Callout({ className, variant, ...props }: CalloutProps) {
  return <div className={cn(calloutVariants({ variant }), className)} {...props} />;
}

export { calloutVariants };
