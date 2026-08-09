'use client';

import * as React from 'react';
import { ExternalLink, Loader2, RefreshCw, Save } from 'lucide-react';
import { WorkbenchFooterNav } from '@/components/project/workbench-footer-nav';
import { WorkbenchHeader } from '@/components/project/workbench-header';
import { useJobFinished, useProject } from '@/components/project/project-context';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { useToast } from '@/components/ui/toast';
import { ModuleError } from '@/components/layout/module-error';
import {
  generateEvidenceMatrix,
  generateEvidenceUnits,
  generateSynthesis,
  getEvidenceMatrix,
  getSynthesis,
  updateEvidenceMatrixLink,
} from '@/lib/api';
import { describeError } from '@/lib/errors';
import {
  ANSWER_STATUS_VARIANT,
  anchorStrengthLabel,
  answerStatusLabel,
  evidenceGradeLabel,
  evidenceStanceLabel,
} from '@/lib/labels';
import type {
  EvidenceMatrix,
  EvidenceMatrixDiagnostics,
  EvidenceStance,
  EvidenceUnit,
  ResearchQuestion,
} from '@/lib/types';

function locator(unit: EvidenceUnit): string {
  return [
    unit.page != null ? `p.${unit.page}` : '',
    unit.section_path ?? '',
    unit.object_ref ?? '',
  ]
    .filter(Boolean)
    .join(' · ') || '未定位';
}

function gradeVariant(grade: string): 'success' | 'warning' | 'muted' {
  if (grade.startsWith('A_') || grade.startsWith('B_')) return 'success';
  if (grade.startsWith('C_')) return 'warning';
  return 'muted';
}

function DiagnosticsCard({
  diagnostics,
  subQuestions,
  busy,
  onRebuild,
}: {
  diagnostics: EvidenceMatrixDiagnostics;
  subQuestions: ResearchQuestion[];
  busy: boolean;
  onRebuild: () => void;
}) {
  const questionById = new Map(subQuestions.map((item) => [item.id, item]));
  const perQuestion = diagnostics.per_question ?? [];

  return (
    <Card className="border-warning/50">
      <CardHeader className="pb-3">
        <CardTitle className="text-base">对齐诊断</CardTitle>
        <p className="text-sm text-muted-foreground">
          子问题已生成，但矩阵尚无链接。下方统计可帮助判断是词法门槛、任务约束还是候选为空。
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-2">
          <Badge variant="secondary">{diagnostics.evidence_unit_count} 条证据</Badge>
          <Badge variant="secondary">{diagnostics.link_count} 条链接</Badge>
          {diagnostics.unlinked_evidence_count > 0 && (
            <Badge variant="warning">{diagnostics.unlinked_evidence_count} 条未对齐</Badge>
          )}
          {diagnostics.rejected_by_lexical != null && diagnostics.rejected_by_lexical > 0 && (
            <Badge variant="muted">词法拒绝 {diagnostics.rejected_by_lexical}</Badge>
          )}
          {diagnostics.rejected_by_task != null && diagnostics.rejected_by_task > 0 && (
            <Badge variant="muted">任务约束拒绝 {diagnostics.rejected_by_task}</Badge>
          )}
          {diagnostics.zero_candidate_questions != null &&
            diagnostics.zero_candidate_questions > 0 && (
              <Badge variant="warning">
                零候选子问题 {diagnostics.zero_candidate_questions}
              </Badge>
            )}
        </div>

        {diagnostics.bridge_sources && Object.keys(diagnostics.bridge_sources).length > 0 && (
          <div className="text-sm text-muted-foreground">
            跨语言桥接来源：
            {Object.entries(diagnostics.bridge_sources)
              .map(([key, count]) => `${key} (${count})`)
              .join('、')}
          </div>
        )}

        {perQuestion.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>子问题</TableHead>
                <TableHead>候选</TableHead>
                <TableHead>已链接 / 全文</TableHead>
                <TableHead>最佳 overlap</TableHead>
                <TableHead>路由</TableHead>
                <TableHead>拒绝 / 原因</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {perQuestion.map((row) => {
                const question = questionById.get(row.question_id);
                return (
                  <TableRow key={row.question_id}>
                    <TableCell className="max-w-xs truncate text-sm">
                      {question?.text ?? row.question_id}
                    </TableCell>
                    <TableCell>{row.candidate_count}</TableCell>
                    <TableCell>
                      {row.classified_count ?? 0} / {row.eligible_abc_count ?? 0}
                    </TableCell>
                    <TableCell>
                      {row.best_score != null ? row.best_score.toFixed(3) : '—'}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {row.routing_mode ?? '—'}
                      {row.bridge_source ? ` · ${row.bridge_source}` : ''}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {[
                        row.rejected_by_lexical ? `词法 ${row.rejected_by_lexical}` : null,
                        row.rejected_by_task ? `任务 ${row.rejected_by_task}` : null,
                        row.no_link_reason ?? null,
                      ]
                        .filter(Boolean)
                        .join(' · ') || '—'}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}

        <Button variant="outline" disabled={busy} onClick={onRebuild}>
          <RefreshCw className="h-4 w-4" />
          降级重建对齐
        </Button>
      </CardContent>
    </Card>
  );
}

export function EvidenceMatrixWorkbench() {
  const { projectId, busy, startJob } = useProject();
  const { toast } = useToast();
  const [matrix, setMatrix] = React.useState<EvidenceMatrix>();
  const [answerByQuestionId, setAnswerByQuestionId] = React.useState<
    Record<string, ResearchQuestion['answer_status']>
  >({});
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [activeQuestion, setActiveQuestion] = React.useState('');
  const [saving, setSaving] = React.useState<string | null>(null);

  const loadSynthesis = React.useCallback(() => {
    void getSynthesis(projectId)
      .then((result) => {
        if (!result.data?.questions?.length) return;
        setAnswerByQuestionId(
          Object.fromEntries(result.data.questions.map((item) => [item.id, item.answer_status])),
        );
      })
      .catch(() => {
        /* 综合判定是增强信息，失败不阻断矩阵主视图 */
      });
  }, [projectId]);

  const load = React.useCallback(() => {
    setLoading(true);
    setLoadError(null);
    getEvidenceMatrix(projectId)
      .then((result) => {
        setMatrix(result.data);
        if (result.data?.questions?.length) {
          setAnswerByQuestionId(
            Object.fromEntries(
              result.data.questions.map((item) => [item.id, item.answer_status]),
            ),
          );
        }
        setActiveQuestion((current) => {
          if (current) return current;
          return result.data?.questions.find((item) => item.kind === 'sub')?.id ?? '';
        });
      })
      .catch((error) => {
        setMatrix(undefined);
        setLoadError(describeError(error));
      })
      .finally(() => setLoading(false));
  }, [projectId]);

  React.useEffect(() => {
    load();
    loadSynthesis();
  }, [load, loadSynthesis]);

  useJobFinished(() => {
    load();
    loadSynthesis();
  });

  const patchLink = (linkId: string, changes: Partial<EvidenceMatrix['links'][number]>) => {
    setMatrix((current) =>
      current
        ? {
            ...current,
            links: current.links.map((link) =>
              link.id === linkId ? { ...link, ...changes } : link,
            ),
          }
        : current,
    );
  };

  const persist = async (
    linkId: string,
    stance: EvidenceStance,
    conditionNote?: string | null,
  ) => {
    setSaving(linkId);
    try {
      const updated = await updateEvidenceMatrixLink(
        projectId,
        linkId,
        stance,
        conditionNote,
      );
      patchLink(linkId, updated);
      toast({ title: '证据矩阵已更新', variant: 'success' });
    } catch (error) {
      toast({ title: '更新失败', description: describeError(error), variant: 'error' });
    } finally {
      setSaving(null);
    }
  };

  const rebuildMatrix = async () => {
    const result = await generateEvidenceMatrix(projectId);
    startJob(result.data, '后端不可用：无法重建证据矩阵');
  };

  const evidenceById = new Map(
    (matrix?.evidence ?? []).map((item) => [item.id, item]),
  );
  const questionById = new Map(
    (matrix?.questions ?? []).map((item) => [item.id, item]),
  );
  const subQuestions = (matrix?.questions ?? []).filter((item) => item.kind === 'sub');
  const links = (matrix?.links ?? []).filter(
    (item) => !activeQuestion || item.research_question_id === activeQuestion,
  );
  const allLinks = matrix?.links ?? [];
  const unlinked =
    (matrix?.evidence.length ?? 0) -
    new Set(allLinks.map((item) => item.evidence_unit_id)).size;
  const showDiagnostics = matrix && subQuestions.length > 0 && allLinks.length === 0;
  const activeAnswerStatus =
    activeQuestion ? answerByQuestionId[activeQuestion] : undefined;

  return (
    <div className="space-y-6">
      <WorkbenchHeader
        title="证据矩阵"
        description="在写作前核对每条证据回答哪个问题，以及研究之间是否真的可比较。"
        actions={
          <>
            <Button
              variant="outline"
              disabled={busy}
              onClick={async () => {
                const result = await generateEvidenceUnits(projectId);
                startJob(result.data, '后端不可用：无法重建证据单元');
              }}
            >
              <RefreshCw className="h-4 w-4" />
              重抽证据
            </Button>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => void rebuildMatrix()}
            >
              <RefreshCw className="h-4 w-4" />
              重建对齐
            </Button>
            <Button
              disabled={busy}
              onClick={async () => {
                const result = await generateSynthesis(projectId);
                startJob(result.data, '后端不可用：无法重算综合判定');
              }}
            >
              重算综合
            </Button>
          </>
        }
      />

      {loading ? (
        <Skeleton className="h-64" />
      ) : loadError ? (
        <ModuleError label="证据矩阵" error={loadError} onRetry={load} />
      ) : !matrix || subQuestions.length === 0 ? (
        <Card>
          <CardContent className="pt-5 text-sm text-muted-foreground">
            尚未生成问题—证据矩阵。运行完整管线后，QEMATRIX 会把全文证据与子问题对齐。
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="flex flex-wrap items-end justify-between gap-3">
            <label className="grid min-w-72 gap-1 text-sm">
              <span className="text-muted-foreground">查看子问题</span>
              <Select
                value={activeQuestion}
                onChange={(event) => setActiveQuestion(event.target.value)}
              >
                {subQuestions.map((question) => {
                  const status = answerByQuestionId[question.id] ?? question.answer_status;
                  return (
                    <option key={question.id} value={question.id}>
                      [{answerStatusLabel(status)}] {question.text}
                    </option>
                  );
                })}
              </Select>
            </label>
            <div className="flex flex-wrap items-center gap-2">
              {activeAnswerStatus && (
                <Badge variant={ANSWER_STATUS_VARIANT[activeAnswerStatus] ?? 'muted'}>
                  综合：{answerStatusLabel(activeAnswerStatus)}
                </Badge>
              )}
              <Badge variant="secondary">{links.length} 个矩阵单元</Badge>
              {unlinked > 0 && <Badge variant="warning">{unlinked} 条证据未对齐</Badge>}
            </div>
          </div>

          {showDiagnostics && matrix.diagnostics && (
            <DiagnosticsCard
              diagnostics={matrix.diagnostics}
              subQuestions={subQuestions}
              busy={busy}
              onRebuild={() => void rebuildMatrix()}
            />
          )}

          <Card>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="min-w-80">证据</TableHead>
                    <TableHead>等级 / 定位</TableHead>
                    <TableHead>数值与可比键</TableHead>
                    <TableHead className="min-w-40">立场</TableHead>
                    <TableHead className="min-w-56">条件说明</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {links.map((link) => {
                    const unit = evidenceById.get(link.evidence_unit_id);
                    const question = questionById.get(link.research_question_id);
                    if (!unit) return null;
                    return (
                      <TableRow key={link.id}>
                        <TableCell>
                          <div className="space-y-1">
                            <div className="font-medium">
                              [{unit.cite_key ?? 'no-key'}] {unit.title ?? '未命名研究'}
                            </div>
                            <p className="line-clamp-4 text-xs text-muted-foreground">
                              {unit.text}
                            </p>
                            <span className="sr-only">问题：{question?.text}</span>
                          </div>
                        </TableCell>
                        <TableCell className="align-top">
                          <Badge variant={gradeVariant(unit.grade)}>
                            {evidenceGradeLabel(unit.grade)}
                          </Badge>
                          {unit.anchor_strength && (
                            <div className="mt-1 text-micro text-muted-foreground">
                              {anchorStrengthLabel(unit.anchor_strength)}
                            </div>
                          )}
                          <div className="mt-2 flex items-center gap-1 text-xs text-muted-foreground">
                            <ExternalLink className="h-3 w-3" />
                            {locator(unit)}
                          </div>
                        </TableCell>
                        <TableCell className="align-top text-xs">
                          {unit.measurements.length === 0 ? (
                            <span className="text-muted-foreground">无结构化数值</span>
                          ) : (
                            <ul className="space-y-1">
                              {unit.measurements.map((item, index) => (
                                <li key={`${item.comparability_key}-${index}`}>
                                  {item.metric_name}={item.value}
                                  {item.unit ?? ''}
                                  {(item.dataset || item.victim_model) && (
                                    <div className="text-muted-foreground">
                                      {[item.dataset, item.victim_model]
                                        .filter(Boolean)
                                        .join(' · ')}
                                    </div>
                                  )}
                                  <div
                                    className="max-w-40 truncate font-mono text-micro text-muted-foreground"
                                    title={item.comparability_key}
                                  >
                                    {item.comparability_key}
                                  </div>
                                </li>
                              ))}
                            </ul>
                          )}
                        </TableCell>
                        <TableCell className="align-top">
                          <Select
                            value={link.stance}
                            onChange={(event) => {
                              const stance = event.target.value as EvidenceStance;
                              patchLink(link.id, { stance });
                              void persist(link.id, stance, link.condition_note);
                            }}
                          >
                            {Object.entries({
                              supports: 'supports',
                              contradicts: 'contradicts',
                              conditional: 'conditional',
                              not_comparable: 'not_comparable',
                              gap: 'gap',
                            }).map(([value]) => (
                              <option key={value} value={value}>
                                {evidenceStanceLabel(value)}
                              </option>
                            ))}
                          </Select>
                          {link.manually_overridden && (
                            <div className="mt-1 text-micro text-muted-foreground">人工覆盖</div>
                          )}
                        </TableCell>
                        <TableCell className="align-top">
                          <Input
                            value={link.condition_note ?? ''}
                            onChange={(event) =>
                              patchLink(link.id, { condition_note: event.target.value })
                            }
                            placeholder="适用条件或不可比原因"
                          />
                        </TableCell>
                        <TableCell className="align-top">
                          <Button
                            size="icon"
                            variant="ghost"
                            disabled={saving === link.id}
                            onClick={() =>
                              persist(link.id, link.stance, link.condition_note)
                            }
                            aria-label="保存矩阵单元"
                          >
                            {saving === link.id ? (
                              <Loader2 className="h-4 w-4 animate-spin" />
                            ) : (
                              <Save className="h-4 w-4" />
                            )}
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
              {links.length === 0 && !showDiagnostics && (
                <p className="p-5 text-sm text-muted-foreground">
                  该问题尚无候选证据，综合阶段会将其标记为证据不足。
                </p>
              )}
            </CardContent>
          </Card>

          {subQuestions.length > 0 && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">子问题综合判定</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-wrap gap-2">
                {subQuestions.map((question) => {
                  const status = answerByQuestionId[question.id] ?? question.answer_status;
                  return (
                    <Badge
                      key={question.id}
                      variant={ANSWER_STATUS_VARIANT[status] ?? 'muted'}
                      className="max-w-full truncate"
                    >
                      {answerStatusLabel(status)} · {question.text}
                    </Badge>
                  );
                })}
              </CardContent>
            </Card>
          )}
        </>
      )}

      <WorkbenchFooterNav current="evidence" />
    </div>
  );
}
