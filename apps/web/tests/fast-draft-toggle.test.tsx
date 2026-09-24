import * as React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FastDraftToggle } from '@/components/ui/fast-draft-toggle';

afterEach(() => vi.useRealTimers());

describe('FastDraftToggle', () => {
  it('shows the hint after a short hover without changing profile', () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    render(<FastDraftToggle value="standard" onChange={onChange} />);
    const button = screen.getByRole('button', { name: '快速草稿' });
    expect(button).toHaveAttribute('type', 'button');
    expect(button).toHaveAttribute('aria-pressed', 'false');
    fireEvent.pointerEnter(button, { pointerType: 'mouse' });
    act(() => vi.advanceTimersByTime(249));
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1));
    expect(screen.getByRole('tooltip')).toHaveTextContent(
      '更快生成初稿，引用仍需完整核验。',
    );
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.keyDown(button, { key: 'Escape' });
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    fireEvent.pointerLeave(button);
  });

  it('shows the hint on keyboard focus and toggles only on activation', () => {
    const onChange = vi.fn();
    const { rerender } = render(<FastDraftToggle value="standard" onChange={onChange} />);
    const button = screen.getByRole('button', { name: '快速草稿' });
    fireEvent.focus(button);
    expect(screen.getByRole('tooltip')).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.keyDown(button, { key: 'Escape' });
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    fireEvent.click(button);
    expect(onChange).toHaveBeenCalledWith('fast_draft');
    rerender(<FastDraftToggle value="fast_draft" onChange={onChange} />);
    expect(button).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(button);
    expect(onChange).toHaveBeenLastCalledWith('standard');
  });
});
