import { expect, test, type Page } from '@playwright/test';

const project = { id: 'ux-project', title: '大语言模型事实一致性评估方法综述', topic: '比较事实一致性评估方法', paper_type: 'review', writing_mode: 'assisted', language: 'zh', status: 'draft', library_count: 12, section_count: 1, created_at: '2026-09-24T00:00:00Z', updated_at: '2026-09-24T00:00:00Z', intake: { version: 1, status: 'ready', paper_type: 'review', language: 'zh', summary: '比较不同评估方法的适用条件与证据局限。' } };
const section = { section_key: 'intro', title: '引言', order_no: 1, status: 'generated', cite_keys: [], citation_warnings: [], word_count: 120, updated_at: '2026-09-24T00:00:00Z', body_ir: { key: 'intro', title: '引言', level: 1, blocks: [{ type: 'paragraph', runs: [{ t: 'text', v: '事实一致性评估需要明确研究问题与评价条件。' }] }], citation_warnings: [] } };
const artifact = { id: 'file1', format: 'markdown', document_version: 1, export_run_id: 'export1', created_at: '2026-09-24T00:00:00Z', file_name: 'paper.md' };

async function fixture(page: Page, mode: 'ready' | 'empty' | 'failed' | 'paused' = 'ready') {
  let created = false;
  let exported = false;
  let resumed = false;
  await page.context().addCookies([{ name: 'paperforge_session', value: 'controlled-session', url: 'http://127.0.0.1:3310' }]);
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const method = route.request().method();
    let body: unknown = [];
    if (path.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: `data: ${JSON.stringify({ seq: 1, type: 'job.closed', status: 'succeeded', payload: {} })}\n\n` });
    if (path === '/auth/me') body = { id: 'ux-user', email: 'research@example.test', email_verified: true };
    else if (path.endsWith('/assets/capabilities')) body = { max_bytes: 33554432, max_mib: 32, preferred_extensions: ['.csv', '.pdf'] };
    else if (path === '/projects' && method === 'POST') { created = true; body = project; }
    else if (path === '/projects') body = created || mode !== 'empty' ? [project] : [];
    else if (path === '/projects/ux-project') {
      if (mode === 'failed') return route.fulfill({ status: 500, json: { detail: '暂时无法读取项目' } });
      body = project;
    }
    else if (path.endsWith('/intake') && method === 'POST') body = { id: 'intake1', project_id: project.id, kind: 'intake', status: 'succeeded', stage: 'done', progress: 1 };
    else if (path.endsWith('/intake')) body = project.intake;
    else if (path.endsWith('/scope')) body = { scope: { topic: project.topic, research_questions: ['如何评估事实一致性？'] } };
    else if (path.endsWith('/sections')) body = mode === 'empty' && !created ? [] : [section];
    else if (path.endsWith('/whitelist')) body = { cite_keys: [] };
    else if (path.endsWith('/visuals/summary')) body = { pending: 0, ready: 0, failed: 0, approved: 0 };
    else if (path.endsWith('/evidence-matrix')) body = { questions: [], evidence: [], links: [] };
    else if (path.endsWith('/readiness')) body = { state: 'unknown', items: [] };
    else if (path.endsWith('/cost/detail')) body = { totals: {}, stages: [], models: [] };
    else if (path.endsWith('/versions')) body = { documents: [], outlines: [] };
    else if (path.endsWith('/citations/audit')) body = { rows: [], hallucinated_cite_keys: [], used_cite_keys: [] };
    else if (path.endsWith('/quality')) body = { readiness_status: 'preflight_ready', issues: [], core_claim_fulltext_coverage: 1 };
    else if (path.endsWith('/exports') && method === 'POST') { exported = true; body = { id: 'export1', project_id: project.id, kind: 'compile', status: 'succeeded', stage: 'done', progress: 1 }; }
    else if (path.endsWith('/exports')) body = exported ? [artifact] : [];
    else if (path.endsWith('/resume')) { resumed = true; body = { id: 'resume1', project_id: project.id, kind: 'full', status: 'succeeded', stage: 'done', progress: 1 }; }
    else if (path.endsWith('/jobs')) body = mode === 'paused' && !resumed ? [{ id: 'pause1', project_id: project.id, kind: 'full', status: 'paused', stage: 'write', progress: 0.5, checkpoint: {}, created_at: '2026-09-24T00:00:00Z' }] : [];
    else if (path.endsWith('/download')) return route.fulfill({ contentType: 'text/markdown', headers: { 'Content-Disposition': 'attachment; filename="paper.md"' }, body: '# Research draft' });
    await route.fulfill({ json: body });
  });
}

for (const width of [390, 768, 1440]) {
  test(`core journey and screenshots ${width}`, async ({ page }) => {
    test.setTimeout(60000);
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ colorScheme: width === 390 ? 'dark' : 'light', reducedMotion: 'reduce' });
    await fixture(page, 'empty');
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('/');
    await page.getByLabel('描述你的研究主题、问题或论文目标').fill('保留这条研究要求');
    await page.getByRole('button', { name: '比较近五年大语言模型事实一致性评估方法' }).click();
    await expect(page.getByLabel('描述你的研究主题、问题或论文目标')).toHaveValue(/保留这条研究要求\n比较/);
    await page.screenshot({ path: `../../artifacts/ui-ux/home-${width}.png`, fullPage: true });
    await page.getByRole('button', { name: '开始研究', exact: true }).click();
    await expect(page).toHaveURL(/projects\/ux-project$/, { timeout: 15000 });
    await expect(page.getByRole('button', { name: /展开规划/ })).toBeVisible();
    await expect(page.getByRole('heading', { name: '稿件', exact: true })).toBeVisible();
    await page.screenshot({ path: `../../artifacts/ui-ux/overview-${width}.png`, fullPage: true });
    await page.goto('/projects/ux-project/write');
    await expect(page.getByRole('button', { name: '保存本节' })).toBeVisible();
    await expect(page.locator('.tiptap')).toContainText('事实一致性评估');
    await page.screenshot({ path: `../../artifacts/ui-ux/write-${width}.png`, fullPage: true });
    await page.goto('/projects/ux-project/export');
    await expect(page.getByRole('button', { name: '生成投稿文件', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '生成投稿文件', exact: true }).click();
    await expect(page.getByRole('link', { name: '下载', exact: true })).toBeVisible();
    await expect(page.getByText('最近一次运行没有产出 PDF', { exact: true })).toBeVisible();
    // Chromium skips request interception for download-attribute navigation.
    // Assert the production attribute, then let the fixture's Content-Disposition trigger the download.
    await expect(page.getByRole('link', { name: '下载', exact: true })).toHaveAttribute('download', '');
    await page.getByRole('link', { name: '下载', exact: true }).evaluate(node => node.removeAttribute('download'));
    const download = page.waitForEvent('download');
    await page.getByRole('link', { name: '下载', exact: true }).click();
    expect((await download).suggestedFilename()).toBe('paper.md');
    await page.screenshot({ path: `../../artifacts/ui-ux/export-${width}.png`, fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });
}

test('mobile navigation traps focus, restores focus and releases scroll on resize', async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const open = page.getByRole('button', { name: '打开导航' });
  await open.click();
  const dialog = page.getByRole('dialog', { name: '导航菜单' });
  await expect(dialog).toBeVisible();
  const close = dialog.getByRole('button', { name: '关闭导航' });
  await expect(close).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(dialog.getByRole('button', { name: '退出登录' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(close).toBeFocused();
  expect(await page.evaluate(() => document.body.style.overflow)).toBe('hidden');
  await page.keyboard.press('Escape');
  await expect(open).toBeFocused();
  expect(await page.evaluate(() => document.body.style.overflow)).not.toBe('hidden');
  await open.click();
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(dialog).toHaveCount(0);
  expect(await page.evaluate(() => document.body.style.overflow)).not.toBe('hidden');
});

test('project load failure offers retry', async ({ page }) => {
  await fixture(page, 'failed');
  await page.goto('/projects/ux-project');
  await expect(page.getByRole('button', { name: /重试/ }).first()).toBeVisible();
});

test('paused project keeps a resume action', async ({ page }) => {
  await fixture(page, 'paused');
  await page.goto('/projects/ux-project');
  await expect(page.getByRole('heading', { name: '研究已暂停' })).toBeVisible();
  await page.getByRole('button', { name: '继续', exact: true }).click();
  await expect(page.getByRole('heading', { name: '研究已暂停' })).toHaveCount(0);
});

test('empty manuscript guides writing before export', async ({ page }) => {
  await fixture(page, 'empty');
  await page.goto('/projects/ux-project/export');
  await expect(page.getByRole('button', { name: '生成投稿文件', exact: true })).toBeDisabled();
  await expect(page.getByText('还没有正文', { exact: true })).toBeVisible();
});
