'use client';

import * as React from 'react';
import {
  ArrowDown,
  ArrowUp,
  Check,
  GripVertical,
  Pencil,
  Plus,
  Trash2,
  UserRoundCheck,
  UsersRound,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Drawer } from '@/components/ui/drawer';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useToast } from '@/components/ui/toast';
import { describeError } from '@/lib/errors';
import { getAcademicProfile, listProjects, updateProject } from '@/lib/api';
import type { AuthorDetail, Project } from '@/lib/types';
import { cn } from '@/lib/utils';

type SaveState = 'idle' | 'waiting' | 'saving' | 'saved' | 'error';

export function PublicationMetadata({
  project,
  onSaved,
  collapsedByDefault = true,
}: {
  project: Project;
  onSaved: () => void;
  collapsedByDefault?: boolean;
}) {
  const { toast } = useToast();
  const [title, setTitle] = React.useState(project.publication_title ?? project.title);
  const [authors, setAuthors] = React.useState<AuthorDetail[]>(() => projectAuthors(project));
  const [keywords, setKeywords] = React.useState(project.keywords ?? []);
  const [confirmed, setConfirmed] = React.useState(project.metadata_confirmed ?? false);
  const [dirty, setDirty] = React.useState(false);
  const [saveState, setSaveState] = React.useState<SaveState>('idle');
  const [newAuthor, setNewAuthor] = React.useState('');
  const [selectedAuthorId, setSelectedAuthorId] = React.useState<string | null>(null);
  const [removed, setRemoved] = React.useState<{ author: AuthorDetail; index: number } | null>(null);
  const [draggedId, setDraggedId] = React.useState<string | null>(null);
  const [editorOpen, setEditorOpen] = React.useState(!collapsedByDefault);
  const [recentAuthors, setRecentAuthors] = React.useState<AuthorDetail[]>([]);
  const [recentOpen, setRecentOpen] = React.useState(false);
  const [recentLoading, setRecentLoading] = React.useState(false);
  const latestSnapshot = React.useRef('');

  React.useEffect(() => {
    setTitle(project.publication_title ?? project.title);
    setAuthors(projectAuthors(project));
    setKeywords(project.keywords ?? []);
    setConfirmed(project.metadata_confirmed ?? false);
    setDirty(false);
    setSaveState('idle');
  }, [project.id, project.updated_at]);

  const snapshot = React.useMemo(
    () => JSON.stringify({ title: title.trim(), authors, keywords }),
    [authors, keywords, title],
  );
  latestSnapshot.current = snapshot;

  const markChanged = React.useCallback(() => {
    setConfirmed(false);
    setDirty(true);
    setSaveState('waiting');
  }, []);

  const persist = React.useCallback(
    async (confirm: boolean, quiet = false) => {
      const currentSnapshot = latestSnapshot.current;
      const issue = validateAuthors(authors);
      if (issue) {
        if (!quiet) toast({ title: issue, variant: 'error' });
        return false;
      }
      if (confirm && (!title.trim() || authors.length === 0 || keywords.length === 0)) {
        toast({ title: '确认导出前，请补齐题名、至少一位作者和关键词', variant: 'error' });
        return false;
      }
      setSaveState('saving');
      try {
        await updateProject(project.id, {
          publication_title: title.trim() || null,
          author_details: authors,
          keywords,
          metadata_confirmed: confirm,
        });
        if (latestSnapshot.current === currentSnapshot) {
          setDirty(false);
          setConfirmed(confirm);
          setSaveState('saved');
        }
        if (confirm) {
          onSaved();
          toast({ title: '署名信息已确认，可用于导出', variant: 'success' });
        }
        return true;
      } catch (error) {
        setSaveState('error');
        if (!quiet) {
          toast({ title: '署名信息未保存', description: describeError(error), variant: 'error' });
        }
        return false;
      }
    },
    [authors, keywords, onSaved, project.id, title, toast],
  );

  React.useEffect(() => {
    if (!dirty || validateAuthors(authors)) return;
    const timer = window.setTimeout(() => void persist(false, true), 800);
    return () => window.clearTimeout(timer);
  }, [authors, dirty, keywords, persist, snapshot, title]);

  const replaceAuthors = (next: AuthorDetail[]) => {
    setAuthors(next);
    setRemoved(null);
    markChanged();
  };

  const addAuthor = () => {
    const name = newAuthor.trim().replace(/\s+/g, ' ');
    if (!name) return;
    replaceAuthors([
      ...authors,
      { id: newId(), name, affiliations: [], email: null, orcid: null, corresponding: false },
    ]);
    setNewAuthor('');
  };

  const addSelf = async () => {
    try {
      const { profile } = await getAcademicProfile();
      if (!profile) {
        toast({
          title: '还没有保存“我的学术身份”',
          description: '可在全局设置中填写一次，之后每个项目都能直接复用。',
        });
        return;
      }
      const existing = authors.findIndex(
        (author) =>
          (profile.email && author.email === profile.email) ||
          author.name.localeCompare(profile.name, undefined, { sensitivity: 'accent' }) === 0,
      );
      const snapshotProfile = { ...profile, id: newId(), affiliations: [...profile.affiliations] };
      if (existing >= 0) {
        const next = [...authors];
        next[existing] = { ...snapshotProfile, id: authors[existing].id };
        replaceAuthors(next);
        setSelectedAuthorId(next[existing].id);
      } else {
        replaceAuthors([...authors, snapshotProfile]);
        setSelectedAuthorId(snapshotProfile.id);
      }
    } catch (error) {
      toast({ title: '无法读取学术身份', description: describeError(error), variant: 'error' });
    }
  };

  const loadRecentAuthors = async () => {
    if (recentOpen) {
      setRecentOpen(false);
      return;
    }
    if (recentAuthors.length > 0) {
      setRecentOpen(true);
      return;
    }
    setRecentLoading(true);
    try {
      const result = await listProjects();
      const existingKeys = new Set(authors.map(authorIdentityKey));
      const seen = new Set<string>();
      const suggestions = [...result.data]
        .filter((item) => item.id !== project.id)
        .sort((a, b) => (b.updated_at ?? '').localeCompare(a.updated_at ?? ''))
        .flatMap(projectAuthors)
        .filter((author) => {
          const key = authorIdentityKey(author);
          if (!key || existingKeys.has(key) || seen.has(key)) return false;
          seen.add(key);
          return true;
        })
        .slice(0, 8);
      setRecentAuthors(suggestions);
      setRecentOpen(suggestions.length > 0);
      if (suggestions.length === 0) {
        toast({ title: '暂时没有可复用的历史合作者' });
      }
    } catch (error) {
      toast({ title: '无法读取最近合作者', description: describeError(error), variant: 'error' });
    } finally {
      setRecentLoading(false);
    }
  };

  const addRecentAuthor = (author: AuthorDetail) => {
    const snapshotAuthor = { ...author, id: newId(), affiliations: [...author.affiliations] };
    replaceAuthors([...authors, snapshotAuthor]);
    setRecentAuthors((items) => items.filter((item) => authorIdentityKey(item) !== authorIdentityKey(author)));
  };

  const moveAuthor = (from: number, to: number) => {
    if (to < 0 || to >= authors.length || from === to) return;
    const next = [...authors];
    const [author] = next.splice(from, 1);
    next.splice(to, 0, author);
    replaceAuthors(next);
  };

  const selectedAuthor = authors.find((author) => author.id === selectedAuthorId) ?? null;

  if (!editorOpen) {
    return (
      <section className="border-t py-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0 flex-1 space-y-1">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                导出信息
              </p>
              <span className="text-xs text-muted-foreground">
                {project.publication_title && project.keywords?.length
                  ? '题名与关键词已由摘要准备'
                  : '摘要完成后自动生成题名与关键词'}
              </span>
            </div>
            <p className="truncate text-sm font-medium">{title.trim() || project.title}</p>
            <p className="truncate text-xs text-muted-foreground">
              {authors.length ? authors.map((author) => author.name).join(' · ') : '作者待补充'}
              {keywords.length ? ` · ${keywords.length} 个关键词` : ''}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <span className={cn('text-xs', confirmed ? 'text-emerald-700 dark:text-emerald-400' : 'text-muted-foreground')}>
              {confirmed ? '已确认' : '待核对'}
            </span>
            <Button variant="ghost" size="sm" onClick={() => setEditorOpen(true)}>
              <Pencil /> 编辑
            </Button>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="space-y-5 border-t pt-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            导出信息
          </h2>
          <p className="text-sm text-muted-foreground">
            系统会在摘要完成后自动生成题名与关键词。这里只需核对作者，或按需做少量修改。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <SaveIndicator state={saveState} dirty={dirty} />
          <Button variant="ghost" size="sm" onClick={() => setEditorOpen(false)}>
            完成编辑
          </Button>
        </div>
      </div>

      <div className="rounded-lg border bg-card px-5 py-8 text-center shadow-sm sm:px-10">
        <Input
          value={title}
          onChange={(event) => {
            setTitle(event.target.value);
            markChanged();
          }}
          aria-label="论文发表题名"
          placeholder="论文题名"
          className="mx-auto h-auto min-h-11 max-w-3xl border-0 bg-transparent px-0 text-center font-serif text-xl font-semibold shadow-none focus-visible:ring-0 sm:text-2xl"
        />
        <Byline authors={authors} />
        <div className="mt-5 flex flex-wrap justify-center gap-2">
          {keywords.map((keyword) => (
            <span
              key={keyword}
              className="inline-flex min-h-8 items-center gap-1 rounded-full bg-muted px-3 text-xs"
            >
              {keyword}
              <button
                type="button"
                className="-mr-2 inline-flex h-11 w-11 items-center justify-center rounded-full text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                aria-label={`删除关键词 ${keyword}`}
                onClick={() => {
                  setKeywords((items) => items.filter((item) => item !== keyword));
                  markChanged();
                }}
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
          <TagInput
            ariaLabel="添加关键词"
            placeholder={keywords.length ? '添加关键词' : '输入关键词后按回车'}
            onAdd={(value) => {
              if (!keywords.includes(value)) setKeywords((items) => [...items, value]);
              markChanged();
            }}
          />
        </div>
      </div>

      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm font-medium">作者顺序</p>
          <div className="flex flex-wrap justify-end gap-1">
            <Button variant="ghost" size="sm" onClick={() => void addSelf()}>
              <UserRoundCheck /> 使用我的学术身份
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void loadRecentAuthors()}
              disabled={recentLoading}
              aria-expanded={recentOpen}
            >
              <UsersRound /> {recentLoading ? '正在读取…' : '最近合作者'}
            </Button>
          </div>
        </div>

        {recentOpen && recentAuthors.length > 0 && (
          <div className="rounded-lg border bg-muted/40 p-3" aria-label="最近合作者建议">
            <p className="mb-2 text-xs text-muted-foreground">来自历史项目；加入后保存为当前项目快照。</p>
            <div className="flex flex-wrap gap-2">
              {recentAuthors.map((author) => (
                <Button
                  key={authorIdentityKey(author)}
                  variant="outline"
                  size="sm"
                  onClick={() => addRecentAuthor(author)}
                >
                  <Plus /> {author.name}
                </Button>
              ))}
            </div>
          </div>
        )}

        <div className="space-y-2" role="list" aria-label="作者列表">
          {authors.map((author, index) => (
            <div
              key={author.id}
              role="listitem"
              draggable
              onDragStart={() => setDraggedId(author.id)}
              onDragEnd={() => setDraggedId(null)}
              onDragOver={(event) => event.preventDefault()}
              onDrop={() => {
                const from = authors.findIndex((item) => item.id === draggedId);
                if (from >= 0) moveAuthor(from, index);
                setDraggedId(null);
              }}
              className={cn(
                'flex min-h-14 items-center gap-2 rounded-lg border bg-background px-2 transition-colors',
                draggedId === author.id && 'opacity-60',
              )}
            >
              <GripVertical className="h-4 w-4 cursor-grab text-muted-foreground" aria-hidden="true" />
              <span className="w-6 text-center font-mono text-xs text-muted-foreground">
                {index + 1}
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{author.name}</p>
                <p className="truncate text-xs text-muted-foreground">
                  {author.affiliations.join(' · ') || '详细资料选填'}
                  {author.corresponding ? ' · 通讯作者' : ''}
                </p>
              </div>
              <div className="flex items-center">
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-11 w-11"
                  onClick={() => moveAuthor(index, index - 1)}
                  disabled={index === 0}
                  aria-label={`将 ${author.name} 上移`}
                >
                  <ArrowUp />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-11 w-11"
                  onClick={() => moveAuthor(index, index + 1)}
                  disabled={index === authors.length - 1}
                  aria-label={`将 ${author.name} 下移`}
                >
                  <ArrowDown />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-11 w-11"
                  onClick={() => setSelectedAuthorId(author.id)}
                  aria-label={`编辑 ${author.name} 的详细资料`}
                >
                  <Pencil />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-11 w-11 text-muted-foreground hover:text-destructive"
                  onClick={() => {
                    setRemoved({ author, index });
                    setAuthors((items) => items.filter((item) => item.id !== author.id));
                    markChanged();
                  }}
                  aria-label={`删除 ${author.name}`}
                >
                  <Trash2 />
                </Button>
              </div>
            </div>
          ))}
          {authors.length === 0 && (
            <p className="rounded-lg border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
              还没有作者。添加姓名即可，单位和 ORCID 可以稍后补充。
            </p>
          )}
        </div>

        {removed && (
          <div className="flex items-center justify-between rounded-lg bg-muted px-3 py-2 text-sm" role="status">
            <span>已移除 {removed.author.name}</span>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                const next = [...authors];
                next.splice(Math.min(removed.index, next.length), 0, removed.author);
                setAuthors(next);
                setRemoved(null);
                markChanged();
              }}
            >
              撤销
            </Button>
          </div>
        )}

        <div className="flex flex-col gap-2 sm:flex-row">
          <Input
            value={newAuthor}
            onChange={(event) => setNewAuthor(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                addAuthor();
              }
            }}
            placeholder="输入作者姓名"
            aria-label="新作者姓名"
            className="h-11"
          />
          <Button variant="outline" className="h-11 sm:shrink-0" onClick={addAuthor} disabled={!newAuthor.trim()}>
            <Plus /> 添加作者
          </Button>
        </div>
      </div>

      <div className="flex flex-col-reverse gap-3 border-t pt-4 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-xs text-muted-foreground">
          {confirmed ? '当前署名已确认用于导出。任何修改都会自动撤销确认。' : '尚未确认；投稿级导出会提醒你完成核对。'}
        </p>
        <Button className="h-11 sm:min-w-36" onClick={() => void persist(true)} disabled={saveState === 'saving'}>
          <Check /> {confirmed ? '已确认用于导出' : '确认用于导出'}
        </Button>
      </div>

      <AuthorDetailsDrawer
        author={selectedAuthor}
        onClose={() => setSelectedAuthorId(null)}
        onChange={(next) => {
          setAuthors((items) => items.map((item) => (item.id === next.id ? next : item)));
          markChanged();
        }}
      />
    </section>
  );
}

function AuthorDetailsDrawer({
  author,
  onClose,
  onChange,
}: {
  author: AuthorDetail | null;
  onClose: () => void;
  onChange: (author: AuthorDetail) => void;
}) {
  if (!author) return null;
  const update = (patch: Partial<AuthorDetail>) => onChange({ ...author, ...patch });
  return (
    <Drawer
      open
      onClose={onClose}
      title={`作者资料 · ${author.name}`}
      description="只有姓名是必填项；通讯作者必须提供邮箱。"
      footer={<Button className="h-11 w-full sm:w-auto" onClick={onClose}>完成</Button>}
    >
      <div className="space-y-6">
        <div className="space-y-2">
          <Label htmlFor={`author-name-${author.id}`}>署名</Label>
          <Input
            id={`author-name-${author.id}`}
            value={author.name}
            onChange={(event) => update({ name: event.target.value })}
            className="h-11"
          />
        </div>
        <div className="space-y-2">
          <Label>单位</Label>
          <div className="flex flex-wrap gap-2">
            {author.affiliations.map((affiliation) => (
              <span key={affiliation} className="inline-flex min-h-8 items-center gap-1 rounded-full bg-muted px-3 text-xs">
                {affiliation}
                <button
                  type="button"
                  className="-mr-2 inline-flex h-11 w-11 items-center justify-center rounded-full"
                  aria-label={`删除单位 ${affiliation}`}
                  onClick={() => update({ affiliations: author.affiliations.filter((item) => item !== affiliation) })}
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
            <TagInput
              ariaLabel="添加单位"
              placeholder="输入单位后按回车"
              onAdd={(value) => {
                if (!author.affiliations.includes(value)) update({ affiliations: [...author.affiliations, value] });
              }}
            />
          </div>
        </div>
        <div className="space-y-2">
          <Label htmlFor={`author-email-${author.id}`}>邮箱</Label>
          <Input
            id={`author-email-${author.id}`}
            type="email"
            value={author.email ?? ''}
            onChange={(event) => update({ email: event.target.value || null })}
            placeholder="name@university.edu"
            className="h-11"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor={`author-orcid-${author.id}`}>ORCID</Label>
          <Input
            id={`author-orcid-${author.id}`}
            value={author.orcid ?? ''}
            onChange={(event) => update({ orcid: event.target.value || null })}
            placeholder="0000-0002-1825-0097"
            inputMode="numeric"
            className="h-11"
          />
          {author.orcid && !validOrcid(author.orcid) && (
            <p className="text-xs text-destructive" role="alert">ORCID 格式或校验位不正确。</p>
          )}
        </div>
        <label className="flex min-h-11 items-center gap-3 rounded-lg border px-3 text-sm">
          <Checkbox
            checked={author.corresponding}
            onCheckedChange={(corresponding) => update({ corresponding })}
            aria-label="设为通讯作者"
          />
          <span>
            通讯作者
            <span className="ml-2 text-xs text-muted-foreground">需填写邮箱</span>
          </span>
        </label>
      </div>
    </Drawer>
  );
}

function TagInput({
  onAdd,
  placeholder,
  ariaLabel,
}: {
  onAdd: (value: string) => void;
  placeholder: string;
  ariaLabel: string;
}) {
  const [value, setValue] = React.useState('');
  const commit = () => {
    const next = value.trim().replace(/\s+/g, ' ');
    if (!next) return;
    onAdd(next);
    setValue('');
  };
  return (
    <Input
      value={value}
      onChange={(event) => setValue(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ',') {
          event.preventDefault();
          commit();
        }
      }}
      aria-label={ariaLabel}
      placeholder={placeholder}
      className="min-w-36 flex-1 rounded-full border-dashed px-3 text-xs shadow-none"
    />
  );
}

function Byline({ authors }: { authors: AuthorDetail[] }) {
  const affiliations = uniqueAffiliations(authors);
  if (!authors.length) {
    return <p className="mt-3 text-sm text-muted-foreground">作者姓名将显示在这里</p>;
  }
  return (
    <div className="mt-4 space-y-2">
      <p className="text-sm">
        {authors.map((author, index) => {
          const marks = author.affiliations.map((item) => affiliations.indexOf(item) + 1);
          return (
            <React.Fragment key={author.id}>
              {index > 0 && <span aria-hidden="true"> · </span>}
              <span>{author.name}</span>
              {(marks.length > 0 || author.corresponding) && (
                <sup className="ml-0.5 text-[10px] text-muted-foreground">
                  {marks.join(',')}{author.corresponding ? '*' : ''}
                </sup>
              )}
            </React.Fragment>
          );
        })}
      </p>
      {affiliations.length > 0 && (
        <div className="space-y-0.5 text-xs text-muted-foreground">
          {affiliations.map((affiliation, index) => (
            <p key={affiliation}><sup>{index + 1}</sup> {affiliation}</p>
          ))}
        </div>
      )}
      {authors.some((author) => author.corresponding) && (
        <p className="text-xs text-muted-foreground">* 通讯作者</p>
      )}
    </div>
  );
}

function SaveIndicator({ state, dirty }: { state: SaveState; dirty: boolean }) {
  const label =
    state === 'saving' ? '正在保存…' :
    state === 'error' ? '自动保存失败' :
    dirty || state === 'waiting' ? '等待自动保存' :
    state === 'saved' ? '草稿已保存' : '已同步';
  return (
    <span className={cn('text-xs text-muted-foreground', state === 'error' && 'text-destructive')} role="status">
      {label}
    </span>
  );
}

function projectAuthors(project: Project): AuthorDetail[] {
  if (project.author_details?.length) {
    return project.author_details.map((author) => ({ ...author, affiliations: [...author.affiliations] }));
  }
  return (project.authors ?? []).map((name, index) => ({
    id: `legacy-${index + 1}`,
    name,
    affiliations: [],
    email: null,
    orcid: null,
    corresponding: false,
  }));
}

function uniqueAffiliations(authors: AuthorDetail[]): string[] {
  return [...new Set(authors.flatMap((author) => author.affiliations))];
}

function authorIdentityKey(author: AuthorDetail): string {
  return (author.email?.trim().toLowerCase() || author.name.trim().toLocaleLowerCase()).normalize('NFKC');
}

function validateAuthors(authors: AuthorDetail[]): string | null {
  for (const author of authors) {
    if (!author.name.trim()) return '作者姓名不能为空';
    if (author.email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(author.email)) {
      return `${author.name} 的邮箱格式不正确`;
    }
    if (author.corresponding && !author.email) return `通讯作者 ${author.name} 需要填写邮箱`;
    if (author.orcid && !validOrcid(author.orcid)) return `${author.name} 的 ORCID 格式或校验位不正确`;
  }
  return null;
}

function validOrcid(value: string): boolean {
  const compact = value.replace(/[^0-9Xx]/g, '').toUpperCase();
  if (!/^\d{15}[\dX]$/.test(compact)) return false;
  let total = 0;
  for (const digit of compact.slice(0, 15)) total = (total + Number(digit)) * 2;
  const remainder = (12 - (total % 11)) % 11;
  return compact[15] === (remainder === 10 ? 'X' : String(remainder));
}

function newId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `author-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
