'use client';

import * as React from 'react';
import Link from 'next/link';
import type { LucideIcon } from 'lucide-react';
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Download,
  Feather,
  Loader2,
  RotateCcw,
  RotateCw,
  Rocket,
  Wand2,
} from 'lucide-react';
import { Button, buttonVariants } from '@/components/ui/button';
import { SectionTitle } from '@/components/ui/section-title';
import { StatusDot, type StatusState } from '@/components/ui/status';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { ModuleError } from '@/components/layout/module-error';
import { useToast } from '@/components/ui/toast';
import { useJobFinished, useProject } from './project-context';
import {
  exportDownloadUrl,
  generateAll,
  getCitationAudit,
  getCostDetail,
  getNumLint,
  getSubmissionReadiness,
  getVersionHistory,
  listExports,
  listJobs,
  restoreDocumentVersion,
  skipPolish,
  skipQualityRepair,
  startPolish,
  startQualityRepair,
} from '@/lib/api';
import { describeError } from '@/lib/errors';
import { artifactPredatesVisuals, latestArtifact } from '@/lib/artifact-relations';
import { stageLabel } from '@/lib/labels';
import { nextAction } from '@/lib/useProjectProgress';
import type { JobWarning, TrackedJob } from '@/lib/useJobTracker';
import { projectHref, RETRYABLE_STAGES, type RetryableStage } from '@/lib/pipeline';
import type {
  CitationAudit,
  CostDetail,
  ExportArtifact,
  Job,
  NumLintReport,
  Project,
  QualityProfile,
  ReviewStyle,
  SubmissionReadiness,
  VersionHistory,
} from '@/lib/types';
import { formatDate } from '@/lib/utils';
import { useAsyncModule } from '@/lib/useAsyncModule';
import { PublicationMetadata } from './publication-metadata';

/** 下拉选项与收起后那行摘要读同一份数据，避免两处文案各写各的。 */
const QUALITY_OPTIONS: { value: QualityProfile; label: string }[] = [
  // 「一次跑到稿」是默认承诺：draft 档一样跑完整质量评估、发现项一条不少，
  // 只是不把它们升级成阻断项，因此一定有导出件。要不要为这些发现项花一轮
  // 重写，交给跑完之后的修复决策。scholarly / submission 则是用户主动选择
  // 「不达标就别给我导出」，那两档保留质量门与自动收敛。
  { value: 'draft', label: '一次跑到稿（推荐）' },
  { value: 'scholarly', label: '学术严谨（未达标不导出）' },
  { value: 'submission', label: '严格投稿（未达标不导出）' },
];

const REVIEW_STYLE_OPTIONS: { value: ReviewStyle; label: string }[] = [
  { value: 'narrative', label: '叙述性综述' },
  { value: 'systematic', label: '系统综述' },
];

function optionLabel<T extends string>(
  options: { value: T; label: string }[],
  value: T,
): string {
  return options.find((option) => option.value === value)?.label ?? value;
}

/**
 * 项目概览。
 *
 * 此前是「下一步 / 稿件快照 / 近期任务 / 成本 / 版本」五张 Card 拼成的
 * SaaS 网格。docs/ui-design.md 原则 07（Typography over containers）要求
 * 分层靠排版而不是容器：一个语义段落不自动等于一张卡片。现在整页 0 张 Card，
 * 层级由「小写标题 + 留白 + 一条分隔线」承担。
 *
 * 唯一保留边框的是最上面的「下一步」——它是全页唯一的行动召唤，
 * 需要一个能被眼睛先抓到的锚点。
 */
export function ProjectOverview() {
  const {
    projectId,
    project,
    paperType,
    progress,
    busy,
    tracked,
    startJob,
    retryStage,
    reload,
  } = useProject();
  const { toast } = useToast();

  const auditModule = useAsyncModule<CitationAudit | undefined>(
    (signal) => getCitationAudit(projectId, signal).then((result) => result.data), undefined, [projectId],
  );
  const jobsModule = useAsyncModule<Job[]>(
    (signal) => listJobs(projectId, signal).then((result) => result.data), [], [projectId],
  );
  const costModule = useAsyncModule<CostDetail | undefined>(
    (signal) => getCostDetail(projectId, signal).then((result) => result.data), undefined, [projectId],
  );
  const versionsModule = useAsyncModule<VersionHistory | undefined>(
    (signal) => getVersionHistory(projectId, signal).then((result) => result.data), undefined, [projectId],
  );
  const exportsModule = useAsyncModule<ExportArtifact[]>(
    (signal) => listExports(projectId, signal).then((result) => result.data), [], [projectId],
  );
  const lintModule = useAsyncModule<NumLintReport | undefined>(
    (signal) => paperType === 'original'
      ? getNumLint(projectId, signal).then((result) => result.data)
      : Promise.resolve(undefined),
    undefined,
    [projectId, paperType],
  );
  const readinessModule = useAsyncModule<SubmissionReadiness | undefined>(
    (signal) =>
      getSubmissionReadiness(projectId, signal).then((result) => result.data),
    undefined,
    [projectId],
  );
  const audit = auditModule.data;
  const jobs = jobsModule.data;
  const cost = costModule.data;
  const versions = versionsModule.data;
  const exports = exportsModule.data;
  const lint = lintModule.data;
  const [qualityProfile, setQualityProfile] = React.useState<QualityProfile>('draft');
  const [reviewStyle, setReviewStyle] = React.useState<ReviewStyle>('narrative');
  /** 点击到 busy 置位之间有一次网络往返，不自己置位就能连点两下起两条管线。 */
  const [starting, setStarting] = React.useState(false);
  const [polishAction, setPolishAction] = React.useState<'start' | 'skip' | null>(null);
  const [repairAction, setRepairAction] = React.useState<'start' | 'skip' | null>(null);
  /** 原则 06：质量模式 / 综述方式是高级参数，默认收起，只留一行当前取值。 */
  const [settingsOpen, setSettingsOpen] = React.useState(false);
  const [restoringVersion, setRestoringVersion] = React.useState<string | null>(null);

  const reloadModules = React.useCallback(() => {
    auditModule.reload();
    jobsModule.reload();
    costModule.reload();
    versionsModule.reload();
    exportsModule.reload();
    lintModule.reload();
    readinessModule.reload();
  }, [auditModule.reload, jobsModule.reload, costModule.reload, versionsModule.reload, exportsModule.reload, lintModule.reload, readinessModule.reload]);
  useJobFinished(reloadModules);

  const action = nextAction(progress, paperType);
  const latestPdf = latestArtifact(exports, 'pdf');
  const latestDocument = versions?.documents[0];
  const pdfNeedsRefresh = artifactPredatesVisuals(latestPdf, progress.visuals);
  const polishDecisionJob = jobs.find(
    (job) =>
      job.kind === 'full' &&
      job.status === 'succeeded' &&
      (job.checkpoint?.polish_decision === 'pending' ||
        job.checkpoint?.polish_decision === 'failed'),
  );
  const qualityRepairJob = jobs.find(
    (job) =>
      job.kind === 'full' &&
      job.status === 'succeeded' &&
      (job.checkpoint?.quality_repair_decision === 'pending' ||
        job.checkpoint?.quality_repair_decision === 'failed'),
  );
  const repairFindingCount = Number(qualityRepairJob?.checkpoint?.quality_finding_count ?? 0);
  const latestFullJob = jobs.find((job) => job.kind === 'full');
  // 一键入口固定跑 draft 档：质检照跑、warnings 一条不少，但发现项不会升级成阻断项。
  // 不说出来的话，用户看到的就是一份「零阻断」的报告，很容易当成已经达标。
  const deliveredProfile =
    ((latestFullJob?.checkpoint?.resume as { kwargs?: { quality_profile?: string } } | undefined)
      ?.kwargs?.quality_profile ?? 'draft');

  const runAll = async () => {
    if (starting || busy) return;
    setStarting(true);
    try {
      const started = await generateAll(projectId, {
        quality_profile: qualityProfile,
        review_style: reviewStyle,
      });
      startJob(started.data, '后端不可用：无法启动全管线');
    } catch (err) {
      toast({ title: '全管线未能启动', description: describeError(err), variant: 'error' });
    } finally {
      setStarting(false);
    }
  };

  const restoreVersion = async (documentId: string, version: number) => {
    if (!window.confirm(`将文稿 v${version} 复制为一个新的当前版本。现有版本和导出文件都会保留，是否继续？`)) return;
    setRestoringVersion(documentId);
    try {
      const restored = await restoreDocumentVersion(projectId, documentId);
      toast({
        title: `已恢复为文稿 v${restored.version}`,
        description: '恢复操作创建了新版本；质量报告和导出需要重新生成。',
      });
      versionsModule.reload();
      reload();
    } catch (error) {
      toast({ title: '版本恢复失败', description: describeError(error), variant: 'error' });
    } finally {
      setRestoringVersion(null);
    }
  };

  const runAllState = runAllButton({
    starting,
    busy,
    // busy 对**任一**项目级任务都为真（单跑检索也会）。只有 kind=full 才是「全管线在跑」，
    // 否则单跑一次检索也会让按钮谎称全管线正在运行。
    running: tracked?.job.kind === 'full' ? tracked : null,
    // 「跑完」不看 project.status（它永远是 draft），看实际产物：有正文且有导出产物。
    done:
      latestFullJob?.status === 'succeeded' &&
      Boolean(latestFullJob.checkpoint?.render),
    submission: qualityProfile === 'submission',
  });
  const RunAllIcon = runAllState.Icon;

  const beginPolish = async () => {
    if (!polishDecisionJob || polishAction || busy) return;
    setPolishAction('start');
    try {
      const started = await startPolish(projectId, polishDecisionJob.id);
      jobsModule.reload();
      startJob(started, '后端不可用：无法启动润色');
    } catch (err) {
      toast({ title: '润色未能启动', description: describeError(err), variant: 'error' });
    } finally {
      setPolishAction(null);
    }
  };

  const keepFirstDraft = async () => {
    if (!polishDecisionJob || polishAction || busy) return;
    setPolishAction('skip');
    try {
      await skipPolish(projectId, polishDecisionJob.id);
      jobsModule.reload();
      toast({ title: '已保留首稿', description: '本轮不再自动润色，可继续手动编辑或导出。' });
    } catch (err) {
      toast({ title: '跳过润色失败', description: describeError(err), variant: 'error' });
    } finally {
      setPolishAction(null);
    }
  };
  const beginQualityRepair = async () => {
    if (!qualityRepairJob || repairAction || busy) return;
    setRepairAction('start');
    try {
      const started = await startQualityRepair(projectId, qualityRepairJob.id);
      jobsModule.reload();
      startJob(started, '后端不可用：无法启动质量修复');
    } catch (err) {
      toast({ title: '质量修复未能启动', description: describeError(err), variant: 'error' });
    } finally {
      setRepairAction(null);
    }
  };

  const keepCurrentQuality = async () => {
    if (!qualityRepairJob || repairAction || busy) return;
    setRepairAction('skip');
    try {
      await skipQualityRepair(projectId, qualityRepairJob.id);
      jobsModule.reload();
      toast({
        title: '已保留当前稿',
        description: '质检结果仍留在写作工作台，随时可以回来处理。',
      });
    } catch (err) {
      toast({ title: '操作失败', description: describeError(err), variant: 'error' });
    } finally {
      setRepairAction(null);
    }
  };
  // 收起状态下的一行摘要：不展开也知道下一次会按什么模式跑。
  const runConfigSummary = [
    optionLabel(QUALITY_OPTIONS, qualityProfile),
    paperType === 'review' ? optionLabel(REVIEW_STYLE_OPTIONS, reviewStyle) : null,
  ]
    .filter(Boolean)
    .join(' · ');

  if (progress.loading) {
    return (
      <div className="space-y-8">
        <Skeleton className="h-24" />
        <Skeleton className="h-32" />
        <Skeleton className="h-24" />
      </div>
    );
  }

  return (
    <div className="space-y-10">
      {/* 下一步：全页唯一的行动召唤，也是唯一保留边框的块。 */}
      <div className="space-y-4 rounded-lg border border-primary/25 bg-accent/40 px-5 py-4">
        {/*
         * 主行只放「下一步」和它自己的那一个按钮。
         *
         * 此前全管线的两个下拉和运行按钮跟主 CTA 挤在同一排，于是「下一步：处理
         * 视觉建议」旁边就并排立着「质量模式 / 综述方式 / 重跑全管线」——那两个
         * 下拉其实一条也不作用于「处理视觉建议」，但位置让人以为它们是这一步的参数。
         */}
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="min-w-0 space-y-1">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              下一步
            </p>
            <p className="font-serif text-xl font-semibold tracking-tight">{action.label}</p>
            <p className="max-w-2xl text-sm text-muted-foreground">{action.reason}</p>
          </div>
          <Link
            href={projectHref(projectId, action.step === 'overview' ? '' : action.step)}
            className={buttonVariants({ className: 'shrink-0' })}
          >
            {action.label} <ArrowRight className="h-4 w-4" />
          </Link>
        </div>

        {/*
         * 全管线是**另一条路径**（一次跑完），不是「下一步」的参数，所以用一条细线
         * 隔开、字号降一档：看得见、够得到，但不跟主 CTA 抢。
         */}
        <div className="space-y-2 border-t border-primary/15 pt-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="min-w-0 text-xs text-muted-foreground">
              {runAllState.hint}
              <Sep />
              <span className="text-foreground">{runConfigSummary}</span>
              <button
                type="button"
                onClick={() => setSettingsOpen((open) => !open)}
                aria-expanded={settingsOpen}
                className="ml-2 underline underline-offset-4 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {settingsOpen ? '收起' : '调整'}
              </button>
            </p>
            <Button
              variant={runAllState.variant}
              size="sm"
              onClick={runAll}
              disabled={runAllState.disabled}
              title={runAllState.title}
              aria-busy={runAllState.spinning}
              className="shrink-0"
            >
              {runAllState.spinning ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <RunAllIcon className="h-4 w-4" />
              )}
              {runAllState.label}
            </Button>
          </div>

          {/* 展开后才出现的高级参数。两个下拉只对**下一次**运行生效，跑的过程中禁用。 */}
          {settingsOpen && (
            <div className="flex flex-wrap items-end gap-2 pt-1">
              <label className="space-y-1 text-xs text-muted-foreground">
                质量模式
                <Select
                  value={qualityProfile}
                  onChange={(event) => setQualityProfile(event.target.value as QualityProfile)}
                  disabled={runAllState.disabled}
                  className="w-28"
                >
                  {QUALITY_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </Select>
              </label>
              {paperType === 'review' && (
                <label className="space-y-1 text-xs text-muted-foreground">
                  综述方式
                  <Select
                    value={reviewStyle}
                    onChange={(event) => setReviewStyle(event.target.value as ReviewStyle)}
                    disabled={runAllState.disabled}
                    className="w-32"
                  >
                    {REVIEW_STYLE_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </Select>
                </label>
              )}
              <p className="basis-full text-xs text-muted-foreground">
                只影响下一次全管线运行，不改变已经生成的内容。
              </p>
            </div>
          )}
        </div>
      </div>

      {/*
        质量修复决策卡。刻意不用 destructive 配色：这是「初稿已经在手上，还能再
        往前推一步」的邀请，不是故障播报。把质检发现染成红色会让用户以为系统交付了
        一份坏东西，而实际上稿子和 PDF 都已经产出、随时可用。用中性的 accent 底
        （和润色那张同一套），措辞讲"还可以加强"，不讲"错误"。
      */}
      {qualityRepairJob && (
        <section
          aria-labelledby="quality-repair-title"
          className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-primary/25 bg-accent/40 px-5 py-4"
        >
          <div className="min-w-0 space-y-1">
            <p id="quality-repair-title" className="flex items-center gap-2 font-medium">
              <Wand2 className="h-4 w-4" />
              初稿已完成，另有 {repairFindingCount} 处论断可以再加强
            </p>
            <p className="max-w-2xl text-sm text-muted-foreground">
              质检已经跑过一遍，这些是论断与证据对应关系上还能收紧的地方。
              修复会重写涉及的章节、重新核对证据并刷新导出件，通常要几分钟；
              当前稿件和 PDF 已经可以直接使用，也可以先读一遍再决定。
              {deliveredProfile === 'draft' && (
                <>
                  {' '}
                  当前这一稿是<strong className="font-medium text-foreground">初稿档</strong>
                  产出的：质检跑了完整一轮，但发现项只作提示、不作阻断，
                  所以「没有阻断项」不等于已经达到投稿标准。
                </>
              )}
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={keepCurrentQuality}
              disabled={Boolean(repairAction) || busy}
            >
              {repairAction === 'skip' && <Loader2 className="h-4 w-4 animate-spin" />}
              暂不处理
            </Button>
            <Button size="sm" onClick={beginQualityRepair} disabled={Boolean(repairAction) || busy}>
              {repairAction === 'start' ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Wand2 className="h-4 w-4" />
              )}
              开始修复
            </Button>
          </div>
        </section>
      )}

      {polishDecisionJob && (
        <section
          aria-labelledby="polish-decision-title"
          className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-primary/25 bg-accent/40 px-5 py-4"
        >
          <div className="min-w-0 space-y-1">
            <p id="polish-decision-title" className="flex items-center gap-2 font-medium">
              <Feather className="h-4 w-4" />
              初稿已交付，可选做连贯性润色
            </p>
            <p className="max-w-2xl text-sm text-muted-foreground">
              这一步只改善长文的连贯与过渡，不动论断与证据，
              完成后会重新复核质量并刷新导出件；也可以就用当前这一稿。
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={keepFirstDraft}
              disabled={Boolean(polishAction) || busy}
            >
              {polishAction === 'skip' && <Loader2 className="h-4 w-4 animate-spin" />}
              保留首稿，跳过润色
            </Button>
            <Button
              size="sm"
              onClick={beginPolish}
              disabled={Boolean(polishAction) || busy}
            >
              {polishAction === 'start' ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Feather className="h-4 w-4" />
              )}
              开始润色
            </Button>
          </div>
        </section>
      )}

      {/* 稿件：draft-first——永远展示当前最好的产物。 */}
      <section className="space-y-3">
        <SectionTitle>稿件</SectionTitle>

        <ModuleError label="引用审计" error={auditModule.error} onRetry={auditModule.reload} />
        {paperType === 'original' && (
          <ModuleError label="数字检查" error={lintModule.error} onRetry={lintModule.reload} />
        )}
        <ModuleError label="导出文件" error={exportsModule.error} onRetry={exportsModule.reload} />

        <p className="text-sm text-muted-foreground">
          <Figure value={progress.libraryCount} unit="篇文献" />
          <Sep />
          <Figure value={progress.sectionCount} unit="章节" />
          <Sep />
          <Figure value={progress.wordCount} unit="字" />
          <Sep />
          <Figure value={progress.exportCount} unit="个导出产物" />
        </p>

        <p className="text-body text-foreground">
          {latestDocument ? `文稿 v${latestDocument.version}` : '尚无文稿版本'}
          {' · '}
          {progress.libraryCount} 篇文献（{audit?.rows.length ?? 0} 处引用）
          {' · '}
          {progress.approvedVisualCount} 张图
          {' · '}
          {exports.length} 个导出产物
        </p>
        {latestPdf && (
          <p className="text-meta text-muted-foreground">
            最新 PDF 生成于 {formatDate(latestPdf.created_at ?? undefined)}
            {pdfNeedsRefresh
              ? '，早于最近一次图片插入，请重新导出以包含新图。'
              : `，包含当前已批准的 ${progress.approvedVisualCount} 张图。`}
          </p>
        )}

        <div className="space-y-1.5 pt-1">
          <TrustRow
            ok={(audit?.hallucinated_cite_keys.length ?? 0) === 0}
            okText={`0 幻觉引用，全文 ${audit?.used_cite_keys.length ?? 0} 个引用键均在白名单内`}
            badText={`${audit?.hallucinated_cite_keys.length ?? 0} 个越权引用`}
            muted={!audit || Boolean(auditModule.error)}
            mutedText={auditModule.error ? '引用审计状态未知，请重试' : '引用审计在正文生成后产出'}
          />
          {paperType === 'original' && (
            <TrustRow
              ok={lint?.consistent ?? true}
              okText={
                lint
                  ? `正文数字全部可溯源（已核 ${lint.checked_count} 处）`
                  : '数字一致性检查在正文生成后产出'
              }
              badText={`${lint?.unsourced_count ?? 0} 处数值在素材中找不到出处`}
              muted={!lint || Boolean(lintModule.error)}
              mutedText={lintModule.error ? '数字检查状态未知，请重试' : '数字一致性检查在正文生成后产出'}
              href={projectHref(projectId, 'write')}
            />
          )}
        </div>

        {latestPdf && (
          <div className="flex items-center gap-3 pt-1 text-sm">
            <a
              href={exportDownloadUrl(projectId, latestPdf.id)}
              download
              className="inline-flex items-center gap-1.5 font-medium underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Download className="h-3.5 w-3.5" /> 下载最新 PDF
            </a>
            <span className="text-xs text-muted-foreground">
              {formatDate(latestPdf.created_at ?? undefined)}
            </span>
          </div>
        )}
      </section>

      {project && <PublicationMetadata project={project} onSaved={reload} collapsedByDefault />}

      <div className="space-y-3">
        <ModuleError
          label="投稿就绪度"
          error={readinessModule.error}
          onRetry={readinessModule.reload}
        />
        {!readinessModule.error && readinessModule.data && (
          <ReadinessSummary readiness={readinessModule.data} />
        )}
      </div>

      <div className="grid gap-10 md:grid-cols-2">
        <div className="space-y-3">
          <ModuleError label="近期任务" error={jobsModule.error} onRetry={jobsModule.reload} />
          <RecentJobs
            jobs={jobs}
            projectId={projectId}
            paperType={paperType}
            onRetry={async (stage) => {
              try {
                await retryStage(stage);
                toast({ title: '已开始重跑', description: `${stageLabel(stage)}正在重新执行。` });
              } catch (error) {
                toast({ title: '重跑未能启动', description: describeError(error), variant: 'error' });
              }
            }}
          />
        </div>
        <div className="space-y-10">
          <div className="space-y-3">
            <ModuleError label="成本" error={costModule.error} onRetry={costModule.reload} />
            {!costModule.error && <CostSummary cost={cost} />}
          </div>
          <div className="space-y-3">
            <ModuleError label="版本" error={versionsModule.error} onRetry={versionsModule.reload} />
            {!versionsModule.error && (
              <VersionSummary
                versions={versions}
                restoringVersion={restoringVersion}
                onRestore={restoreVersion}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function ReadinessSummary({ readiness }: { readiness: SubmissionReadiness }) {
  const stateLabel = {
    pass: '通过',
    warn: '有提醒',
    fail: '未就绪',
    unknown: '状态未知',
    stale: '检查已过期',
  } as const;
  const dotState = (state: SubmissionReadiness['state']): StatusState =>
    state === 'pass' ? 'done' : state === 'fail' ? 'failed' : 'degraded';
  return (
    <section className="space-y-3 border-y py-4" aria-labelledby="readiness-title">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SectionTitle id="readiness-title">投稿就绪度</SectionTitle>
        <StatusDot state={dotState(readiness.state)} label={stateLabel[readiness.state]} />
      </div>
      <div className="grid gap-x-6 gap-y-2 md:grid-cols-2">
        {readiness.items.map((item) => (
          <div key={item.key} className="flex items-start justify-between gap-3 text-xs">
            <div className="min-w-0">
              <StatusDot state={dotState(item.state)} label={item.label} />
              <p className="mt-0.5 text-muted-foreground">{item.reason}</p>
            </div>
            {item.state !== 'pass' && (
              <Link
                href={item.fix_href}
                className="shrink-0 rounded px-1 py-1 font-medium underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                处理
              </Link>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

interface RunAllButtonState {
  label: string;
  /** 按钮左边那行小字：说清全管线此刻是什么处境，按钮本身只承担动作。 */
  hint: string;
  title: string;
  Icon: LucideIcon;
  variant: 'ghost' | 'outline';
  disabled: boolean;
  spinning: boolean;
}

/**
 * 「跑通全管线」按钮的四态。
 *
 * 此前只有 `disabled={busy}` 一条逻辑，文案在三种状态下完全一样：跑的时候、
 * 跑完之后、和一次都没跑过，按钮都写着「跑通全管线」。ghost 变体禁用后只是
 * 淡一档，跟普通的次要按钮长得几乎一样——用户看不出到底跑没跑、能不能点，
 * 这正是最容易误导的地方。文案现在必须自己说清当前处于哪一态。
 */
function runAllButton({
  starting,
  busy,
  running,
  done,
  submission,
}: {
  starting: boolean;
  busy: boolean;
  running: TrackedJob | null;
  done: boolean;
  submission: boolean;
}): RunAllButtonState {
  const idleLabel = submission ? '生成投稿候选稿' : '跑通全管线';

  if (starting) {
    return {
      label: '正在启动…',
      hint: '正在提交任务',
      title: '正在提交全管线任务',
      Icon: Rocket,
      variant: 'ghost',
      disabled: true,
      spinning: true,
    };
  }
  if (running) {
    // 阶段名比一个干瘪的「运行中」有用得多：全管线要跑十几分钟，用户需要知道跑到哪了。
    return {
      label: `全管线运行中 · ${running.label}`,
      hint: '详细进度见上方任务条',
      title: '全管线正在运行，完成后可再次触发；详细进度见上方任务条',
      Icon: Rocket,
      variant: 'ghost',
      disabled: true,
      spinning: true,
    };
  }
  if (busy) {
    // 单阶段任务（检索 / 大纲 / 导出）也占着 worker，此时起全管线会和它抢同一批产物。
    return {
      label: '等待当前任务结束',
      hint: '有其他任务在跑，结束后可启动全管线',
      title: '有其他任务正在运行，结束后才能启动全管线',
      Icon: Rocket,
      variant: 'ghost',
      disabled: true,
      spinning: false,
    };
  }
  if (done) {
    return {
      label: submission ? '重跑投稿候选稿' : '重跑全管线',
      hint: '已通过学术质量门并生成导出件',
      title: '已经跑过一次：再跑会重新生成大纲与正文并重新编译，旧版本保留在版本记录里',
      Icon: RotateCcw,
      variant: 'outline',
      disabled: false,
      spinning: false,
    };
  }
  return {
    label: idleLabel,
    hint: '也可以不逐步来，自动收敛到学术严谨稿和 PDF',
    title: '检索 → 大纲 → 写作 → 质量修复 → 编译；未达门槛时保留最佳稿并说明需补内容',
    Icon: Rocket,
    variant: 'ghost',
    disabled: false,
    spinning: false,
  };
}

/** 「46 篇文献」这类计数：数字用等宽 + 前景色，单位用 muted，读起来是一句话而不是四张指标卡。 */
function Figure({ value, unit }: { value: number; unit: string }) {
  return (
    <>
      <span className="font-medium tabular-nums text-foreground">{value.toLocaleString()}</span>{' '}
      {unit}
    </>
  );
}

function Sep() {
  return <span className="px-2 text-border">·</span>;
}

function TrustRow({
  ok,
  okText,
  badText,
  muted,
  mutedText,
  href,
}: {
  ok: boolean;
  okText: string;
  badText: string;
  muted?: boolean;
  mutedText?: string;
  href?: string;
}) {
  if (muted) {
    return <p className="text-sm text-muted-foreground">{mutedText}</p>;
  }
  return (
    <div className="flex items-start gap-2 text-sm">
      {/*
        未达成项用 warning 而不是 destructive：这张清单描述的是「还差什么」，
        不是「哪里坏了」。满屏红色会把一份能用的稿子渲染成事故现场。
      */}
      {ok ? (
        <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success-strong" />
      ) : (
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning-strong" />
      )}
      <span className={ok ? 'text-muted-foreground' : 'text-warning-strong'}>
        {ok ? okText : badText}
        {!ok && href && (
          <Link href={href} className="ml-1 underline underline-offset-4">
            去处理 →
          </Link>
        )}
      </span>
    </div>
  );
}

/**
 * 近期任务。
 *
 * 状态此前是彩色 Badge，五行任务就是五个色块。改成一列固定宽度的状态词 +
 * 一个小圆点：降级与失败仍然靠颜色一眼可辨，但不再有色块在页面上抢视线。
 */
function jobWarnings(job: Job): JobWarning[] {
  const raw = job.error?.warnings;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((warning) => {
    if (!warning || typeof warning !== 'object') return [];
    const record = warning as Record<string, unknown>;
    const stage = typeof record.stage === 'string' ? record.stage : job.stage ?? '未知阶段';
    const reason =
      typeof record.reason === 'string'
        ? record.reason
        : typeof record.error === 'string'
          ? record.error
          : '阶段未完成';
    return [{
      stage,
      reason,
      message: typeof record.message === 'string' ? record.message : undefined,
      count: typeof record.count === 'number' ? record.count : undefined,
    }];
  });
}

function jobBlockers(job: Job): Array<{ code: string; message: string }> {
  const raw = job.error?.blockers;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((blocker) => {
    if (!blocker || typeof blocker !== 'object') return [];
    const record = blocker as Record<string, unknown>;
    if (typeof record.message !== 'string') return [];
    return [{
      code: typeof record.code === 'string' ? record.code : 'quality_gate_blocked',
      message: record.message,
    }];
  });
}

function RecentJobs({
  jobs,
  projectId,
  paperType,
  onRetry,
}: {
  jobs: Job[];
  projectId: string;
  paperType: Project['paper_type'];
  onRetry: (stage: RetryableStage) => Promise<void>;
}) {
  const recent = jobs.slice(0, 5);
  const [expanded, setExpanded] = React.useState<string | null>(null);
  return (
    <section className="space-y-3">
      <SectionTitle>近期任务</SectionTitle>
      {recent.length === 0 ? (
        <p className="text-sm text-muted-foreground">还没有运行过任务。</p>
      ) : (
        <ul className="space-y-2">
          {recent.map((job) => {
            const warnings = jobWarnings(job);
            const blockers = jobBlockers(job);
            const evidenceBlocked = blockers.some((blocker) =>
              [
                'research_questions_missing',
                'question_evidence_coverage_low',
                'evidence_source_diversity_low',
                'evidence_classifier_rejected_majority',
                'no_fully_synthesized_question',
              ].includes(blocker.code),
            );
            const state = jobState(job.status, warnings.length);
            const open = expanded === job.id;
            return (
              <li key={job.id} className="border-b pb-2 last:border-0">
                <button
                  type="button"
                  onClick={() => setExpanded(open ? null : job.id)}
                  aria-expanded={open}
                  className="flex min-h-11 w-full items-center gap-3 text-left text-body focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <StatusDot state={state.state} label={state.label} className="w-24 shrink-0" />
                  <span className="min-w-0 flex-1 truncate">{stageLabel(job.stage)}</span>
                  <span className="shrink-0 text-meta text-muted-foreground">
                    {formatDate(job.created_at ?? undefined)}
                  </span>
                </button>
                {open && (
                  <div className="space-y-3 pb-2 text-meta text-muted-foreground sm:pl-28">
                    <Link href={`/projects/${projectId}/jobs/${job.id}`} className="text-foreground underline">查看执行详情与修复记录 →</Link>
                    {(job.error?.code as string | undefined) === 'job_abandoned' ? (
                      <div className="space-y-2">
                        {/* 被硬杀掉的任务（部署换掉了 worker 容器、进程被 OOM）没有任何
                            收尾代码会运行。此前这种行永远停在「进行中」，进度条一直走，
                            而且把项目锁着——用户点什么都是「已有任务正在运行」。 */}
                        <p className="font-medium text-warning-foreground">
                          这次运行被中断了：执行它的进程已经不存在（通常是服务更新或重启）。
                          已保留到中断前的产物，可以重新发起。
                        </p>
                      </div>
                    ) : job.status === 'needs_input' ? (
                      <div className="space-y-2">
                        <p className="font-medium text-warning-foreground">
                          {evidenceBlocked
                            ? '证据就绪门禁未通过，流程已在正文写作前停止。'
                            : '自动修复已达上限，已保留目前质量最好的稿件，未生成新导出件。'}
                        </p>
                        {blockers.length > 0 && (
                          <ul className="list-disc space-y-1 pl-4">
                            {blockers.map((blocker, index) => (
                              <li key={`${blocker.code}-${index}`}>{blocker.message}</li>
                            ))}
                          </ul>
                        )}
                        <Link
                          href={projectHref(
                            projectId,
                            paperType === 'original'
                              ? 'assets'
                              : evidenceBlocked
                                ? 'questions'
                                : 'write',
                          )}
                          className="text-foreground underline underline-offset-4"
                        >
                          {paperType === 'original'
                            ? '去补充研究素材'
                            : evidenceBlocked
                              ? '去查看问题—证据矩阵'
                              : '去查看质量报告'}{' '}
                          →
                        </Link>
                      </div>
                    ) : warnings.length > 0 ? (
                      <ul className="space-y-2">
                        {warnings.map((warning, index) => (
                          <li key={`${warning.stage}-${warning.reason}-${index}`} className="space-y-1">
                            <p>
                              <span className="font-medium text-foreground">
                                {stageLabel(warning.stage)}
                              </span>
                              ：{warning.reason}
                              {warning.count !== undefined ? `（${warning.count} 处）` : ''}
                              {warning.message ? ` — ${warning.message}` : ''}
                            </p>
                            {RETRYABLE_STAGES.has(warning.stage) && (
                              <Button
                                variant="outline"
                                size="xs"
                                onClick={() => void onRetry(warning.stage as RetryableStage)}
                              >
                                <RotateCw /> 重跑此阶段
                              </Button>
                            )}
                          </li>
                        ))}
                      </ul>
                    ) : job.status === 'failed' && job.stage && RETRYABLE_STAGES.has(job.stage) ? (
                      <Button
                        variant="outline"
                        size="xs"
                        onClick={() => void onRetry(job.stage as RetryableStage)}
                      >
                        <RotateCw /> 重跑{stageLabel(job.stage)}
                      </Button>
                    ) : (
                      <p>这次任务没有记录需要处理的告警。</p>
                    )}
                    {(job.stage === 'search' || warnings.some((warning) => warning.stage === 'search')) && (
                      <p>
                        也可以前往{' '}
                        <Link
                          href={projectHref(projectId, 'library')}
                          className="text-foreground underline underline-offset-4"
                        >
                          文献工作台
                        </Link>
                        {' '}更换检索源后再试。
                      </p>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function jobState(
  status: string,
  warnings: number,
): { label: string; state: StatusState } {
  if (status === 'succeeded') {
    return warnings > 0
      ? { label: '降级完成', state: 'degraded' }
      : { label: '完成', state: 'done' };
  }
  if (status === 'failed') {
    return { label: '失败', state: 'failed' };
  }
  if (status === 'paused') return { label: '已暂停', state: 'paused' };
  if (status === 'needs_input') return { label: '需补充材料', state: 'needs_input' };
  if (status === 'cancelled') return { label: '已取消', state: 'idle' };
  return { label: '进行中', state: 'running' };
}

/** 货币代码 → 金额前缀。未列入的币种直接打代码，猜一个符号比打 'SEK ' 更容易让人看错金额。 */
function currencySymbol(code: string | undefined): string {
  const key = (code ?? '').trim().toUpperCase();
  if (key === 'CNY') return '\u00a5';
  if (key === 'USD' || key === '') return '$';
  return `${key} `;
}

function CostSummary({ cost }: { cost: CostDetail | undefined }) {
  const failed = cost?.totals.failed_call_count ?? 0;
  const spend = cost?.totals.cost_estimate ?? 0;
  // 未定价的调用（没配价格，或 provider 没回 usage）让金额只是一个下界。
  // 图片生成目前三个 provider 都不报价，所以它也计入不完整。
  const unpricedCalls = cost?.totals.unpriced_call_count ?? 0;
  const unpricedImages = cost?.images?.unpriced_call_count ?? 0;
  const complete = (cost?.totals.cost_complete ?? true) && unpricedImages === 0;
  // 金额的货币由部署配置决定；把人民币印成 `$` 是这里唯一要防的事。
  const symbol = currencySymbol(cost?.totals.currency);
  return (
    <section className="space-y-3">
      <SectionTitle>成本</SectionTitle>
      <p className="text-sm text-muted-foreground">
        <Figure value={cost?.totals.call_count ?? 0} unit="次调用" />
        <Sep />
        <Figure value={cost?.totals.input_tokens ?? 0} unit="输入 tokens" />
        <Sep />
        <Figure value={cost?.totals.output_tokens ?? 0} unit="输出 tokens" />
        {(cost?.images?.call_count ?? 0) > 0 && (
          <>
            <Sep />
            <Figure value={cost?.images?.call_count ?? 0} unit="次图片生成" />
          </>
        )}
        {spend > 0 && (
          <>
            <Sep />
            <span className="font-medium tabular-nums text-foreground">
              {complete ? '' : '≥ '}
              {symbol}
              {spend.toFixed(4)}
            </span>
          </>
        )}
      </p>
      {!complete && (
        <p className="text-sm text-muted-foreground">
          {unpricedCalls + unpricedImages} 次调用无法估价
          {unpricedCalls > 0 ? '（该模型未配置 LLM_MODEL_PRICES，或供应商未返回 usage）' : ''}
          {unpricedImages > 0 ? '（图片生成暂无价格来源）' : ''}
          ，因此上面的金额是下界而不是账单。
        </p>
      )}
      {failed > 0 && (
        <p className="text-sm text-warning-strong">
          <span className="font-medium tabular-nums">{failed}</span> 次调用失败
        </p>
      )}
      {(cost?.images?.failed_call_count ?? 0) > 0 && (
        <p className="text-sm text-warning-strong">
          <span className="font-medium tabular-nums">{cost?.images?.failed_call_count}</span>{' '}
          次图片生成失败
        </p>
      )}
      {(cost?.images?.by_size?.length ?? 0) > 0 && (
        <div className="space-y-1 text-xs text-muted-foreground">
          {cost?.images?.by_size?.map((row, index) => (
            <p key={`${row.provider}-${row.model ?? ''}-${row.width ?? 0}-${row.height ?? 0}-${index}`}>
              {row.provider}{row.model ? ` / ${row.model}` : ''}
              {' · '}{row.width && row.height ? `${row.width}×${row.height}` : '未产出尺寸'}
              {' · '}{row.call_count} 次
              {row.cost_estimate > 0 ? ` · 估算 $${row.cost_estimate.toFixed(4)}` : ''}
            </p>
          ))}
        </div>
      )}
    </section>
  );
}

function VersionSummary({
  versions,
  restoringVersion,
  onRestore,
}: {
  versions: VersionHistory | undefined;
  restoringVersion: string | null;
  onRestore: (documentId: string, version: number) => Promise<void>;
}) {
  const docs = versions?.documents ?? [];
  const outlines = versions?.outlines ?? [];
  if (docs.length === 0 && outlines.length === 0) {
    return (
      <section className="space-y-3">
        <SectionTitle>版本</SectionTitle>
        <p className="text-sm text-muted-foreground">还没有文稿或大纲版本。</p>
      </section>
    );
  }
  return (
    <section className="space-y-3">
      <SectionTitle>版本</SectionTitle>
      <p className="text-sm text-muted-foreground">
        {docs.length > 0 && (
          <>
            文稿 <span className="font-medium tabular-nums text-foreground">v{docs[0].version}</span>
            （共 {docs.length} 版）
          </>
        )}
        {docs.length > 0 && outlines.length > 0 && <Sep />}
        {outlines.length > 0 && (
          <>
            大纲{' '}
            <span className="font-medium tabular-nums text-foreground">v{outlines[0].version}</span>
            （共 {outlines.length} 版）
          </>
        )}
      </p>
      {docs.length > 1 && (
        <div className="space-y-1 border-t pt-2">
          {docs.slice(0, 5).map((document) => (
            <div key={document.id} className="flex min-h-9 items-center justify-between gap-3 text-xs">
              <span className="text-muted-foreground">
                文稿 v{document.version} · {document.section_count ?? '—'} 章 · {formatDate(document.created_at ?? undefined)}
                {document.is_current && ' · 当前'}
              </span>
              {!document.is_current && (
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={restoringVersion !== null}
                  onClick={() => onRestore(document.id, document.version)}
                >
                  {restoringVersion === document.id && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  恢复此版
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
