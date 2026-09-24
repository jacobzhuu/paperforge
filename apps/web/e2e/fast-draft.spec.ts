import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page, context }) => {
  await context.addCookies([{
    name: 'paperforge_session', value: 'controlled-session', url: 'http://127.0.0.1:3310',
  }]);
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const body = path.endsWith('/auth/me')
      ? { id: 'test-user', email: 'visual@example.test', email_verified: true }
      : path.endsWith('/assets/capabilities')
        ? { max_bytes: 33554432, max_mib: 32, preferred_extensions: ['.txt'] }
        : [];
    await route.fulfill({ json: body });
  });
});

test('desktop light: direct toggle, delayed hint, keyboard dismissal', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/');
  const toggle = page.getByRole('button', { name: '快速草稿' });
  await expect(toggle).toHaveAttribute('aria-pressed', 'false');
  await toggle.hover();
  await expect(page.getByRole('tooltip')).toHaveText('更快生成初稿，引用仍需完整核验。');
  await toggle.focus();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('tooltip')).toHaveCount(0);
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-pressed', 'true');
  await page.waitForTimeout(450);
  await page.screenshot({ path: '../../artifacts/fast-draft-desktop-light.png', fullPage: true });
});

test('mobile dark with reduced motion: label and state remain visible', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
  await page.goto('/');
  const toggle = page.getByRole('button', { name: '快速草稿' });
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-pressed', 'true');
  await expect(toggle).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await toggle.locator('.fast-draft-icon').evaluate(node => getComputedStyle(node).animationName)).toBe('none');
  await page.mouse.move(385, 800);
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.screenshot({ path: '../../artifacts/fast-draft-mobile-dark.png', fullPage: true });
});
