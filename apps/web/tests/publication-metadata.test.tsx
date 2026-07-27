import * as React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { makeProject, makeToastSpy, ok } from './helpers';
import type { AuthorDetail } from '@/lib/types';

const updateProject = vi.fn();
const getAcademicProfile = vi.fn();
const listProjects = vi.fn();
const toastSpy = makeToastSpy();

vi.mock('@/lib/api', () => ({
  updateProject: (...args: unknown[]) => updateProject(...args),
  getAcademicProfile: (...args: unknown[]) => getAcademicProfile(...args),
  listProjects: (...args: unknown[]) => listProjects(...args),
}));

vi.mock('@/components/ui/toast', () => ({
  useToast: () => ({ toast: toastSpy.toast }),
}));

import { PublicationMetadata } from '@/components/project/publication-metadata';

const authors: AuthorDetail[] = [
  {
    id: 'ada',
    name: 'Ada Lovelace',
    affiliations: ['Analytical Engine Lab'],
    email: null,
    orcid: null,
    corresponding: false,
  },
  {
    id: 'grace',
    name: 'Grace Hopper',
    affiliations: ['Yale University'],
    email: 'grace@example.org',
    orcid: '0000-0002-1825-0097',
    corresponding: true,
  },
];

function project(overrides: Record<string, unknown> = {}) {
  return makeProject({
    publication_title: 'A Reliable Paper',
    authors: authors.map((author) => author.name),
    author_details: authors,
    keywords: ['reliability', 'evidence'],
    metadata_confirmed: true,
    ...overrides,
  });
}

function renderEditor(value = project(), onSaved = vi.fn()) {
  render(<PublicationMetadata project={value} onSaved={onSaved} />);
  fireEvent.click(screen.getByRole('button', { name: '编辑' }));
}

describe('投稿署名编辑器', () => {
  beforeEach(() => {
    updateProject.mockResolvedValue(ok(project()));
    getAcademicProfile.mockResolvedValue({ profile: null });
    listProjects.mockResolvedValue(ok([]));
  });

  it('概览默认只显示紧凑署名，按需展开编辑', () => {
    render(<PublicationMetadata project={project()} onSaved={vi.fn()} />);
    expect(screen.getByText('A Reliable Paper')).toBeInTheDocument();
    expect(screen.getByText('题名与关键词已由摘要准备')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '论文发表题名' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    expect(screen.getByRole('textbox', { name: '论文发表题名' })).toBeInTheDocument();
  });

  it('排序会撤销旧确认，重新确认时保持当前作者顺序', async () => {
    const onSaved = vi.fn();
    renderEditor(project(), onSaved);

    fireEvent.click(screen.getByRole('button', { name: '将 Ada Lovelace 下移' }));
    expect(screen.getByText(/尚未确认/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认用于导出' }));

    await waitFor(() => {
      expect(updateProject).toHaveBeenCalledWith(
        'p1',
        expect.objectContaining({
          metadata_confirmed: true,
          author_details: [
            expect.objectContaining({ name: 'Grace Hopper' }),
            expect.objectContaining({ name: 'Ada Lovelace' }),
          ],
        }),
      );
    });
    expect(onSaved).toHaveBeenCalled();
  });

  it('一键复用账号身份时保存项目快照，不改写原档案 id', async () => {
    getAcademicProfile.mockResolvedValue({
      profile: {
        id: 'account-profile',
        name: 'Lin Chen',
        affiliations: ['Paper Forge Lab'],
        email: 'lin@example.org',
        orcid: null,
        corresponding: false,
      },
    });
    renderEditor(project({ author_details: [], authors: [] }));

    fireEvent.click(screen.getByRole('button', { name: '使用我的学术身份' }));
    expect((await screen.findAllByText('Lin Chen')).length).toBeGreaterThan(0);
    expect(screen.getByRole('dialog', { name: /作者资料/ })).toBeInTheDocument();
  });

  it('从历史项目复用合作者时去重，并保存独立快照', async () => {
    listProjects.mockResolvedValue(ok([
      project(),
      project({
        id: 'older-project',
        updated_at: '2026-07-01T00:00:00Z',
        author_details: [
          authors[0],
          {
            id: 'history-lin',
            name: 'Lin Chen',
            affiliations: ['Paper Forge Lab'],
            email: 'lin@example.org',
            orcid: null,
            corresponding: false,
          },
        ],
      }),
    ]));
    renderEditor(project());

    fireEvent.click(screen.getByRole('button', { name: '最近合作者' }));
    const suggestions = await screen.findByLabelText('最近合作者建议');
    const suggestion = within(suggestions).getByRole('button', { name: /Lin Chen/ });
    expect(within(suggestions).queryByRole('button', { name: /Ada Lovelace/ })).not.toBeInTheDocument();
    fireEvent.click(suggestion);

    expect((await screen.findAllByText('Lin Chen')).length).toBeGreaterThan(0);
  });

  it('通讯作者没有邮箱时阻止确认并给出逐项原因', async () => {
    const invalid = [{ ...authors[0], corresponding: true }];
    renderEditor(project({ author_details: invalid, authors: ['Ada Lovelace'], metadata_confirmed: false }));

    fireEvent.click(screen.getByRole('button', { name: '确认用于导出' }));
    await waitFor(() => {
      expect(toastSpy.toast).toHaveBeenCalledWith(
        expect.objectContaining({ title: '通讯作者 Ada Lovelace 需要填写邮箱' }),
      );
    });
    expect(updateProject).not.toHaveBeenCalled();
  });

  it('摘要完成前只给自动生成提示，不展示题名或关键词输入框', () => {
    render(
      <PublicationMetadata
        project={project({ publication_title: null, keywords: [] })}
        onSaved={vi.fn()}
      />,
    );
    expect(screen.getByText('摘要完成后自动生成题名与关键词')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '论文发表题名' })).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '添加关键词' })).not.toBeInTheDocument();
  });
});
