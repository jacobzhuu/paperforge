import * as React from 'react';
import { vi } from 'vitest';
import type { PaperSection, Project, VisualAsset } from '@/lib/types';

/** 一个默认成功的 `ApiResult`。 */
export function ok<T>(data: T) {
  return Promise.resolve({ data, source: 'live' as const });
}

/** 让某个接口以 500 失败——用来验证「一个模块挂了不拖垮整页」。 */
export function fail(message = 'API 500: boom') {
  return Promise.reject(new Error(message));
}

export function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p1',
    title: '测试项目',
    paper_type: 'review',
    writing_mode: 'assisted',
    language: 'zh',
    status: 'draft',
    library_count: 0,
    ...overrides,
  } as Project;
}

export function makeSection(overrides: Partial<PaperSection> = {}): PaperSection {
  return {
    section_key: 'introduction',
    title: '引言',
    order_no: 1,
    status: 'generated',
    cite_keys: [],
    body_ir: {
      key: 'introduction',
      level: 1,
      title: '引言',
      blocks: [],
      citation_warnings: [],
    },
    citation_warnings: [],
    word_count: 100,
    updated_at: '2026-07-27T00:00:00Z',
    ...overrides,
  } as unknown as PaperSection;
}

export function makeVisual(overrides: Partial<VisualAsset> & { id: string }): VisualAsset {
  return {
    asset_ref: `va_${overrides.id}`,
    kind: 'diagram',
    generation_status: 'ready',
    review_status: 'pending',
    title: '示意图',
    caption: '一张图',
    alt_text: '一张图的替代文本',
    figure_label: `fig:va_${overrides.id}`,
    spec: { kind: 'diagram' },
    renditions: {},
    input_hash: overrides.id,
    version: 1,
    ...overrides,
  } as VisualAsset;
}

/**
 * 最小的 project-context 替身。
 *
 * 真实的 Provider 会去拉项目、白名单与进度，那些在这里都是噪声——被测的是
 * 各工作台自己的加载与降级行为。
 */
export function mockProjectContext(overrides: Record<string, unknown> = {}) {
  const startJob = vi.fn(() => 'job-1');
  return {
    projectId: 'p1',
    project: makeProject(),
    paperType: 'review',
    whitelist: [],
    loading: false,
    error: null,
    source: 'live',
    reload: vi.fn(),
    progress: {},
    tracked: null,
    jobs: {},
    busy: false,
    jobMessage: null,
    startJob,
    onJobFinished: () => () => {},
    onJobEvent: () => () => {},
    ...overrides,
  };
}

/** Toast 的收集器：断言「用户到底看到了什么提示」。 */
export function makeToastSpy() {
  const calls: { title: string; description?: string; variant?: string }[] = [];
  const toast = vi.fn((entry: { title: string; description?: string; variant?: string }) => {
    calls.push(entry);
  });
  return { toast, calls };
}

/** React 18 的 act 需要这个标志，否则会刷一堆警告。 */
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

export const noop = () => {};
export const Fragment = React.Fragment;
