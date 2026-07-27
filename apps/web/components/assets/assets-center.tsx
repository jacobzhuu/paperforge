'use client';

import * as React from 'react';
import Link from 'next/link';
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  FileSpreadsheet,
  FileText,
  Image as ImageIcon,
  Loader2,
  Trash2,
  Upload,
} from 'lucide-react';
import { Button, buttonVariants } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog } from '@/components/ui/dialog';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { useToast } from '@/components/ui/toast';
import { LoadState } from '@/components/layout/load-state';
import { WorkbenchHeader } from '@/components/project/workbench-header';
import { WorkbenchFooterNav } from '@/components/project/workbench-footer-nav';
import { useJobFinished, useProject } from '@/components/project/project-context';
import { VisualsGallery } from '@/components/assets/visuals-gallery';
import { assetDownloadUrl, deleteAsset, getNumLint, getRuntimeSettings, listAssets, listVisuals, uploadAsset } from '@/lib/api';
import type { AssetKind, NumLintReport, UserAsset, VisualAsset } from '@/lib/types';
import { describeError } from '@/lib/errors';
import { projectHref } from '@/lib/pipeline';
import { cn } from '@/lib/utils';

const KIND_LABEL: Record<AssetKind, string> = {
  dataset: '数据集',
  result_table: '结果表格',
  figure: '图',
  method_note: '方法笔记',
  code: '代码',
  bib: 'BibTeX',
};

export function AssetsCenter() {
  const { projectId, reload: reloadProject } = useProject();
  const { toast } = useToast();

  const [assets, setAssets] = React.useState<UserAsset[]>([]);
  const [lint, setLint] = React.useState<NumLintReport | undefined>();
  const [visuals, setVisuals] = React.useState<VisualAsset[]>([]);
  const [aiGenerationAvailable, setAiGenerationAvailable] = React.useState(false);
  const [tab, setTab] = React.useState('raw');
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [uploading, setUploading] = React.useState(false);
  const [dragOver, setDragOver] = React.useState(false);
  const [pendingDelete, setPendingDelete] = React.useState<UserAsset | null>(null);
  const inputRef = React.useRef<HTMLInputElement>(null);

  const reload = React.useCallback(async () => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    const [rows, report, visualRows, runtime] = await Promise.all([
      listAssets(projectId),
      getNumLint(projectId),
      listVisuals(projectId),
      getRuntimeSettings(),
    ]);
    setAssets(rows.data);
    setLint(report.data);
    setVisuals(visualRows.data);
    setAiGenerationAvailable(Boolean(
      runtime.data?.ai_images_enabled && runtime.data?.image_provider_configured,
    ));
    setLoadError(null);
    setLoading(false);
  }, [projectId]);

  const runReload = React.useCallback(() => {
    setLoadError(null);
    reload().catch((err) => {
      setLoadError(describeError(err));
      setLoading(false);
    });
  }, [reload]);

  React.useEffect(() => {
    runReload();
  }, [runReload]);
  useJobFinished(runReload);

  const onUpload = async (files: FileList | File[] | null) => {
    const list = files ? Array.from(files) : [];
    if (list.length === 0) return;
    setUploading(true);
    let succeeded = 0;
    const failures: string[] = [];
    try {
      // 逐文件独立成败：此前一个 413 就会把整个循环抛出去，
      // setUploading(false) 永远执行不到，上传按钮从此转圈到死。
      for (const file of list) {
        try {
          const result = await uploadAsset(projectId, file);
          if (result.data) succeeded += 1;
          else failures.push(`${file.name}：${result.note ?? '未写入'}`);
        } catch (err) {
          failures.push(`${file.name}：${describeError(err)}`);
        }
      }
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = '';
    }
    if (succeeded > 0) {
      toast({ title: `已上传 ${succeeded} 个素材`, variant: 'success' });
      reloadProject();
    }
    if (failures.length > 0) {
      // 每条失败都要能看到：此前 setMessage 互相覆盖，只剩最后一条。
      toast({
        title: `${failures.length} 个素材上传失败`,
        description: failures.join('；'),
        variant: 'error',
      });
    }
    runReload();
  };

  const confirmRemove = async () => {
    const asset = pendingDelete;
    setPendingDelete(null);
    if (!asset) return;
    try {
      await deleteAsset(projectId, asset.id);
      toast({ title: '素材已删除', variant: 'success' });
      reloadProject();
    } catch (err) {
      toast({ title: '素材未能删除', description: describeError(err), variant: 'error' });
    }
    runReload();
  };

  return (
    <div className="space-y-6">
      <WorkbenchHeader
        title="素材中心"
        description="结果表格 / 图 / 方法笔记 / 代码 / BibTeX —— 确定性解析后作为正文数字的唯一出处"
        actions={
          <>
            <input
              ref={inputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => onUpload(e.target.files)}
            />
            <Button onClick={() => inputRef.current?.click()} disabled={!projectId || uploading}>
              {uploading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Upload className="h-4 w-4" />
              )}
              上传素材
            </Button>
          </>
        }
      />

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="raw">原始素材</TabsTrigger>
          <TabsTrigger value="visuals">图表与插图 {visuals.length > 0 ? `(${visuals.length})` : ''}</TabsTrigger>
        </TabsList>

        <TabsContent value="raw">
          {/* NUMLINT 摘要留在这里，明细已移到写作台的校验面板。 */}
          <NumLintSummary report={lint} projectId={projectId} />

          <LoadState loading={loading} error={loadError} onRetry={runReload} skeletonClassName="h-40">
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            void onUpload(e.dataTransfer.files);
          }}
          className={cn(
            'space-y-4 rounded-xl transition-colors',
            dragOver && 'outline-dashed outline-2 outline-offset-4 outline-primary/60',
          )}
        >
          {assets.length === 0 ? (
            <PureGenerationNotice />
          ) : (
            assets.map((asset) => (
              <AssetCard
                key={asset.id}
                asset={asset}
                projectId={projectId}
                onDelete={() => setPendingDelete(asset)}
              />
            ))
          )}
        </div>
          </LoadState>
        </TabsContent>

        <TabsContent value="visuals">
          <LoadState loading={loading} error={loadError} onRetry={runReload} skeletonClassName="h-40">
            <VisualsGallery
              projectId={projectId}
              assets={assets}
              visuals={visuals}
              aiGenerationAvailable={aiGenerationAvailable}
              onChanged={runReload}
            />
          </LoadState>
        </TabsContent>
      </Tabs>

      <Dialog
        open={pendingDelete !== null}
        onClose={() => setPendingDelete(null)}
        title="删除素材？"
        description={
          pendingDelete
            ? `「${pendingDelete.title ?? '未命名'}」删除后，正文中引用其数值的位置将失去出处，数字一致性检查会把它们标为无出处。此操作无法撤销。`
            : undefined
        }
        footer={
          <>
            <Button variant="outline" onClick={() => setPendingDelete(null)}>
              取消
            </Button>
            <Button variant="destructive" onClick={confirmRemove}>
              删除
            </Button>
          </>
        }
      />

      <WorkbenchFooterNav current="assets" />
    </div>
  );
}

/** 无素材不是错误状态——这是产品明确支持的「纯生成模式」。 */
function PureGenerationNotice() {
  return (
    <div className="rounded-xl border border-dashed p-8 text-center">
      <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted">
        <Upload className="h-6 w-6 text-muted-foreground" />
      </div>
      <p className="mt-3 font-medium">还没有素材</p>
      <p className="mx-auto mt-1 max-w-lg text-sm text-muted-foreground">
        把文件拖到这里，或点右上角「上传素材」。上传结果表格后，正文里的数字会由系统从表格确定性注入。
      </p>
      <div className="mx-auto mt-4 max-w-lg rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-left text-xs text-warning-foreground">
        <p className="font-medium">纯生成模式</p>
        <p className="mt-0.5 leading-relaxed">
          没有素材也可以继续写作。实验结果处会渲染{' '}
          <code className="font-mono">\todo{'{'}待补充实验数据{'}'}</code>{' '}
          占位并明确标注「结果待实验补充」——系统在任何路径下都不会编造实验数值。
        </p>
      </div>
    </div>
  );
}

function NumLintSummary({
  report,
  projectId,
}: {
  report: NumLintReport | undefined;
  projectId: string;
}) {
  if (!report) return null;
  if (report.consistent) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-success/40 bg-success/10 px-3 py-2 text-sm">
        <CheckCircle2 className="h-4 w-4 shrink-0 text-success-strong" />
        <span>
          正文数字与素材一致（已核 {report.checked_count} 处，{report.sourced_count} 处有出处）
        </span>
      </div>
    );
  }
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm">
      <span className="flex items-center gap-2 text-destructive-strong">
        <AlertTriangle className="h-4 w-4 shrink-0" />
        正文里有 {report.unsourced_count} 处数值在素材中找不到出处
      </span>
      <Link
        href={projectHref(projectId, 'write')}
        className={buttonVariants({ variant: 'outline', size: 'sm' })}
      >
        在写作台逐条定位 →
      </Link>
    </div>
  );
}

function AssetCard({
  asset,
  projectId,
  onDelete,
}: {
  asset: UserAsset;
  projectId: string;
  onDelete: () => void;
}) {
  const Icon =
    asset.kind === 'figure'
      ? ImageIcon
      : asset.kind === 'result_table' || asset.kind === 'dataset'
        ? FileSpreadsheet
        : FileText;

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between space-y-0 pb-3">
        <div className="flex items-start gap-2">
          <Icon className="mt-0.5 h-4 w-4 text-muted-foreground" />
          <div>
            <CardTitle className="text-sm">{asset.title ?? '（未命名）'}</CardTitle>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {KIND_LABEL[asset.kind] ?? asset.kind}
              {asset.row_count != null && ` · ${asset.row_count} 行 × ${asset.column_count} 列`}
              {asset.number_count > 0 && ` · ${asset.number_count} 个可引用数值`}
              {asset.asset_ref && ` · ref ${asset.asset_ref}`}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {/* 后端 GET /assets/{id}/download 一直实现着，但此前界面没有任何入口。 */}
          <a
            href={assetDownloadUrl(projectId, asset.id)}
            download
            aria-label="下载原文件"
            className="rounded p-1 text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Download className="h-4 w-4" />
          </a>
          <button
            type="button"
            onClick={onDelete}
            aria-label="删除素材"
            className="rounded p-1 text-muted-foreground transition-colors hover:text-destructive-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </CardHeader>
      {asset.headers.length > 0 && (
        <CardContent className="px-0 pb-0">
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  {asset.headers.map((header) => (
                    <TableHead key={header}>{header}</TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {asset.preview_rows.map((row, index) => (
                  <TableRow key={index}>
                    {row.map((cell, cellIndex) => (
                      <TableCell key={cellIndex} className="tabular-nums">
                        {cell}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      )}
      {asset.warnings.length > 0 && (
        <CardContent className="pt-3 text-xs text-warning-strong">
          {asset.warnings.map((warning, index) => (
            <p key={index}>⚠️ {warning}</p>
          ))}
        </CardContent>
      )}
    </Card>
  );
}
