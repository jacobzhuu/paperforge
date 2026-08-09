import * as React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AuthPageShell } from '@/components/auth/app-shell';

describe('AuthPageShell', () => {
  it('呈现统一的认证品牌叙事和账户内容', () => {
    render(
      <AuthPageShell>
        <div>认证表单</div>
      </AuthPageShell>,
    );

    expect(screen.getByRole('region', { name: '账户访问' })).toHaveTextContent('认证表单');
    expect(screen.getByRole('complementary', { name: 'PaperForge 产品理念' })).toHaveTextContent(
      '让研究从材料出发，在证据中成稿。',
    );
    expect(screen.getByText('汇入材料')).toBeInTheDocument();
    expect(screen.getByText('循证写作')).toBeInTheDocument();
    expect(screen.getByText('灵活交付')).toBeInTheDocument();
  });

  it('将背景和稿件预览从辅助技术中隐藏', () => {
    const { container } = render(
      <AuthPageShell>
        <div>认证表单</div>
      </AuthPageShell>,
    );

    const hiddenDecorations = container.querySelectorAll('[aria-hidden="true"]');
    expect(hiddenDecorations.length).toBeGreaterThanOrEqual(2);
  });
});
