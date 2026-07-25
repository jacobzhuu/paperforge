'use client';

import * as React from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import {
  AlertTriangle,
  CheckCircle2,
  FileSpreadsheet,
  FileText,
  Image as ImageIcon,
  Loader2,
  Trash2,
  Upload,
} from 'lucide-react';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { DataSourceBanner } from '@/components/data-source-banner';
import { deleteAsset, getNumLint, listAssets, uploadAsset } from '@/lib/api';
import type { AssetKind, DataSource, NumLintReport, UserAsset } from '@/lib/types';

const KIND_LABEL: Record<AssetKind, string> = {
  dataset: '数据集',
  result_table: '结果表格',
  figure: '图',
  method_note: '方法笔记',
  code: '代码',
  bib: 'BibTeX',
};

export function AssetsCenter() {
  const params = useSearchParams();
  const projectId = params.get('project') ?? '';

  const [assets, setAssets] = React.useState<UserAsset[]>([]);
  const [lint, setLint] = React.useState<NumLintReport | undefined>();
  const [source, setSource] = React.useState<DataSource>('live');
  const [note, setNote] = React.useState<string | undefined>();
  const [loading, setLoading] = React.useState(true);
  const [uploading, setUploading] = React.useState(false);
  const [message, setMessage] = React.useState<string | null>(null);
  const inputRef = React.useRef<HTMLInputElement>(null);

  const reload = React.useCallback(async () => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    const [rows, report] = await Promise.all([listAssets(projectId), getNumLint(projectId)]);
    setAssets(rows.data);
    setLint(report.data);
    setSource(rows.source);
    setNote(rows.note);
    setLoading(false);
  }, [projectId]);

  React.useEffect(() => {
    void reload();
  }, [reload]);

  const onUpload = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    setMessage(null);
    for (const file of Array.from(files)) {
      const result = await uploadAsset(projectId, file);
      if (!result.data) setMessage(result.note ?? '上传失败');
    }
    setUploading(false);
    if (inputRef.current) inputRef.current.value = '';
    void reload();
  };

  const remove = async (asset: UserAsset) => {
    await deleteAsset(projectId, asset.id);
    void reload();
  };

  return (
    <div className="space-y-6">
      <PageHeader
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

      <DataSourceBanner source={source} note={note} />

      {message && (
        <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
          {message}
        </div>
      )}

      <NumLintCard report={lint} />

      {!projectId ? (
        <EmptyHint />
      ) : loading ? (
        <div className="h-40 animate-pulse rounded-xl border bg-muted/40" />
      ) : assets.length === 0 ? (
        <div className="rounded-xl border border-dashed py-16 text-center text-sm text-muted-foreground">
          还没有素材。上传结果表格后，正文里的数字会由系统从表格确定性注入；
          <br />
          没有素材时，实验结果处会渲染 <code>\todo{'{'}待补充实验数据{'}'}</code> 占位而不是编造数值。
        </div>
      ) : (
        <div className="space-y-4">
          {assets.map((asset) => (
            <AssetCard key={asset.id} asset={asset} onDelete={() => remove(asset)} />
          ))}
        </div>
      )}
    </div>
  );
}

function NumLintCard({ report }: { report: NumLintReport | undefined }) {
  if (!report) return null;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">数字一致性（NUMLINT）</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="flex items-center gap-2 text-sm">
          {report.consistent ? (
            <>
              <CheckCircle2 className="h-4 w-4 text-success" />
              <span>
                正文数字与素材 100% 一致（已核 {report.checked_count} 处，
                {report.sourced_count} 处有出处）
              </span>
            </>
          ) : (
            <>
              <AlertTriangle className="h-4 w-4 text-destructive" />
              <span className="text-destructive">
                发现 {report.unsourced_count} 处无法在素材中找到出处的数值
              </span>
            </>
          )}
        </div>
        {report.unsourced.length > 0 && (
          <ul className="space-y-1 border-t pt-2 text-xs">
            {report.unsourced.slice(0, 8).map((finding, index) => (
              <li key={index}>
                <Badge variant="destructive" className="mr-1.5 font-mono">
                  {finding.value}
                </Badge>
                <span className="text-muted-foreground">
                  [{finding.section_key}] …{finding.context}…
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function AssetCard({ asset, onDelete }: { asset: UserAsset; onDelete: () => void }) {
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
        <button onClick={onDelete} aria-label="删除素材" className="text-muted-foreground hover:text-destructive">
          <Trash2 className="h-4 w-4" />
        </button>
      </CardHeader>
      {asset.headers.length > 0 && (
        <CardContent className="px-0 pb-0">
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
        </CardContent>
      )}
      {asset.warnings.length > 0 && (
        <CardContent className="pt-3 text-xs text-warning">
          {asset.warnings.map((warning, index) => (
            <p key={index}>⚠️ {warning}</p>
          ))}
        </CardContent>
      )}
    </Card>
  );
}

function EmptyHint() {
  return (
    <div className="rounded-xl border border-dashed py-16 text-center text-sm text-muted-foreground">
      请先从
      <Link href="/projects" className="mx-1 underline">
        项目列表
      </Link>
      进入某个项目。
    </div>
  );
}
