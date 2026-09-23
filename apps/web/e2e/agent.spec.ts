import { expect, test } from '@playwright/test';

const project = { id: 'p1', title: '证据驱动写作示例', paper_type: 'review', language: 'zh',
  status: 'draft', created_at: '2026-09-18T00:00:00Z', counters: {} };
const trace = {
  trace_id: 'trace', engine: 'langgraph', currency: 'CNY',
  jobs: [{ id: 'j1', status: 'paused', created_at: '2026-09-18T00:00:00Z' }],
  summary: { calls: 2, known_cost: 0.12, unpriced_calls: 1, input_tokens: 120,
    output_tokens: 40, unknown_usage_calls: 1, errors: {} },
  budget: {}, interruption: { id: 'i1', reason: 'budget_exhausted', sections: ['s1'] },
  events_truncated: false, calls_truncated: false,
  events: [{ type: 'agent.span_finished', name: 'write', at: '2026-09-18T00:00:00Z',
    job_id: 'j1', status: 'completed', elapsed_ms: 1200 },
    { type: 'polish.completed', at: '2026-09-18T00:00:01Z', job_id: 'j1',
      policy: 'selective_parallel', total: 9, rewritten: 3, skipped: 6, rejected: 1 }],
  calls: [{ role: 'writer', model: 'controlled-writer', latency_ms: 1200,
    cost_estimate: null, error_code: null, input_tokens: null, output_tokens: null }],
};

test.beforeEach(async ({ page, context }) => {
  await context.addCookies([{ name: 'paperforge_session', value: 'controlled-session', url: 'http://127.0.0.1:3310' }]);
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace('/api/v1', '');
    let body: unknown = [];
    if (path === '/auth/me' || path === '/auth/login') {
      const user = { id: 'user', email: 'demo@example.test', email_verified: true };
      body = path.endsWith('login') ? { user } : user;
    } else if (path === '/projects') body = [project];
    else if (path === '/projects/p1') body = project;
    else if (path.endsWith('/trace')) body = trace;
    else if (path.endsWith('/resume')) {
      expect(route.request().postDataJSON()).toEqual({ interrupt_id: 'i1', choice: 'finish' });
      body = { id: 'j2', status: 'queued', kind: 'write', project_id: 'p1', checkpoint: {} };
    } else if (path.endsWith('/evidence-matrix')) body = { questions: [], evidence: [], links: [] };
    else if (path.endsWith('/scope')) body = { scope: null };
    else if (path.endsWith('/questions')) body = [];
    else if (path.endsWith('/readiness')) body = { state: 'unknown', items: [] };
    else if (path.endsWith('/whitelist')) body = { cite_keys: [] };
    else if (path.endsWith('/visuals/summary')) body = { pending: 0, ready: 0, failed: 0, approved: 0 };
    else if (path.endsWith('/evidence/search')) body = {
      version: 'test', mode: 'hybrid_rerank', vector_backend: 'pgvector_exact', reranked: false,
      corpus_count: 1, indexed_count: 1, corpus_truncated: false, warnings: [], elapsed_ms: 12,
      results: [{ evidence_id: 'e1', work_id: 'w1', text: '模型效果依赖统一的任务条件与评价协议。',
        content_hash: 'hash', page: 3, section_path: 'Results', source_document_file_id: null,
        char_start: 0, char_end: 20, grade: 'B_located_prose', score: 0.9,
        verification_status: 'not_assessed' }],
    };
    await route.fulfill({ json: body });
  });
});

test('task inspection preserves unknown accounting and accepts a typed repair decision', async ({ page }) => {
  await page.goto('/projects/p1/jobs/j1');
  await expect(page.getByRole('heading', { name: '任务执行详情' })).toBeVisible();
  await expect(page.getByText('费用和用量不完整', { exact: false })).toBeVisible();
  await expect(page.getByText('共 9 节 · 润色 3 节 · 跳过 6 节 · 拒绝修改 1 节')).toBeVisible();
  await expect(page.getByText('按需并行润色', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '结束本轮修复' }).click();
  await expect(page).toHaveURL(/\/jobs\/j2$/);
});

test('evidence search keeps source locations and distinguishes relevance from verification', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/projects/p1/evidence');
  await page.getByLabel('研究问题或论断').fill('模型效果依赖什么条件');
  await page.getByRole('button', { name: '查找证据', exact: true }).click();
  await expect(page.getByText('第 3 页', { exact: false })).toBeVisible();
  await expect(page.getByText('尚未核验论断支持关系', { exact: false })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

const outline = {
  outline_id: 'o2', version: 2, status: 'draft', content_hash: 'a'.repeat(64),
  tree: { sections: [{ key: 's1', title: '实验设计', level: 1, kind: 'body', cite_keys: [],
    depends_on: [], independent: true, dependency_reason: '独立研究问题' }],
  dependency_contract: { version: 'dependencies-v1', requires_confirmation: true } },
};

test('rebuilt dependencies require confirmation and pin the selected polish policy', async ({ page }) => {
  await page.route('**/api/v1/projects/p1/outline', async route => {
    if (route.request().method() === 'PUT') {
      expect(route.request().postDataJSON().status).toBe('confirmed');
      await route.fulfill({ json: { ...outline, status: 'confirmed' } });
    } else await route.fulfill({ json: outline });
  });
  let selected = '';
  await page.route('**/api/v1/projects/p1/sections/generate', async route => {
    selected = route.request().postDataJSON().polish_policy;
    await route.fulfill({ json: { id: 'j3', status: 'queued', kind: 'write', project_id: 'p1', checkpoint: {} } });
  });
  await page.goto('/projects/p1/outline');
  await expect(page.getByRole('region', { name: '章节执行关系' })).toBeVisible();
  await expect(page.getByRole('button', { name: '开始写作' })).toBeDisabled();
  await page.getByRole('button', { name: '确认章节关系' }).click();
  await expect(page.getByRole('button', { name: '开始写作' })).toBeEnabled();
  await page.getByLabel('润色策略').selectOption('selective_parallel');
  await page.getByRole('button', { name: '开始写作' }).click();
  await expect.poll(() => selected).toBe('selective_parallel');
});

test('dependency rebuild submits the loaded version and source hash', async ({ page }) => {
  await page.route('**/api/v1/projects/p1/outline', route => route.fulfill({ json: outline }));
  let submitted: unknown;
  await page.route('**/api/v1/projects/p1/outline/dependencies/rebuild', async route => {
    submitted = route.request().postDataJSON();
    await route.fulfill({ json: { id: 'j4', status: 'queued', kind: 'outline', project_id: 'p1', checkpoint: {} } });
  });
  await page.goto('/projects/p1/outline');
  await page.getByRole('button', { name: '优化章节依赖' }).click();
  await expect.poll(() => submitted).toEqual({ outline_id: 'o2', content_hash: 'a'.repeat(64) });
});
