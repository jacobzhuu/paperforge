import * as React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Button } from '@/components/ui/button';

describe('Button 统一加载态', () => {
  it('进行中自动禁用、暴露 aria-busy，并阻止重复提交', () => {
    const onClick = vi.fn();
    render(
      <Button loading loadingLabel="正在保存…" onClick={onClick}>
        保存
      </Button>,
    );

    const button = screen.getByRole('button', { name: '正在保存…' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
    fireEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('没有替代文案时保留原文，避免按钮宽度跳动', () => {
    render(<Button loading>上传并识别</Button>);
    expect(screen.getByRole('button', { name: '上传并识别' })).toBeDisabled();
  });
});
