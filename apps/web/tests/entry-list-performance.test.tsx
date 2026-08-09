import * as React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { EntryList } from '@/components/library/entry-list';
import type { LibraryEntry } from '@/lib/types';

function entry(index: number): LibraryEntry {
  return {
    id: `entry-${index}`,
    status: 'candidate',
    relevance_score: 1 - index / 1000,
    added_via: 'search',
    bibtex_key: `Paper${index}`,
    work: {
      id: `work-${index}`,
      canonical_title: `性能测试文献 ${index}`,
      authors: ['Researcher'],
      publication_year: 2026,
    },
  } as LibraryEntry;
}

describe('文献虚拟列表性能边界', () => {
  it('330 条数据只渲染视口与 overscan 行', () => {
    render(
      <EntryList
        entries={Array.from({ length: 330 }, (_, index) => entry(index))}
        bulkSelectedIds={new Set()}
        onToggleBulk={vi.fn()}
        onToggleIncluded={vi.fn()}
        onOpen={vi.fn()}
        emptyHint="无"
      />,
    );

    expect(screen.getByRole('list', { name: '研究语境文献列表' })).toBeInTheDocument();
    expect(screen.getAllByRole('listitem').length).toBeLessThanOrEqual(12);
    expect(screen.getByText('性能测试文献 0')).toBeInTheDocument();
    expect(screen.queryByText('性能测试文献 329')).not.toBeInTheDocument();
  });
});
