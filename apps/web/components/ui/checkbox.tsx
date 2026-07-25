import * as React from 'react';
import { Check } from 'lucide-react';
import { cn } from '@/lib/utils';

const BOX_CLASS =
  'flex h-4 w-4 shrink-0 items-center justify-center rounded border border-input ' +
  'transition-colors';

interface CheckboxIndicatorProps {
  checked?: boolean;
  disabled?: boolean;
  className?: string;
}

/**
 * 纯展示的勾选方框。
 *
 * 当整行本身已经是可交互控件（button / label）时用它：嵌套 <button> 既是非法 HTML
 * （React hydration 会报错），也会造成两个重叠的可交互控件，键盘与读屏用户会被绕进去。
 */
export function CheckboxIndicator({
  checked = false,
  disabled,
  className,
}: CheckboxIndicatorProps) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        BOX_CLASS,
        checked && 'border-primary bg-primary text-primary-foreground',
        disabled && 'opacity-50',
        className,
      )}
    >
      {checked && <Check className="h-3 w-3" strokeWidth={3} />}
    </span>
  );
}

interface CheckboxProps {
  checked?: boolean;
  onCheckedChange?: (checked: boolean) => void;
  disabled?: boolean;
  className?: string;
  'aria-label'?: string;
}

/** 独立的勾选控件。若外层已经是按钮，请改用 CheckboxIndicator。 */
export function Checkbox({
  checked = false,
  onCheckedChange,
  disabled,
  className,
  ...rest
}: CheckboxProps) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onCheckedChange?.(!checked)}
      className={cn(
        BOX_CLASS,
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50',
        checked && 'border-primary bg-primary text-primary-foreground',
        className,
      )}
      {...rest}
    >
      {checked && <Check className="h-3 w-3" strokeWidth={3} />}
    </button>
  );
}
