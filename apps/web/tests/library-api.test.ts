import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  confirmPdfUpload,
  rejectPdfUpload,
  retryPdfUpload,
  updateLibraryEntry,
  uploadLibraryPdf,
} from '@/lib/api';

const uploadResponse = {
  upload: {
    id: 'upload-1',
    filename: 'paper.pdf',
    status: 'matching',
    matched_work: null,
  },
  job: null,
};

describe('文献 PDF API 客户端', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(() =>
        Promise.resolve(
          new Response(JSON.stringify(uploadResponse), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        ),
      ),
    );
  });

  it('上传使用 FormData，并让浏览器生成 multipart boundary', async () => {
    const file = new File(['%PDF-1.7'], 'paper.pdf', { type: 'application/pdf' });
    await uploadLibraryPdf('p1', file);

    const [, init] = vi.mocked(fetch).mock.calls[0];
    expect(init?.method).toBe('POST');
    expect(init?.body).toBeInstanceOf(FormData);
    const headers = init?.headers as Headers;
    expect(headers.has('Content-Type')).toBe(false);
    expect((init?.body as FormData).get('file')).toBe(file);
  });

  it('确认、重试、拒绝和单条更新使用约定的 REST 路径与字段', async () => {
    await confirmPdfUpload('p1', 'upload-1', 'core');
    let [url, init] = vi.mocked(fetch).mock.calls.at(-1)!;
    expect(url).toContain('/projects/p1/library/pdf-uploads/upload-1/confirm');
    expect(init?.body).toBe(JSON.stringify({ literature_role: 'core' }));

    await retryPdfUpload('p1', 'upload-1');
    [url, init] = vi.mocked(fetch).mock.calls.at(-1)!;
    expect(url).toContain('/projects/p1/library/pdf-uploads/upload-1/retry');
    expect(init?.method).toBe('POST');

    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 204 }));
    await rejectPdfUpload('p1', 'upload-1');
    [url, init] = vi.mocked(fetch).mock.calls.at(-1)!;
    expect(url).toContain('/projects/p1/library/pdf-uploads/upload-1');
    expect(init?.method).toBe('DELETE');

    await updateLibraryEntry('p1', 'entry-1', {
      literature_role: 'core',
    });
    [url, init] = vi.mocked(fetch).mock.calls.at(-1)!;
    expect(url).toContain('/projects/p1/library/entries/entry-1');
    expect(init?.method).toBe('PATCH');
    expect(init?.body).toBe(
      JSON.stringify({ literature_role: 'core' }),
    );
  });
});
