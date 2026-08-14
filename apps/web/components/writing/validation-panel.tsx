'use client';

import * as React from 'react';
import Link from 'next/link';
import {
  CheckCircle2,
  ChevronDown,
  FileCheck2,
  MapPin,
  PencilLine,
  Sparkles,
  XCircle,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { SectionTitle } from '@/components/ui/section-title';
import { Select } from '@/components/ui/select';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ModuleError } from '@/components/layout/module-error';
import { useProjectData } from '@/components/project/project-context';
import { getClaimEvidence, reviewClaimEvidence } from '@/lib/api';
import { describeError } from '@/lib/errors';
import { projectHref } from '@/lib/pipeline';
import type {
  ClaimEvidence,
  CitationAudit,
  NumLintReport,
  PaperSection,
  QualityReport,
  QualityProfile,
  ReviewStyle,
  SectionIR,
} from '@/lib/types';
import { collectCiteKeys } from '@/lib/ir-serde';

/**
 * 配色约定：**质检发现项一律用 warning，不用 destructive。**
 *
 * 一处未绑定证据的论断、一个待补出处的数字，说的是稿子还差一步，不是系统出了故障。
 * 整面板染成红色会让人以为交付物坏了——而它其实已经生成、已经可以导出。红色留给
 * 真正的失败：请求报错、保存失败，以及会丢东西的破坏性操作。
 */
type ValidationScope = 'section' | 'document';
type DocumentTab = 'quality' | 'audit' | 'numbers';
type ValidationModuleState = {
  loading: boolean;
  ready: boolean;
  error: string | null;
  reload: () => void;
};
const READY_VALIDATION_STATE: ValidationModuleState = {
  loading: false,
  ready: true,
  error: null,
  reload: () => undefined,
};

/**
 * 右栏校验区。
 *
 * 关键改动：NUMLINT 从素材中心搬到这里。
 * 「正文里有 24 处无出处的数字」这条信息此前显示在 `/assets` 上——一个不含正文
 * 的页面；而 finding 本身带着 `section_key` 与 `context`，足够定位到出问题的章节。
 *
 * 「0 幻觉引用」在这里是一枚安静的绿色徽章，不是一张庆祝卡片：它是常态，
 * 不该每次都占掉右栏最显眼的位置。
 */
export function ValidationPanel({
  activeSection,
  activeDraft,
  audit,
  quality,
  numlint,
  auditState = READY_VALIDATION_STATE,
  qualityState = READY_VALIDATION_STATE,
  numlintState = READY_VALIDATION_STATE,
  showNumbers,
  onGenerateQuality,
  onRepairQuality,
  busy,
  qualityRunning,
  onJumpToSection,
}: {
  activeSection: PaperSection | undefined;
  activeDraft: SectionIR | null;
  audit: CitationAudit | undefined;
  quality: QualityReport | undefined;
  numlint: NumLintReport | undefined;
  auditState?: ValidationModuleState;
  qualityState?: ValidationModuleState;
  numlintState?: ValidationModuleState;
  showNumbers: boolean;
  onGenerateQuality: (qualityProfile: QualityProfile, reviewStyle: ReviewStyle) => void;
  onRepairQuality: (qualityProfile: QualityProfile, reviewStyle: ReviewStyle) => void;
  busy: boolean;
  qualityRunning: boolean;
  onJumpToSection: (sectionKey: string) => void;
}) {
  const [scope, setScope] = React.useState<ValidationScope>('section');
  const [documentTab, setDocumentTab] = React.useState<DocumentTab>('quality');

  const sectionKeys = activeDraft ? collectCiteKeys(activeDraft) : (activeSection?.cite_keys ?? []);
  const sectionWarnings = activeDraft?.citation_warnings ?? activeSection?.citation_warnings ?? [];
  const unsourcedHere = (numlint?.unsourced ?? []).filter(
    (f) => f.section_key === activeSection?.section_key,
  );

  return (
    <div className="space-y-4">
      <Tabs value={scope} onValueChange={(value) => setScope(value as ValidationScope)}>
        <TabsList className="grid w-full grid-cols-2">
          <TabsTrigger value="section">当前章节</TabsTrigger>
          <TabsTrigger value="document">整篇论文</TabsTrigger>
        </TabsList>
      </Tabs>

      {scope === 'section' && (
        <SectionValidation
          activeSection={activeSection}
          sectionKeys={sectionKeys}
          warnings={sectionWarnings}
          unsourced={unsourcedHere}
          showNumbers={showNumbers}
          numlintState={numlintState}
        />
      )}

      {scope === 'document' && (
        <div className="space-y-4">
          <Tabs
            value={documentTab}
            onValueChange={(value) => setDocumentTab(value as DocumentTab)}
          >
            <TabsList
              className={`grid w-full ${showNumbers ? 'grid-cols-3' : 'grid-cols-2'}`}
            >
              <TabsTrigger value="quality">质量门</TabsTrigger>
              <TabsTrigger value="audit">引用审计</TabsTrigger>
              {showNumbers && <TabsTrigger value="numbers">数字检查</TabsTrigger>}
            </TabsList>
          </Tabs>

          {documentTab === 'quality' && (
            <div className="space-y-3">
              <ModuleError label="质量报告" error={qualityState.error} onRetry={qualityState.reload} />
              {!qualityState.error && <QualityTab
                report={quality}
                onGenerate={onGenerateQuality}
                onRepair={onRepairQuality}
                disabled={busy}
                qualityRunning={qualityRunning}
                onJumpToSection={onJumpToSection}
              />}
            </div>
          )}
          {documentTab === 'audit' && (
            <div className="space-y-3">
              <ModuleError label="引用审计" error={auditState.error} onRetry={auditState.reload} />
              {!auditState.error && <AuditTab audit={audit} onJumpToSection={onJumpToSection} />}
            </div>
          )}
          {documentTab === 'numbers' && showNumbers && (
            <div className="space-y-3">
              <ModuleError label="数字检查" error={numlintState.error} onRetry={numlintState.reload} />
              {!numlintState.error && <NumbersTab report={numlint} onJumpToSection={onJumpToSection} />}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SectionValidation({
  activeSection,
  sectionKeys,
  warnings,
  unsourced,
  showNumbers,
  numlintState,
}: {
  activeSection: PaperSection | undefined;
  sectionKeys: string[];
  warnings: PaperSection['citation_warnings'];
  unsourced: NonNullable<NumLintReport['unsourced']>;
  showNumbers: boolean;
  numlintState: ValidationModuleState;
}) {
  if (!activeSection) {
    return (
      <p className="rounded-md border border-dashed p-4 text-meta text-muted-foreground">
        选择一个章节后，这里会显示只属于该章节的校验结果。
      </p>
    );
  }
  const numbersUnknown = showNumbers && Boolean(numlintState.error);
  const issueCount = warnings.length + (showNumbers && !numbersUnknown ? unsourced.length : 0);
  return (
    <div className="space-y-5 divide-y [&>section:not(:first-child)]:pt-5">
      <header className="space-y-2">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-meta text-muted-foreground">当前章节</p>
            <h3 className="truncate text-body font-medium">{activeSection.title}</h3>
          </div>
          <Badge variant={numbersUnknown ? 'muted' : issueCount === 0 ? 'success' : 'warning'}>
            {numbersUnknown ? '状态未知' : issueCount === 0 ? '未发现问题' : `${issueCount} 项待处理`}
          </Badge>
        </div>
        <p className="text-meta text-muted-foreground">
          本页只随当前章节变化；整篇投稿门、引用审计和数字检查请切到“整篇论文”。
        </p>
      </header>

      {numbersUnknown && (
        <ModuleError label="数字检查" error={numlintState.error} onRetry={numlintState.reload} />
      )}

      <section className="space-y-3">
        <SectionTitle>本章引用（{sectionKeys.length}）</SectionTitle>
        <div className="flex flex-wrap gap-1">
          {sectionKeys.map((key) => (
            <Badge key={key} variant="outline" className="font-mono text-meta">
              {key}
            </Badge>
          ))}
          {sectionKeys.length === 0 && (
            <span className="text-meta text-muted-foreground">本章暂无引用</span>
          )}
        </div>
        {warnings.length > 0 && (
          <div className="space-y-2">
            {warnings.map((warning, index) => (
              <div
                key={`${warning.path}-${index}`}
                className="rounded-md border border-warning/40 bg-warning/5 p-3 text-meta"
              >
                <p className="font-medium text-warning-strong">{warning.message}</p>
                {warning.rejected_keys.length > 0 && (
                  <p className="mt-1 font-mono text-muted-foreground">
                    {warning.rejected_keys.join(', ')}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {showNumbers && (
        <section className="space-y-3">
          <SectionTitle>本章数字（{unsourced.length} 项待核）</SectionTitle>
          {unsourced.length === 0 ? (
            <div className="flex items-start gap-2 text-meta text-muted-foreground">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success-strong" />
              本章未发现无法追溯的数值。
            </div>
          ) : (
            <div className="space-y-2">
              {unsourced.slice(0, 8).map((finding, index) => (
                <div key={`${finding.value}-${index}`} className="rounded-md bg-muted/50 p-2 text-meta">
                  <Badge variant="warning" className="mr-1.5 font-mono">
                    {finding.value}
                  </Badge>
                  <span className="text-muted-foreground">…{finding.context.slice(0, 60)}…</span>
                </div>
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function AuditTab({
  audit,
  onJumpToSection,
}: {
  audit: CitationAudit | undefined;
  onJumpToSection: (sectionKey: string) => void;
}) {
  if (!audit) {
    return (
      <p className="rounded-md border border-dashed p-3 text-meta text-muted-foreground">
        引用审计在正文生成后自动产出。
      </p>
    );
  }
  const hallucinated = audit.hallucinated_cite_keys;
  return (
    <div className="space-y-6 divide-y [&>section:not(:first-child)]:pt-6">
      <section>
        <header className="mb-3 flex items-start justify-between gap-3">
          <div>
            <h3 className="text-body font-medium">整篇引用审计</h3>
            <p className="text-meta text-muted-foreground">检查全文引用是否都在已核验白名单内。</p>
          </div>
          <Badge variant={hallucinated.length === 0 ? 'success' : 'warning'}>
            {hallucinated.length === 0 ? '0 个越权引用' : `${hallucinated.length} 个越权引用`}
          </Badge>
        </header>
        <div className="space-y-1.5 text-meta">
          <Row label="已用 / 白名单" value={`${audit.used_cite_keys.length} / ${audit.whitelist_size}`} />
          <Row label="被移除的引用" value={String(audit.removed_citation_warnings.length)} />
          <Row label="未被引用的文献" value={String(audit.unused_cite_keys.length)} />
        </div>
      </section>

      {audit.removed_citation_warnings.length > 0 && (
        <section>
          <header className="pb-2">
            <h3 className="text-body">已移除的引用</h3>
          </header>
          <div className="space-y-1 text-meta">
            {audit.removed_citation_warnings.map((warning, index) => (
              <button
                key={index}
                type="button"
                onClick={() => warning.section_key && onJumpToSection(warning.section_key)}
                className="block w-full rounded p-1 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {warning.section_key && (
                  <span className="text-muted-foreground">[{warning.section_key}] </span>
                )}
                {warning.message}
                <span className="ml-1 font-mono">{warning.rejected_keys.join(', ')}</span>
              </button>
            ))}
          </div>
        </section>
      )}

      {audit.unused_cite_keys.length > 0 && (
        <section>
          <header className="pb-2">
            <h3 className="text-body">
              未被引用的入库文献（{audit.unused_cite_keys.length}）
            </h3>
          </header>
          <div className="flex flex-wrap gap-1">
            {audit.unused_cite_keys.map((key) => (
              <Badge key={key} variant="muted" className="font-mono text-meta">
                {key}
              </Badge>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function NumbersTab({
  report,
  onJumpToSection,
}: {
  report: NumLintReport | undefined;
  onJumpToSection: (sectionKey: string) => void;
}) {
  if (!report) {
    return (
      <p className="rounded-md border border-dashed p-3 text-meta text-muted-foreground">
        数字一致性检查在正文生成后自动产出。
      </p>
    );
  }
  if (report.consistent) {
    return (
      <section className="space-y-2">
        <header>
          <h3 className="text-body font-medium">整篇数字检查</h3>
          <p className="text-meta text-muted-foreground">检查全文数字能否回溯到已上传素材。</p>
        </header>
        <div className="flex items-start gap-2 py-3 text-meta">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success-strong" />
          <span>
            正文数字与素材 100% 一致。已核 {report.checked_count} 处，
            {report.sourced_count} 处找到出处。
          </span>
        </div>
      </section>
    );
  }
  return (
    <section>
      <header className="pb-2">
        <h3 className="text-body font-medium text-warning-strong">
          {report.unsourced_count} 处数值待补出处
        </h3>
        <p className="text-meta text-muted-foreground">以下结果覆盖整篇论文，可点条目定位章节。</p>
      </header>
      <div className="space-y-1.5 text-meta">
        <p className="text-muted-foreground">
          这些数字在已上传的素材里找不到来源。点条目跳到所在章节核对。
        </p>
        {report.unsourced.slice(0, 20).map((finding, index) => (
          <button
            key={index}
            type="button"
            onClick={() => onJumpToSection(finding.section_key)}
            className="block w-full rounded p-1 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Badge variant="warning" className="mr-1.5 font-mono">
              {finding.value}
            </Badge>
            <span className="text-muted-foreground">
              [{finding.section_key}] …{finding.context.slice(0, 50)}…
            </span>
          </button>
        ))}
        {report.unsourced.length > 20 && (
          <p className="text-muted-foreground">仅显示前 20 条，共 {report.unsourced.length} 条。</p>
        )}
      </div>
    </section>
  );
}

function QualityTab({
  report,
  onGenerate,
  onRepair,
  disabled,
  qualityRunning,
  onJumpToSection,
}: {
  report: QualityReport | undefined;
  onGenerate: (qualityProfile: QualityProfile, reviewStyle: ReviewStyle) => void;
  onRepair: (qualityProfile: QualityProfile, reviewStyle: ReviewStyle) => void;
  disabled: boolean;
  qualityRunning: boolean;
  onJumpToSection: (sectionKey: string) => void;
}) {
  const { projectId } = useProjectData();
  const [qualityProfile, setQualityProfile] = React.useState<QualityProfile>(
    report?.quality_profile ?? 'scholarly',
  );
  const [reviewStyle, setReviewStyle] = React.useState<ReviewStyle>(
    report?.review_style ?? 'narrative',
  );
  const [evidence, setEvidence] = React.useState<ClaimEvidence[]>([]);
  const [evidenceLoading, setEvidenceLoading] = React.useState(false);
  const [evidenceError, setEvidenceError] = React.useState<string | null>(null);
  const [reviewing, setReviewing] = React.useState<Record<string, ClaimEvidence['manual_status']>>(
    {},
  );
  const [reviewErrors, setReviewErrors] = React.useState<Record<string, string>>({});
  const [editingReviewId, setEditingReviewId] = React.useState<string | null>(null);
  const [confirmRepair, setConfirmRepair] = React.useState(false);

  React.useEffect(() => {
    if (!report?.report_id) return;
    setQualityProfile(report.quality_profile);
    setReviewStyle(report.review_style);
  }, [report?.quality_profile, report?.report_id, report?.review_style]);

  React.useEffect(() => {
    let alive = true;
    setEvidence([]);
    setEvidenceLoading(Boolean(report?.report_id));
    setEvidenceError(null);
    setReviewing({});
    setReviewErrors({});
    setEditingReviewId(null);
    if (!report?.report_id) {
      return () => {
        alive = false;
      };
    }
    void getClaimEvidence(projectId, report.report_id, true)
      .then((result) => {
        if (alive) setEvidence(result.data);
      })
      .catch((error) => {
        if (alive) setEvidenceError(describeError(error));
      })
      .finally(() => {
        if (alive) setEvidenceLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [projectId, report?.report_id]);

  const reviewEvidence = async (
    anchor: ClaimEvidence,
    manualStatus: ClaimEvidence['manual_status'],
  ) => {
    setReviewing((current) => ({ ...current, [anchor.id]: manualStatus }));
    setReviewErrors((current) => {
      const next = { ...current };
      delete next[anchor.id];
      return next;
    });
    try {
      const updated = await reviewClaimEvidence(projectId, anchor.id, manualStatus);
      setEvidence((rows) => rows.map((row) => (row.id === updated.id ? updated : row)));
      setEditingReviewId(null);
    } catch (error) {
      setReviewErrors((current) => ({ ...current, [anchor.id]: describeError(error) }));
    } finally {
      setReviewing((current) => {
        const next = { ...current };
        delete next[anchor.id];
        return next;
      });
    }
  };

  const settings = (
    <details className="group rounded-md border">
      <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-3 text-meta font-medium">
        <span>
          检查设置
          <span className="ml-2 font-normal text-muted-foreground">
            {qualityProfile === 'draft'
              ? '快速草稿'
              : qualityProfile === 'submission'
                ? '严格投稿'
                : '学术严谨'}
            {' · '}
            {reviewStyle === 'systematic' ? '系统综述' : '叙述性综述'}
          </span>
        </span>
        <ChevronDown className="h-4 w-4 text-muted-foreground transition-transform group-open:rotate-180" />
      </summary>
      <div className="grid gap-3 border-t p-3 sm:grid-cols-2">
        <label className="space-y-1 text-meta text-muted-foreground">
          质量模式
          <Select
            value={qualityProfile}
            disabled={disabled}
            onChange={(event) => setQualityProfile(event.target.value as QualityProfile)}
          >
            <option value="draft">快速草稿</option>
            <option value="scholarly">学术严谨（推荐）</option>
            <option value="submission">严格投稿</option>
          </Select>
        </label>
        <label className="space-y-1 text-meta text-muted-foreground">
          综述方式
          <Select
            value={reviewStyle}
            disabled={disabled}
            onChange={(event) => setReviewStyle(event.target.value as ReviewStyle)}
          >
            <option value="narrative">叙述性综述</option>
            <option value="systematic">系统综述</option>
          </Select>
        </label>
        <p className="text-meta text-muted-foreground sm:col-span-2">
          设置只影响下一轮整篇检查，不会直接修改当前报告。
        </p>
      </div>
    </details>
  );

  if (!report) {
    return (
      <div className="space-y-4">
        <header className="space-y-3 border-b pb-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <p className="text-meta text-muted-foreground">整篇论文</p>
              <h3 className="text-body font-medium">投稿质量门</h3>
              <p className="mt-1 text-meta text-muted-foreground">
                尚未生成整篇质量报告。学术严谨模式会检查证据等级、可比较性与数字定位。
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              className="w-full sm:w-auto"
              onClick={() => onGenerate(qualityProfile, reviewStyle)}
              disabled={disabled}
              loading={qualityRunning}
              loadingLabel="正在检查整篇…"
            >
              <Sparkles className="h-4 w-4" />
              {disabled && !qualityRunning ? '其它任务运行中' : '生成整篇质量报告'}
            </Button>
          </div>
        </header>
        {settings}
      </div>
    );
  }

  const metrics = [
    { label: '总字数', value: report.word_count.toLocaleString() },
    { label: '引用条数', value: String(report.cite_count) },
    { label: '每千字引用', value: report.citation_density.toFixed(1) },
    { label: '文献利用率', value: `${Math.round(report.library_coverage * 100)}%` },
    { label: '近 5 年占比', value: `${Math.round(report.recent_ratio * 100)}%` },
    { label: '全文卡片覆盖', value: `${Math.round(report.fulltext_coverage * 100)}%` },
    {
      label: '核心论断全文覆盖',
      value: `${Math.round(report.core_claim_fulltext_coverage * 100)}%`,
    },
  ];
  const depthMetricLabels: Array<[string, string, boolean]> = [
    ['1_fulltext_acquisition_rate', '1 全文获取率', false],
    ['2_methods_results_section_coverage', '2 方法/结果定位覆盖', false],
    ['3_structured_object_coverage', '3 表/式/图结构化覆盖', false],
    ['4_core_claim_evidence_coverage', '4 核心论断证据覆盖', false],
    ['5_numeric_locator_coverage', '5 数字定位覆盖', false],
    ['6_numeric_source_consistency', '6 数值原文一致', false],
    ['7_unsupported_strong_claim_rate', '7 无依据强结论率', true],
    ['8_invalid_comparison_rate', '8 错误比较率', true],
    ['9_cross_study_synthesis_paragraph_rate', '9 跨研究综合段落', false],
    ['10_paper_enumeration_paragraph_rate', '10 按论文罗列段落', true],
    ['11_question_answer_completeness', '11 问题回答完整度', false],
  ];
  const blockers = report.blockers ?? [];
  const hasEvidenceBlocker = blockers.some((blocker) =>
    EVIDENCE_BLOCKER_CODES.has(blocker.code),
  );
  const blockingClaims = unresolvedCoreClaims(evidence);

  return (
    <>
    <div className="space-y-6 divide-y [&>section:not(:first-child)]:pt-6">
      <header className="space-y-4 border-b pb-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 space-y-2">
            <div>
              <p className="text-meta text-muted-foreground">整篇论文</p>
              <h3 className="text-body font-medium">投稿质量门</h3>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Badge
                variant={
                  report.readiness_status === 'submission_ready' ||
                  report.readiness_status === 'preflight_ready'
                    ? 'success'
                    : report.readiness_status === 'needs_revision'
                      ? 'warning'
                      : 'muted'
                }
              >
                {report.readiness_status === 'submission_ready'
                  ? '可提交'
                  : report.readiness_status === 'preflight_ready'
                    ? '内容预检通过，待 PDF 验收'
                    : report.readiness_status === 'needs_revision'
                      ? '需要修订'
                      : '草稿'}
              </Badge>
              <span className="text-meta text-muted-foreground">
                {blockers.length > 0 ? `${blockers.length} 项阻断` : '无投稿阻断项'}
              </span>
              {report.stale && <Badge variant="warning">报告已过期</Badge>}
            </div>
            {report.generated_at && (
              <p className="text-micro text-muted-foreground">
                最近检查：{new Date(report.generated_at).toLocaleString('zh-CN')}
              </p>
            )}
            <Link
              href={projectHref(projectId, 'evidence')}
              className="inline-flex min-h-8 items-center text-meta font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              查看证据矩阵
            </Link>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="w-full sm:w-auto"
            onClick={() => onGenerate(qualityProfile, reviewStyle)}
            disabled={disabled}
            loading={qualityRunning}
            loadingLabel="正在检查整篇…"
          >
            <FileCheck2 className="h-4 w-4" />
            {disabled && !qualityRunning ? '其它任务运行中' : '重新检查整篇'}
          </Button>
        </div>
        {settings}
      </header>

      {blockers.length > 0 && (
        <section>
          <header className="pb-2">
            <h3 className="text-body text-warning-strong">
              投稿阻断项（{blockers.length}）
            </h3>
          </header>
          <div className="space-y-2 text-meta">
            {blockers.map((blocker) => (
              <div
                key={blocker.code}
                className="rounded-md border border-warning/40 bg-warning/5 p-3 text-warning-strong"
              >
                {blocker.message}
              </div>
            ))}
            {hasEvidenceBlocker && (
              <BlockingEvidenceTasks
                claims={blockingClaims}
                hasEvidence={evidence.length > 0}
                loading={evidenceLoading}
                disabled={disabled}
                qualityRunning={qualityRunning}
                projectId={projectId}
                onAutoRepair={() => setConfirmRepair(true)}
                onRecheck={() => onGenerate(qualityProfile, reviewStyle)}
                onJumpToSection={onJumpToSection}
              />
            )}
          </div>
        </section>
      )}

      {evidenceError && (
        <p
          role="alert"
          className="rounded-md border border-destructive/40 bg-card p-3 text-meta text-destructive-strong"
        >
          核心论断证据加载失败：{evidenceError}
        </p>
      )}

      {evidence.length > 0 && (
        <EvidenceReviewList
          evidence={evidence}
          reviewing={reviewing}
          errors={reviewErrors}
          editingReviewId={editingReviewId}
          onEdit={setEditingReviewId}
          onReview={reviewEvidence}
          onJumpToSection={onJumpToSection}
        />
      )}

      {report.hints.length > 0 && (
        <section>
          <header className="pb-2">
            <h3 className="text-body">覆盖建议</h3>
          </header>
          <div className="space-y-1 text-meta text-muted-foreground">
            {report.hints.map((hint, index) => (
              <p key={index}>· {hint.message}</p>
            ))}
          </div>
        </section>
      )}

      {report.soft_check.length > 0 && (
        <section>
          <header className="pb-2">
            <h3 className="text-body">语义引用软校验（弱相关）</h3>
          </header>
          <div className="space-y-1.5 text-meta">
            {report.soft_check.map((finding, index) => (
              <p key={index}>
                <Badge variant="warning" className="mr-1.5 font-mono">
                  {finding.cite_key}
                </Badge>
                {finding.reason}
              </p>
            ))}
          </div>
        </section>
      )}

      <section>
        <header className="pb-2">
          <h3 className="text-body">质量评分（仅提示，不设门槛）</h3>
        </header>
        <div className="grid grid-cols-2 gap-2">
          {metrics.map((metric) => (
            <div key={metric.label} className="rounded-md border p-2 text-center">
              <p className="text-base font-semibold tabular-nums">{metric.value}</p>
              <p className="text-meta text-muted-foreground">{metric.label}</p>
            </div>
          ))}
        </div>
      </section>

      {Object.keys(report.depth_metrics ?? {}).length > 0 && (
        <section>
          <header className="pb-2">
            <h3 className="text-body">问题驱动深度指标</h3>
            <p className="text-meta text-muted-foreground">
              绿色趋势通常更好；标有“越低越好”的指标用于暴露过度推断和罗列。
            </p>
          </header>
          <div className="grid grid-cols-2 gap-2">
            {depthMetricLabels.map(([key, label, lowerIsBetter]) => {
              const raw = report.depth_metrics[key];
              if (typeof raw !== 'number') return null;
              return (
                <div key={key} className="rounded-md border p-2">
                  <p className="text-base font-semibold tabular-nums">
                    {Math.round(raw * 100)}%
                  </p>
                  <p className="text-meta text-muted-foreground">{label}</p>
                  {lowerIsBetter && (
                    <p className="text-micro text-muted-foreground">越低越好</p>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}

    </div>
    <Dialog
      open={confirmRepair}
      onClose={() => setConfirmRepair(false)}
      title="自动补证据并修订全文？"
      description="系统会尝试补充可定位全文证据，并只重写未通过质量门的章节。"
      footer={
        <>
          <Button variant="outline" onClick={() => setConfirmRepair(false)}>
            取消
          </Button>
          <Button
            onClick={() => {
              setConfirmRepair(false);
              onRepair(qualityProfile, reviewStyle);
            }}
          >
            <Sparkles /> 开始自动修复
          </Button>
        </>
      }
    >
      <div className="space-y-3 text-sm text-muted-foreground">
        <p>自动修复可能检索并纳入新的全文来源，因此通常需要几分钟。</p>
        <p>
          每轮改写前都会保存当前正文；只有阻断项减少且没有引入关键退化时才接受新稿，
          否则恢复原文。完成后会自动生成新的质量报告。
        </p>
      </div>
    </Dialog>
    </>
  );
}

const EVIDENCE_BLOCKER_CODES = new Set([
  'claim_evidence_missing',
  'core_claim_fulltext_missing',
  'original_claim_source_missing',
  'supported_core_claims_missing',
  'evidence_binding_missing',
  'evidence_grade_violation',
  'comparison_not_comparable',
  'numeric_locator_missing',
]);

function claimEvidenceIsResolved(anchor: ClaimEvidence): boolean {
  const located = Boolean(
    anchor.source_page !== null && anchor.source_page !== undefined ||
      anchor.source_section ||
      anchor.source_paragraph !== null && anchor.source_paragraph !== undefined,
  );
  return Boolean(
    ['fulltext', 'user_asset'].includes(anchor.source_kind) &&
      anchor.grade_ok !== false &&
      anchor.comparability_ok !== false &&
      anchor.manual_status !== 'rejected' &&
      (anchor.support_status === 'supported' ||
        (anchor.manual_status === 'confirmed' && located)),
  );
}

function unresolvedCoreClaims(evidence: ClaimEvidence[]): ClaimEvidence[] {
  const groups = new Map<string, ClaimEvidence[]>();
  evidence
    .filter((anchor) => anchor.is_core)
    .forEach((anchor) => {
      const key = `${anchor.section_key}\u0000${anchor.claim_text}`;
      groups.set(key, [...(groups.get(key) ?? []), anchor]);
    });
  return Array.from(groups.values())
    .filter((anchors) => !anchors.some(claimEvidenceIsResolved))
    .map(
      (anchors) =>
        anchors.find((anchor) => anchor.manual_status === 'confirmed') ?? anchors[0],
    );
}

function evidenceProblem(anchor: ClaimEvidence): string {
  const located = Boolean(
    anchor.source_page !== null && anchor.source_page !== undefined ||
      anchor.source_section ||
      anchor.source_paragraph !== null && anchor.source_paragraph !== undefined,
  );
  if (anchor.manual_status === 'confirmed' && !located) {
    return '你已确认这段摘录，但它没有页码、章节或段落定位。人工确认不能替代可追溯定位。';
  }
  if (anchor.manual_status === 'rejected') {
    return '这条证据已被人工标记为不支持，需要换证据，或删除、弱化正文论断。';
  }
  if (anchor.source_kind === 'abstract' || anchor.support_status === 'abstract_only') {
    return '当前只有摘要证据；核心论断必须由可定位的全文内容支撑。';
  }
  if (!located || anchor.support_status === 'fulltext_unlocated') {
    return '已找到全文，但证据没有页码、章节或段落定位。';
  }
  if (anchor.grade_ok === false) {
    return '证据等级不足以支撑这一类强论断，需要更直接的方法、结果或结构化证据。';
  }
  if (anchor.comparability_ok === false) {
    return '被比较的研究对象或指标不可直接比较，需要改写比较口径或更换证据。';
  }
  if (anchor.support_status === 'insufficient_support') {
    return '证据摘录只支持论断的一部分，需要换证据，或删除、弱化超出证据的表述。';
  }
  return '当前证据没有闭合该核心论断，需要补充定位证据或修改正文。';
}

function BlockingEvidenceTasks({
  claims,
  hasEvidence,
  loading,
  disabled,
  qualityRunning,
  projectId,
  onAutoRepair,
  onRecheck,
  onJumpToSection,
}: {
  claims: ClaimEvidence[];
  hasEvidence: boolean;
  loading: boolean;
  disabled: boolean;
  qualityRunning: boolean;
  projectId: string;
  onAutoRepair: () => void;
  onRecheck: () => void;
  onJumpToSection: (sectionKey: string) => void;
}) {
  const awaitingRecheck = !loading && hasEvidence && claims.length === 0;
  return (
    <div className="space-y-3 rounded-md border border-primary/30 bg-card p-3 text-foreground">
      <div className="space-y-1">
        <p className="font-medium">可以从这里继续处理</p>
        <p className="text-muted-foreground">
          {awaitingRecheck
            ? '当前人工判定已经闭合核心论断；上方阻断来自判定前的旧报告，不需要修改正文。'
            : '自动修复会补充全文证据并局部改写失败章节；也可以定位到章节手动删除、弱化或换证据。'}
        </p>
      </div>
      {awaitingRecheck ? (
        <div className="grid gap-2 sm:grid-cols-2">
          <Button
            size="sm"
            onClick={onRecheck}
            disabled={disabled}
            loading={qualityRunning}
            loadingLabel="正在应用判定…"
          >
            <FileCheck2 />
            {disabled && !qualityRunning ? '其它任务运行中' : '应用判定并重新检查'}
          </Button>
          <Button variant="outline" size="sm" onClick={onAutoRepair} disabled={disabled}>
            <Sparkles /> 仍然自动修复
          </Button>
        </div>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          <Button
            size="sm"
            onClick={onAutoRepair}
            disabled={disabled || loading}
            loading={qualityRunning}
            loadingLabel="正在自动修复…"
          >
            <Sparkles />
            {disabled && !qualityRunning ? '其它任务运行中' : '自动补证据并修订'}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => claims[0] && onJumpToSection(claims[0].section_key)}
            disabled={claims.length === 0}
          >
            <MapPin /> 定位第一条论断
          </Button>
        </div>
      )}

      {loading && <p className="text-muted-foreground">正在定位对应的核心论断…</p>}
      {!loading && claims.length > 0 && (
        <div className="space-y-2">
          <p className="font-medium">待处理论断（{claims.length}）</p>
          {claims.map((anchor) => (
            <article key={anchor.id} className="space-y-2 rounded-md border bg-background p-3">
              <div className="flex items-start justify-between gap-2">
                <p className="leading-relaxed">{anchor.claim_text}</p>
                <Badge variant="warning" className="shrink-0">待修复</Badge>
              </div>
              <p className="text-muted-foreground">{evidenceProblem(anchor)}</p>
              <Button
                variant="outline"
                size="sm"
                onClick={() => onJumpToSection(anchor.section_key)}
                aria-label={`手动修复：定位到章节 ${anchor.section_key}`}
              >
                <MapPin /> 定位到章节 {anchor.section_key}
              </Button>
            </article>
          ))}
        </div>
      )}
      {!loading && !hasEvidence && claims.length === 0 && (
        <p className="text-muted-foreground">
          当前报告没有返回可定位的论断条目，可先运行自动修复，或到证据矩阵检查全文来源。
        </p>
      )}

      <details className="group rounded-md border">
        <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-2 px-3 font-medium">
          手动处理方法
          <ChevronDown className="h-4 w-4 text-muted-foreground transition-transform group-open:rotate-180" />
        </summary>
        <ol className="list-decimal space-y-1.5 border-t px-7 py-3 text-muted-foreground">
          <li>定位到章节，找到上方列出的完整论断。</li>
          <li>删除或弱化证据没有覆盖的表述，或者换用带页码、章节或段落定位的全文证据。</li>
          <li>保存章节，再点击“重新检查整篇”。</li>
        </ol>
        <Link
          href={projectHref(projectId, 'evidence')}
          className="mx-3 mb-3 inline-flex min-h-9 items-center text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          打开证据矩阵
        </Link>
      </details>
    </div>
  );
}

const SUPPORT_STATUS_LABELS: Record<string, string> = {
  supported: '机器校验：支持',
  insufficient_support: '机器校验：支持不足',
  abstract_only: '仅摘要证据',
  missing_evidence: '缺少证据',
};

function EvidenceReviewList({
  evidence,
  reviewing,
  errors,
  editingReviewId,
  onEdit,
  onReview,
  onJumpToSection,
}: {
  evidence: ClaimEvidence[];
  reviewing: Record<string, ClaimEvidence['manual_status']>;
  errors: Record<string, string>;
  editingReviewId: string | null;
  onEdit: (id: string | null) => void;
  onReview: (
    anchor: ClaimEvidence,
    status: ClaimEvidence['manual_status'],
  ) => Promise<void>;
  onJumpToSection: (sectionKey: string) => void;
}) {
  return (
    <section>
      <header className="pb-3">
        <h3 className="text-body font-medium">全部核心论断证据（{evidence.length}）</h3>
        <p className="text-meta text-muted-foreground">
          这里审阅整篇论文的核心论断。人工判定保存后，需重新检查整篇才能更新质量门。
        </p>
      </header>
      <div className="space-y-3 text-meta">
        {evidence.slice(0, 10).map((anchor) => {
          const pendingStatus = reviewing[anchor.id];
          const isPending = Boolean(pendingStatus);
          const editing = editingReviewId === anchor.id;
          const reviewed = anchor.manual_status !== 'unreviewed';
          const confirmed = anchor.manual_status === 'confirmed';
          const resolved = claimEvidenceIsResolved(anchor);
          return (
            <article key={anchor.id} className="overflow-hidden rounded-md border bg-card">
              <div className="space-y-3 p-3">
                <div className="space-y-1.5">
                  <div className="flex items-start justify-between gap-2">
                    <p className="font-medium leading-relaxed">{anchor.claim_text}</p>
                    <Button
                      variant="ghost"
                      size="xs"
                      className="shrink-0"
                      onClick={() => onJumpToSection(anchor.section_key)}
                      aria-label={`定位到章节 ${anchor.section_key}`}
                    >
                      {anchor.section_key}
                    </Button>
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5 text-muted-foreground">
                    <span className="font-mono">{anchor.cite_key ?? anchor.source_key ?? '无来源'}</span>
                    <span aria-hidden="true">·</span>
                    <span>{anchor.source_kind}</span>
                    {anchor.source_page !== null && anchor.source_page !== undefined && (
                      <span>第 {anchor.source_page} 页</span>
                    )}
                    {anchor.source_section && <span>{anchor.source_section}</span>}
                    <Badge
                      variant={anchor.support_status === 'supported' ? 'success' : 'warning'}
                    >
                      {SUPPORT_STATUS_LABELS[anchor.support_status] ?? anchor.support_status}
                    </Badge>
                  </div>
                </div>

                {anchor.evidence_excerpt && (
                  <blockquote className="line-clamp-4 rounded-md bg-muted/50 p-3 leading-relaxed text-muted-foreground">
                    {anchor.evidence_excerpt}
                  </blockquote>
                )}

                {anchor.entailment_verdict && (
                  <div className="space-y-1.5 rounded-md border bg-muted/20 p-3">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge
                        variant={
                          anchor.entailment_verdict === 'supported'
                            ? 'success'
                            : anchor.entailment_verdict === 'partial' ||
                                anchor.entailment_verdict === 'uncertain'
                              ? 'warning'
                              : 'destructive'
                        }
                      >
                        语义核验：{anchor.entailment_verdict}
                      </Badge>
                      {anchor.entailment_confidence !== null &&
                        anchor.entailment_confidence !== undefined && (
                          <span className="text-muted-foreground">
                            置信度 {(anchor.entailment_confidence * 100).toFixed(0)}%
                          </span>
                        )}
                      {anchor.entailment_cached && <Badge variant="muted">缓存复用</Badge>}
                    </div>
                    {anchor.entailment_reason && (
                      <p className="leading-relaxed text-muted-foreground">
                        {anchor.entailment_reason}
                      </p>
                    )}
                  </div>
                )}

                {errors[anchor.id] && (
                  <p
                    role="alert"
                    className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-destructive-strong"
                  >
                    保存失败：{errors[anchor.id]}。请重试。
                  </p>
                )}
              </div>

              {!reviewed && (
                <div className="grid grid-cols-2 gap-2 border-t bg-muted/20 p-3">
                  <Button
                    variant="default"
                    size="sm"
                    onClick={() => void onReview(anchor, 'confirmed')}
                    disabled={isPending}
                    loading={pendingStatus === 'confirmed'}
                    loadingLabel="保存中…"
                  >
                    <CheckCircle2 /> 确认证据
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void onReview(anchor, 'rejected')}
                    disabled={isPending}
                    loading={pendingStatus === 'rejected'}
                    loadingLabel="保存中…"
                  >
                    <XCircle /> 标记不支持
                  </Button>
                </div>
              )}

              {reviewed && (
                <div className="border-t p-3" aria-live="polite">
                  <div
                    className={`flex items-start gap-2 rounded-md p-3 ${
                      confirmed
                        ? 'bg-success/10 text-success-strong'
                        : 'bg-warning/10 text-warning-strong'
                    }`}
                  >
                    {confirmed ? (
                      <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                    ) : (
                      <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
                    )}
                    <div className="min-w-0 flex-1">
                      <p className="font-medium">
                        {confirmed ? '人工已确认证据' : '人工已判定为不支持'}
                      </p>
                      <p className="mt-0.5 text-micro opacity-80">
                        {confirmed && !resolved
                          ? '判定已保存，但定位或证据相符性仍未满足质量门；请使用上方修复入口。'
                          : '已保存；重新检查整篇后会计入投稿质量门。'}
                      </p>
                    </div>
                    <Button
                      variant="ghost"
                      size="xs"
                      className="shrink-0"
                      onClick={() => onEdit(editing ? null : anchor.id)}
                      disabled={isPending}
                      aria-expanded={editing}
                    >
                      <PencilLine /> 更改判定
                    </Button>
                  </div>

                  {editing && (
                    <div className="mt-2 flex flex-wrap justify-end gap-2 rounded-md border bg-background p-2">
                      <Button
                        variant={confirmed ? 'destructive' : 'default'}
                        size="sm"
                        onClick={() =>
                          void onReview(anchor, confirmed ? 'rejected' : 'confirmed')
                        }
                        disabled={isPending}
                        loading={
                          pendingStatus === (confirmed ? 'rejected' : 'confirmed')
                        }
                        loadingLabel="保存中…"
                      >
                        {confirmed ? '改为不支持' : '改为确认'}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => void onReview(anchor, 'unreviewed')}
                        disabled={isPending}
                        loading={pendingStatus === 'unreviewed'}
                        loadingLabel="保存中…"
                      >
                        撤销判定
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => onEdit(null)} disabled={isPending}>
                        取消
                      </Button>
                    </div>
                  )}
                </div>
              )}
            </article>
          );
        })}
        {evidence.length > 10 && (
          <p className="text-muted-foreground">仅显示前 10 条，共 {evidence.length} 条。</p>
        )}
      </div>
    </section>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span className="tabular-nums">{value}</span>
    </div>
  );
}
