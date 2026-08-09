'use client';

import * as React from 'react';
import { Loader2, Lock, LockOpen, RefreshCw, Save } from 'lucide-react';
import { WorkbenchFooterNav } from '@/components/project/workbench-footer-nav';
import { WorkbenchHeader } from '@/components/project/workbench-header';
import { useJobFinished, useProject } from '@/components/project/project-context';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import {
  generateResearchQuestions,
  getResearchQuestions,
  updateResearchQuestion,
} from '@/lib/api';
import { describeError } from '@/lib/errors';
import type { ResearchQuestion } from '@/lib/types';

function commaList(value: string): string[] {
  return value
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function QuestionsWorkbench() {
  const { projectId, busy, startJob } = useProject();
  const { toast } = useToast();
  const [questions, setQuestions] = React.useState<ResearchQuestion[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [saving, setSaving] = React.useState<string | null>(null);

  const load = React.useCallback(() => {
    getResearchQuestions(projectId)
      .then((result) => setQuestions(result.data))
      .finally(() => setLoading(false));
  }, [projectId]);

  React.useEffect(load, [load]);
  useJobFinished(load);

  const patchLocal = (id: string, changes: Partial<ResearchQuestion>) => {
    setQuestions((rows) =>
      rows.map((row) => (row.id === id ? { ...row, ...changes } : row)),
    );
  };

  const save = async (question: ResearchQuestion) => {
    setSaving(question.id);
    try {
      const updated = await updateResearchQuestion(projectId, question.id, {
        text: question.text,
        comparison_dimensions: question.comparison_dimensions,
        expected_evidence_kinds: question.expected_evidence_kinds,
        answer_status: question.answer_status,
        search_query: question.search_query ?? '',
      });
      patchLocal(question.id, updated);
      toast({ title: '研究问题已保存并锁定', variant: 'success' });
    } catch (error) {
      toast({ title: '保存失败', description: describeError(error), variant: 'error' });
    } finally {
      setSaving(null);
    }
  };

  const toggleLock = async (question: ResearchQuestion) => {
    setSaving(question.id);
    try {
      const updated = await updateResearchQuestion(projectId, question.id, {
        locked: !question.locked,
      });
      patchLocal(question.id, updated);
      toast({
        title: updated.locked ? '已锁定：重生成不会覆盖这一问题' : '已解锁：交回自动重生成管理',
        variant: 'success',
      });
    } catch (error) {
      toast({ title: '操作失败', description: describeError(error), variant: 'error' });
    } finally {
      setSaving(null);
    }
  };

  return (
    <div className="space-y-6">
      <WorkbenchHeader
        title="研究问题"
        description="章节由子问题驱动；比较维度决定哪些研究可以被放在同一结论中。保存后问题会被锁定，重建与自动适配都不再改写它。"
        actions={
          <Button
            variant="outline"
            disabled={busy}
            onClick={async () => {
              const result = await generateResearchQuestions(projectId);
              startJob(result.data, '后端不可用：无法重建问题树');
            }}
          >
            <RefreshCw className="h-4 w-4" />
            从研究范围重建
          </Button>
        }
      />

      {loading ? (
        <div className="space-y-3">
          <Skeleton className="h-32" />
          <Skeleton className="h-48" />
        </div>
      ) : questions.length === 0 ? (
        <Card>
          <CardContent className="pt-5 text-sm text-muted-foreground">
            尚未生成问题树。运行一次完整管线后，系统会在检索后自动分解核心问题。
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          {questions.map((question) => (
            <Card key={question.id}>
              <CardHeader className="flex-row items-center justify-between space-y-0">
                <CardTitle className="flex items-center gap-2 text-sm">
                  {question.kind === 'core' ? '核心问题' : `子问题 ${question.order_index}`}
                  <Badge variant={question.kind === 'core' ? 'default' : 'secondary'}>
                    {question.answer_status}
                  </Badge>
                  {question.locked && (
                    <Badge variant="outline" className="gap-1">
                      <Lock className="h-3 w-3" />
                      已锁定
                    </Badge>
                  )}
                </CardTitle>
                <div className="flex items-center gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => toggleLock(question)}
                    disabled={saving === question.id}
                    title={
                      question.locked
                        ? '解锁后，「从研究范围重建」会重新生成这一问题'
                        : '锁定后，重建与自动适配都不会改写这一问题'
                    }
                  >
                    {question.locked ? (
                      <LockOpen className="h-4 w-4" />
                    ) : (
                      <Lock className="h-4 w-4" />
                    )}
                    {question.locked ? '解锁' : '锁定'}
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => save(question)}
                    disabled={saving === question.id}
                  >
                    {saving === question.id ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Save className="h-4 w-4" />
                    )}
                    保存
                  </Button>
                </div>
              </CardHeader>
              <CardContent className="grid gap-4">
                <label className="grid gap-1 text-sm">
                  <span className="text-muted-foreground">问题文本</span>
                  <Input
                    value={question.text}
                    onChange={(event) =>
                      patchLocal(question.id, { text: event.target.value })
                    }
                  />
                </label>
                {question.kind === 'sub' && (
                  <div className="grid gap-4 md:grid-cols-2">
                    <label className="grid gap-1 text-sm">
                      <span className="text-muted-foreground">比较维度（逗号分隔）</span>
                      <Input
                        value={question.comparison_dimensions.join(', ')}
                        onChange={(event) =>
                          patchLocal(question.id, {
                            comparison_dimensions: commaList(event.target.value),
                          })
                        }
                        placeholder="任务, 数据集, 指标, 数据划分"
                      />
                    </label>
                    <label className="grid gap-1 text-sm">
                      <span className="text-muted-foreground">预期证据类型（逗号分隔）</span>
                      <Input
                        value={question.expected_evidence_kinds.join(', ')}
                        onChange={(event) =>
                          patchLocal(question.id, {
                            expected_evidence_kinds: commaList(event.target.value),
                          })
                        }
                        placeholder="experimental_fact, author_conclusion"
                      />
                    </label>
                    <label className="grid gap-1 text-sm">
                      <span className="text-muted-foreground">检索式（英文）</span>
                      <Input
                        value={question.search_query ?? ''}
                        onChange={(event) =>
                          patchLocal(question.id, { search_query: event.target.value })
                        }
                        placeholder="poisoning attack sequential recommendation"
                      />
                    </label>
                    <label className="grid gap-1 text-sm">
                      <span className="text-muted-foreground">回答状态</span>
                      <Select
                        value={question.answer_status}
                        onChange={(event) =>
                          patchLocal(question.id, {
                            answer_status: event.target
                              .value as ResearchQuestion['answer_status'],
                          })
                        }
                      >
                        <option value="answered">已回答</option>
                        <option value="partial">部分回答</option>
                        <option value="contested">存在争议</option>
                        <option value="insufficient_evidence">证据不足</option>
                      </Select>
                    </label>
                  </div>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <WorkbenchFooterNav current="questions" />
    </div>
  );
}
