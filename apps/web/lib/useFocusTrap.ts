'use client';

import * as React from 'react';

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
  '[contenteditable="true"]',
].join(',');

/**
 * 模态焦点管理。
 *
 * Dialog / Drawer 此前只有 Escape 关闭：没有 focus trap，键盘用户可以 Tab 到
 * 对话框**背后**的页面并在看不见焦点环的情况下操作它；关闭后焦点也不归还，
 * 读屏用户会被扔回文档开头。四步新建向导受影响最明显。
 */
export function useFocusTrap(open: boolean, ref: React.RefObject<HTMLElement | null>): void {
  React.useEffect(() => {
    if (!open) return;
    const container = ref.current;
    if (!container) return;

    const previouslyFocused = document.activeElement as HTMLElement | null;

    // 打开时把焦点送进对话框：优先第一个可聚焦元素，否则聚焦容器本身。
    const focusFirst = () => {
      const targets = container.querySelectorAll<HTMLElement>(FOCUSABLE);
      if (targets.length > 0) targets[0].focus();
      else container.focus();
    };
    const raf = requestAnimationFrame(focusFirst);

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;
      const targets = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );
      if (targets.length === 0) {
        e.preventDefault();
        return;
      }
      const first = targets[0];
      const last = targets[targets.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', onKeyDown);

    // 背景滚动锁：模态打开时滚轮不该带动身后的长列表。
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousOverflow;
      previouslyFocused?.focus?.();
    };
  }, [open, ref]);
}

/** 生成稳定的元素 id，用于 aria-labelledby / aria-describedby。 */
export function useId(prefix: string): string {
  const reactId = React.useId();
  return `${prefix}-${reactId.replace(/:/g, '')}`;
}
