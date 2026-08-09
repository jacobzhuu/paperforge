'use client';

import * as React from 'react';
import { FileText, Upload } from 'lucide-react';
import { Dialog } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { useToast } from '@/components/ui/toast';
import type { LibraryPdfUploadResult } from '@/lib/types';
import { describeError } from '@/lib/errors';

export function PdfUploadDialog({
  open,
  onClose,
  onUpload,
  onUploaded,
}: {
  open: boolean;
  onClose: () => void;
  onUpload: (file: File) => Promise<LibraryPdfUploadResult>;
  onUploaded: (result: LibraryPdfUploadResult) => void;
}) {
  const { toast } = useToast();
  const [file, setFile] = React.useState<File | null>(null);
  const [uploading, setUploading] = React.useState(false);
  const inputRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    if (!open) {
      setFile(null);
      setUploading(false);
    }
  }, [open]);

  const pick = (candidate?: File) => {
    if (!candidate) return;
    const looksLikePdf =
      candidate.type === 'application/pdf' || candidate.name.toLowerCase().endsWith('.pdf');
    if (!looksLikePdf) {
      toast({
        title: '请选择 PDF 文件',
        description: '学术论文原文只接受 .pdf；表格、图片和方法笔记请放到素材中心。',
        variant: 'error',
      });
      return;
    }
    setFile(candidate);
  };

  const submit = async () => {
    if (!file || uploading) return;
    setUploading(true);
    try {
      const created = await onUpload(file);
      onUploaded(created);
      toast({
        title:
          created.upload.status === 'needs_confirmation'
            ? '已识别 PDF，请确认匹配'
            : 'PDF 已上传，正在识别与匹配',
        variant: 'success',
      });
      onClose();
    } catch (error) {
      toast({
        title: 'PDF 上传失败',
        description: describeError(error),
        variant: 'error',
      });
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = '';
    }
  };

  return (
    <Dialog
      open={open}
      onClose={() => {
        if (!uploading) onClose();
      }}
      title="上传文献 PDF"
      description="系统会提取 DOI、题名、作者和年份；确认匹配后，原文按项目私有保存并进入全文解析。"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={uploading}>
            取消
          </Button>
          <Button
            onClick={() => void submit()}
            disabled={!file}
            loading={uploading}
            loadingLabel="正在上传…"
          >
            <Upload />
            上传并识别
          </Button>
        </>
      }
    >
      <input
        ref={inputRef}
        type="file"
        accept=".pdf,application/pdf"
        className="hidden"
        onChange={(event) => pick(event.target.files?.[0])}
      />
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          pick(event.dataTransfer.files[0]);
        }}
        className="flex min-h-36 w-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed px-6 py-8 text-center transition-colors hover:bg-accent/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <FileText className="h-8 w-8 text-muted-foreground" />
        {file ? (
          <>
            <span className="max-w-full truncate text-body font-medium">{file.name}</span>
            <span className="text-meta text-muted-foreground">
              {(file.size / 1024 / 1024).toFixed(1)} MiB · 点击可更换
            </span>
          </>
        ) : (
          <>
            <span className="text-body font-medium">选择或拖入一篇 PDF</span>
            <span className="text-meta text-muted-foreground">
              上传文件不会进入跨用户共享的 OA 存储
            </span>
          </>
        )}
      </button>
    </Dialog>
  );
}
