'use client';

import * as React from 'react';
import Link from 'next/link';
import { Button } from '@/components/ui/button';
import { Callout } from '@/components/ui/callout';
import { Dialog } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { getIntake, submitIntake, updateProject } from '@/lib/api';
import { describeError } from '@/lib/errors';
import { CITATION_STYLE_LABEL, VENUE_TEMPLATES, LANGUAGE_LABEL, PAPER_TYPE_LABEL, WRITING_MODE_LABEL } from '@/lib/labels';
import type { CitationStyle, IntakeOverrides, IntakeState, Language, PaperType, WritingMode } from '@/lib/types';
import { useProject } from './project-context';

export function IntakePanel() {
  const { projectId, project, reload, startJob, busy } = useProject();
  const [state, setState] = React.useState<IntakeState | null>(project?.intake ?? null);
  const [error, setError] = React.useState<string | null>(null);
  const [sending, setSending] = React.useState(false);
  const [editing, setEditing] = React.useState(false);
  const [expanded, setExpanded] = React.useState(false);
  const detailsId = React.useId();
  const [answer, setAnswer] = React.useState('');
  const [topic, setTopic] = React.useState('');
  const [overrides, setOverrides] = React.useState<IntakeOverrides>({});
  const [mode, setMode] = React.useState<WritingMode>('assisted');
  const [citation, setCitation] = React.useState<CitationStyle>('gbt7714');
  const [template, setTemplate] = React.useState('article');
  const versionRef = React.useRef('');
  const present = Boolean(project?.intake);

  React.useEffect(() => {
    if (!present) return;
    setState(project?.intake ?? null);
    setError(null);
    versionRef.current = '';
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      let next: IntakeState | undefined;
      try {
        next = await getIntake(projectId);
        if (!alive) return;
        setState(next);
        const signature = `${next.version}:${next.status}`;
        if (signature !== versionRef.current) {
          versionRef.current = signature;
          reload();
        }
      } catch (err) {
        if (alive) setError(describeError(err));
      } finally {
        if (alive) timer = setTimeout(load, next?.status === 'running' ? 2000 : 8000);
      }
    };
    void load();
    return () => { alive = false; clearTimeout(timer); };
  }, [present, projectId, reload]);

  if (!present || !state || !project) return null;
  const running = state.status === 'running' && busy;
  const waiting = state.status === 'needs_input';
  const ready = state.status === 'ready';

  const start = async (edit = false) => {
    if (sending) return;
    setSending(true);
    setError(null);
    try {
      if (edit) {
        const settingsChanged = mode !== project.writing_mode || citation !== project.citation_style || template !== (project.venue_template || 'article');
        if (settingsChanged) {
          await updateProject(projectId, { writing_mode: mode, citation_style: citation, venue_template: template });
          reload();
        }
        const intentChanged = topic !== (project.topic ?? '') || overrides.paper_type !== (state.paper_type ?? state.overrides?.paper_type)
          || overrides.language !== (state.language ?? project.language) || overrides.submission_target !== (state.submission_target ?? '');
        if (settingsChanged && !intentChanged) {
          setEditing(false);
          return;
        }
      }
      const job = await submitIntake(projectId, {
        version: state.version,
        ...(edit ? { topic, overrides } : answer.trim() ? { answer: answer.trim() } : {}),
      });
      startJob(job, '需求理解未能启动');
      setEditing(false);
      setState({ ...state, status: 'running' });
      setAnswer('');
      reload();
    } catch (err) {
      setError(describeError(err));
    } finally { setSending(false); }
  };
  const edit = () => {
    setTopic(project.topic ?? '');
    setMode(project.writing_mode);
    setCitation(project.citation_style ?? 'gbt7714');
    setTemplate(project.venue_template || 'article');
    setOverrides({
      ...state.overrides,
      paper_type: state.paper_type ?? state.overrides?.paper_type,
      language: state.language ?? project.language,
      submission_target: state.submission_target ?? '',
    });
    setEditing(true);
  };

  return (
    <section aria-label="我理解的任务" className="space-y-3 rounded-lg border bg-card p-4 sm:p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-serif text-lg font-semibold">{
          ready ? '我理解的任务' : waiting ? '先明确研究方向' : state.status === 'running' ? '正在理解研究目标' : '开始理解研究目标'
        }</h2>
        <Button variant="ghost" size="sm" onClick={edit} disabled={sending || running || busy}>修改理解</Button>
      </div>
      {ready && <button type="button" aria-expanded={expanded} aria-controls={detailsId}
        onClick={() => setExpanded(value => !value)}
        className="flex min-h-11 w-full items-center justify-between gap-3 rounded-md text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <span className="min-w-0 truncate">{state.summary || '研究规划已保存'}</span>
        <span className="shrink-0 text-muted-foreground">{expanded ? '收起规划 ↑' : '展开规划 ↓'}</span>
      </button>}
      <div id={detailsId} hidden={ready && !expanded} className="space-y-3">
      <p className="text-sm font-medium">{(ready || waiting) && state.paper_type ? PAPER_TYPE_LABEL[state.paper_type] : '论文类型待判断'} · {LANGUAGE_LABEL[state.language ?? project.language]} · {WRITING_MODE_LABEL[project.writing_mode]}</p>
      {(ready || waiting) && state.summary && <p className="text-sm leading-relaxed">{state.summary}</p>}
      {ready && <p className="text-sm text-muted-foreground">{project.writing_mode === 'assisted'
        ? '已提出研究规划。你可以修改研究范围，再决定何时开始检索与写作。'
        : state.material_issues?.length ? '研究规划已保存，补齐材料后可继续全自动生成。'
        : busy ? '正在按当前方案继续研究；你可以随时暂停任务。' : '研究规划已保存，可以查看研究进度或继续工作。'}{state.next_step && ` ${state.next_step}`}</p>}
      {(ready || waiting) && state.submission_target && <p className="text-sm text-muted-foreground">投稿目标：{state.submission_target}。排版在导出前确认。</p>}
      {waiting && <div className="space-y-3">
        {state.questions?.map((question, index) => <fieldset key={index} className="space-y-2">
          <legend className="text-sm">{question.question}</legend>
          <div className="flex flex-wrap gap-2">{question.options.map(option => <Button key={option} size="sm" variant="outline"
            onClick={() => setAnswer(current => `${current}${current ? '\n' : ''}${question.question}：${option}`)}>{option}</Button>)}</div>
        </fieldset>)}
        <label className="block text-sm" htmlFor="intake-answer">补充研究目标</label>
        <textarea id="intake-answer" rows={3} value={answer} onChange={e => setAnswer(e.target.value)}
          className="w-full rounded-md border bg-background p-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
        <Button onClick={() => void start()} disabled={!answer.trim() || sending || busy} loading={sending}>继续研究</Button>
      </div>}
      {(state.status === 'failed' || state.status === 'pending') && <div className="space-y-2">
        <p className="text-sm text-muted-foreground">{state.error || '项目和材料已保存，开始理解后会在这里显示研究规划。'}</p>
        <Button onClick={() => void start()} loading={sending} disabled={sending || busy}>{state.status === 'failed' ? '重试需求理解' : '开始需求理解'}</Button>
      </div>}
      </div>
      {ready && state.material_issues && state.material_issues.length > 0 && <Callout variant="warning">
        <p>后续写作还需补充材料：</p>
        <ul className="list-disc pl-5">{state.material_issues.map(issue => <li key={issue.code}>{issue.message}</li>)}</ul>
        <Link className="underline" href={`/projects/${projectId}/assets`}>进入素材中心</Link>
      </Callout>}
      {((ready && expanded) || waiting) && state.materials && state.materials.length > 0 && <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer">本次规划使用的材料</summary>
        <ul className="mt-2 space-y-1">{state.materials.map(material => <li key={material.id}>{material.title} · 已接收{material.parsed ? ' · 已解析' : ' · 尚无可用解析内容'}{material.used ? ' · 摘录已用于本次规划' : ' · 未用于本次规划'}{material.truncated ? '（仅部分内容）' : ''}{material.warnings?.length ? ` · 解析提醒：${material.warnings.join('；')}` : ''}</li>)}</ul>
      </details>}
      {ready && expanded && <Link className="inline-block rounded text-sm underline underline-offset-4 focus-visible:ring-2 focus-visible:ring-ring" href={`/projects/${projectId}/scope`}>查看并修改研究规划 →</Link>}
      {error && <Callout variant="error" role="alert">{error}</Callout>}
      <Dialog open={editing} onClose={() => setEditing(false)} title="修改理解"
        description="手动修改优先采用。合作方式和投稿设置用于后续任务；修改研究目标会重新规划。"
        footer={<Button onClick={() => void start(true)} disabled={sending || busy} loading={sending}>保存并更新规划</Button>}>
        <div className="space-y-4">
          <label className="block space-y-1 text-sm">研究目标<textarea rows={4} value={topic} onChange={e => setTopic(e.target.value)} className="block w-full rounded-md border bg-background p-2 focus-visible:ring-2 focus-visible:ring-ring" /></label>
          <label className="block space-y-1 text-sm">论文类型<Select value={overrides.paper_type ?? ''} disabled={state.type_locked !== false} onChange={e => setOverrides(value => ({ ...value, paper_type: e.target.value as PaperType || undefined }))}>
            <option value="">根据研究目标判断</option><option value="review">综述论文</option><option value="original">研究型论文</option>
          </Select></label>
          {state.type_locked && <p className="text-xs text-muted-foreground">检索或写作已经开始，改变论文类型需要新建项目。</p>}
          <label className="block space-y-1 text-sm">语言<Select value={overrides.language ?? 'zh'} onChange={e => setOverrides(value => ({ ...value, language: e.target.value as Language }))}><option value="zh">中文</option><option value="en">English</option></Select></label>
          <fieldset className="space-y-2"><legend className="text-sm">合作方式</legend>
            {(['assisted', 'auto'] as const).map(value => <label key={value} className="flex items-center gap-2 text-sm"><input type="radio" name="intake-mode" checked={mode === value} onChange={() => setMode(value)} />{value === 'assisted' ? '协作：由你决定何时继续' : '全自动：方向明确后继续生成'}</label>)}
          </fieldset>
          <details className="space-y-3"><summary className="cursor-pointer text-sm font-medium">投稿要求</summary>
          <label className="block space-y-1 text-sm">目标期刊或会议<Input value={overrides.submission_target ?? ''} onChange={e => setOverrides(value => ({ ...value, submission_target: e.target.value }))} /></label>
          <label className="block space-y-1 text-sm">引用样式<Select value={citation} onChange={e => setCitation(e.target.value as CitationStyle)}>{(Object.keys(CITATION_STYLE_LABEL) as CitationStyle[]).map(value => <option key={value} value={value}>{CITATION_STYLE_LABEL[value]}</option>)}</Select></label>
          <label className="block space-y-1 text-sm">投稿模板<Select value={template} onChange={e => setTemplate(e.target.value)}>{VENUE_TEMPLATES.map(value => <option key={value.id} value={value.id}>{value.label}</option>)}</Select></label>
          </details>
        </div>
      </Dialog>
    </section>
  );
}
