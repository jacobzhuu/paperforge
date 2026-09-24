import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:3310',
    trace: 'retain-on-failure',
    video: 'on',
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
      args: ['--no-sandbox'],
    },
  },
  webServer: {
    command: 'pnpm dev --hostname 127.0.0.1 --port 3310',
    url: 'http://127.0.0.1:3310/login',
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
  },
});
