'use client';

import * as React from 'react';
import { AlertTriangle, CheckCircle2, Sparkles } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Select } from '@/components/ui/select';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useProject } from '@/components/project/project-context';
import { getClaimEvidence, reviewClaimEvidence } from '@/lib/api';
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

type Tab = 'section' | 'audit' | 'numbers' | 'quality';

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
  showNumbers,
  onGenerateQuality,
  busy,
  onJumpToSection,
}: {
  activeSection: PaperSection | undefined;
  activeDraft: SectionIR | null;
  audit: CitationAudit | undefined;
  quality: QualityReport | undefined;
  numlint: NumLintReport | undefined;
  showNumbers: boolean;
  onGenerateQuality: (qualityProfile: QualityProfile, reviewStyle: ReviewStyle) => void;
  busy: boolean;
  onJumpToSection: (sectionKey: string) => void;
}) {
  const [tab, setTab] = React.useState<Tab>('section');

  const hallucinated = audit?.hallucinated_cite_keys ?? [];
  const sectionKeys = activeDraft ? collectCiteKeys(activeDraft) : (activeSection?.cite_keys ?? []);
  const unsourcedHere = (numlint?.unsourced ?? []).filter(
    (f) => f.section_key === activeSection?.section_key,
  );

  return (
    <div className="space-y-3">
      {/* 顶部信任摘要：一眼看完，不用切 tab。 */}
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant={hallucinated.length === 0 ? 'success' : 'destructive'} className="gap-1">
          {hallucinated.length === 0 ? (
            <>
              <CheckCircle2 className="h-3 w-3" /> 0 幻觉引用
            </>
          ) : (
            <>
              <AlertTriangle className="h-3 w-3" /> {hallucinated.length} 个越权引用
            </>
          )}
        </Badge>
        {showNumbers && numlint && (
          <Badge variant={numlint.consistent ? 'success' : 'destructive'} className="gap-1">
            {numlint.consistent ? (
              <>
                <CheckCircle2 className="h-3 w-3" /> 数字可溯源
              </>
            ) : (
              <>
                <AlertTriangle className="h-3 w-3" /> {numlint.unsourced_count} 处无出处
              </>
            )}
          </Badge>
        )}
      </div>

      <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
        <TabsList className="w-full">
          <TabsTrigger value="section">本节</TabsTrigger>
          <TabsTrigger value="audit">引用</TabsTrigger>
          {showNumbers && <TabsTrigger value="numbers">数字</TabsTrigger>}
          <TabsTrigger value="quality">质量</TabsTrigger>
        </TabsList>
      </Tabs>

      {tab === 'section' && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">本节引用（{sectionKeys.length}）</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="flex flex-wrap gap-1">
              {sectionKeys.map((key) => (
                <Badge key={key} variant="outline" className="font-mono text-xs">
                  {key}
                </Badge>
              ))}
              {sectionKeys.length === 0 && (
                <span className="text-xs text-muted-foreground">本节暂无引用</span>
              )}
            </div>
            {showNumbers && unsourcedHere.length > 0 && (
              <div className="space-y-1 border-t pt-2">
                <p className="text-xs font-medium text-destructive-strong">
                  本节 {unsourcedHere.length} 处数值无出处
                </p>
                {unsourcedHere.slice(0, 5).map((finding, index) => (
                  <p key={index} className="text-xs text-muted-foreground">
                    <Badge variant="destructive" className="mr-1 font-mono">
                      {finding.value}
                    </Badge>
                    …{finding.context.slice(0, 40)}…
                  </p>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {tab === 'audit' && <AuditTab audit={audit} onJumpToSection={onJumpToSection} />}

      {tab === 'numbers' && showNumbers && (
        <NumbersTab report={numlint} onJumpToSection={onJumpToSection} />
      )}

      {tab === 'quality' && (
        <QualityTab report={quality} onGenerate={onGenerateQuality} disabled={busy} />
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
      <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
        引用审计在正文生成后自动产出。
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <Card>
        <CardContent className="space-y-1.5 py-3 text-xs">
          <Row label="已用 / 白名单" value={`${audit.used_cite_keys.length} / ${audit.whitelist_size}`} />
          <Row label="被移除的引用" value={String(audit.removed_citation_warnings.length)} />
          <Row label="未被引用的文献" value={String(audit.unused_cite_keys.length)} />
        </CardContent>
      </Card>

      {audit.removed_citation_warnings.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">已移除的引用</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-xs">
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
          </CardContent>
        </Card>
      )}

      {audit.unused_cite_keys.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">
              未被引用的入库文献（{audit.unused_cite_keys.length}）
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-1">
            {audit.unused_cite_keys.map((key) => (
              <Badge key={key} variant="muted" className="font-mono text-xs">
                {key}
              </Badge>
            ))}
          </CardContent>
        </Card>
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
      <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
        数字一致性检查在正文生成后自动产出。
      </p>
    );
  }
  if (report.consistent) {
    return (
      <Card>
        <CardContent className="flex items-start gap-2 py-3 text-xs">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success-strong" />
          <span>
            正文数字与素材 100% 一致。已核 {report.checked_count} 处，
            {report.sourced_count} 处找到出处。
          </span>
        </CardContent>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm text-destructive-strong">
          {report.unsourced_count} 处数值无出处
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-1.5 text-xs">
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
            <Badge variant="destructive" className="mr-1.5 font-mono">
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
      </CardContent>
    </Card>
  );
}

function QualityTab({
  report,
  onGenerate,
  disabled,
}: {
  report: QualityReport | undefined;
  onGenerate: (qualityProfile: QualityProfile, reviewStyle: ReviewStyle) => void;
  disabled: boolean;
}) {
  const { projectId } = useProject();
  const [qualityProfile, setQualityProfile] = React.useState<QualityProfile>(
    report?.quality_profile ?? 'draft',
  );
  const [reviewStyle, setReviewStyle] = React.useState<ReviewStyle>(
    report?.review_style ?? 'narrative',
  );
  const [evidence, setEvidence] = React.useState<ClaimEvidence[]>([]);

  React.useEffect(() => {
    if (!report?.report_id) {
      setEvidence([]);
      return;
    }
    void getClaimEvidence(projectId, report.report_id, true).then((result) => {
      setEvidence(result.data);
    });
  }, [projectId, report?.report_id]);

  const reviewEvidence = async (
    anchor: ClaimEvidence,
    manualStatus: ClaimEvidence['manual_status'],
  ) => {
    const updated = await reviewClaimEvidence(projectId, anchor.id, manualStatus);
    setEvidence((rows) => rows.map((row) => (row.id === updated.id ? updated : row)));
  };

  const controls = (
    <div className="grid gap-2 sm:grid-cols-2">
      <label className="space-y-1 text-xs text-muted-foreground">
        质量模式
        <Select
          value={qualityProfile}
          onChange={(event) => setQualityProfile(event.target.value as QualityProfile)}
        >
          <option value="draft">快速草稿</option>
          <option value="submission">严格投稿</option>
        </Select>
      </label>
      <label className="space-y-1 text-xs text-muted-foreground">
        综述方式
        <Select
          value={reviewStyle}
          onChange={(event) => setReviewStyle(event.target.value as ReviewStyle)}
        >
          <option value="narrative">叙述性综述</option>
          <option value="systematic">系统综述</option>
        </Select>
      </label>
    </div>
  );

  if (!report) {
    return (
      <div className="flex flex-col gap-3 rounded-md border border-dashed p-4">
        {controls}
        <p className="text-xs text-muted-foreground">
          快速草稿只给提示；严格投稿会阻断占位符、未定位全文证据、未审批章节和版面错误。
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onGenerate(qualityProfile, reviewStyle)}
          disabled={disabled}
        >
          <Sparkles className="h-4 w-4" /> 生成质量报告
        </Button>
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

  return (
    <div className="space-y-3">
      {controls}
      <div className="flex flex-wrap items-center gap-2">
        <Badge
          variant={
            report.readiness_status === 'submission_ready' ||
            report.readiness_status === 'preflight_ready'
              ? 'success'
              : report.readiness_status === 'needs_revision'
                ? 'destructive'
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
        {report.stale && <Badge variant="warning">报告已过期，请重新生成</Badge>}
        <Button
          variant="outline"
          size="sm"
          onClick={() => onGenerate(qualityProfile, reviewStyle)}
          disabled={disabled}
        >
          重新检查
        </Button>
      </div>

      {report.blockers.length > 0 && (
        <Card className="border-destructive/40">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-destructive-strong">
              投稿阻断项（{report.blockers.length}）
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-xs">
            {report.blockers.map((blocker) => (
              <p key={blocker.code}>· {blocker.message}</p>
            ))}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">质量评分（仅提示，不设门槛）</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-2 gap-2">
          {metrics.map((metric) => (
            <div key={metric.label} className="rounded-md border p-2 text-center">
              <p className="text-base font-semibold tabular-nums">{metric.value}</p>
              <p className="text-xs text-muted-foreground">{metric.label}</p>
            </div>
          ))}
        </CardContent>
      </Card>

      {report.hints.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">覆盖建议</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-xs text-muted-foreground">
            {report.hints.map((hint, index) => (
              <p key={index}>· {hint.message}</p>
            ))}
          </CardContent>
        </Card>
      )}

      {report.soft_check.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">语义引用软校验（弱相关）</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5 text-xs">
            {report.soft_check.map((finding, index) => (
              <p key={index}>
                <Badge variant="warning" className="mr-1.5 font-mono">
                  {finding.cite_key}
                </Badge>
                {finding.reason}
              </p>
            ))}
          </CardContent>
        </Card>
      )}

      {evidence.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">核心论断证据（{evidence.length}）</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-xs">
            {evidence.slice(0, 10).map((anchor) => (
              <div key={anchor.id} className="space-y-1 border-b pb-2 last:border-0">
                <p className="font-medium">{anchor.claim_text}</p>
                <p className="text-muted-foreground">
                  {anchor.cite_key ?? '无引用'} · {anchor.source_kind}
                  {anchor.source_page ? ` · 第 ${anchor.source_page} 页` : ''}
                  {anchor.source_section ? ` · ${anchor.source_section}` : ''} ·{' '}
                  {anchor.support_status}
                </p>
                {anchor.evidence_excerpt && (
                  <p className="line-clamp-3 rounded bg-muted/50 p-2 text-muted-foreground">
                    {anchor.evidence_excerpt}
                  </p>
                )}
                <div className="flex gap-1">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void reviewEvidence(anchor, 'confirmed')}
                  >
                    确认证据
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => void reviewEvidence(anchor, 'rejected')}
                  >
                    标记不支持
                  </Button>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
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
