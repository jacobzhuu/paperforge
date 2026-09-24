import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page, context }) => {
  await context.addCookies([{ name: 'paperforge_session', value: 'controlled-session', url: 'http://127.0.0.1:3310' }]);
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    await route.fulfill({ json: path.endsWith('/auth/me')
      ? { id: 'test-user', email: 'intake@example.test', email_verified: true }
      : path.endsWith('/assets/capabilities') ? { max_bytes: 33554432, max_mib: 32, preferred_extensions: ['.csv', '.pdf'] }
      : [] });
  });
});

test('首页默认零下拉，面板键盘可用并归还焦点', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('combobox')).toHaveCount(0);
  await expect(page.getByText('协作 · 中文（默认）')).toBeVisible();
  await expect(page.getByRole('button', { name: /未选择项目/ })).toHaveCount(0);
  await expect(page.getByText('支持文献、数据与代码，单文件最大 32 MiB。')).toBeVisible();
  const adjust = page.getByRole('button', { name: '调整', exact: true });
  await adjust.click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByLabel('语言', { exact: true }).selectOption('en');
  await page.getByRole('radio', { name: /全自动/ }).check();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(adjust).toBeFocused();
  await expect(page.getByText('全自动 · English')).toBeVisible();
  await page.screenshot({ path: '../../artifacts/intake-home-desktop.png', fullPage: true });
});

test('移动端设置不撑开首页，不出现水平溢出', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('button', { name: '调整', exact: true }).click();
  await page.getByText('投稿要求（可稍后修改）').click();
  await expect(page.getByRole('dialog')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: '../../artifacts/intake-settings-mobile.png', fullPage: true });
});
