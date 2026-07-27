import path from 'node:path';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * 前端测试基建。
 *
 * 这份配置本身就是一次补课：`apps/web` 此前没有任何自动化测试，而本轮改造里
 * 最关键的几条不变量——「任一辅助接口失败时其余区域仍可用」「AI 生图必须经过
 * 显式确认」「批准插图不会覆盖未保存的草稿」——恰恰都是单测最擅长盯的那类。
 * 靠手工回归守这些不变量，只会在下一次重构时再丢一遍。
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, '.') },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.ts', 'tests/**/*.test.tsx'],
    // Next 的 app 目录与 .next 产物不参与测试收集。
    exclude: ['node_modules', '.next'],
  },
});
