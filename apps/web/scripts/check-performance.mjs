import fs from 'node:fs';
import path from 'node:path';
import { gzipSync } from 'node:zlib';

const root = process.cwd();
const manifestPath = path.join(root, '.next', 'app-build-manifest.json');
const budgetPath = path.join(root, 'performance-budgets.json');
const reportOnly = process.argv.includes('--report');

if (!fs.existsSync(manifestPath)) {
  console.error('缺少 .next/app-build-manifest.json；请先运行 pnpm build。');
  process.exit(1);
}

const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const budgets = JSON.parse(fs.readFileSync(budgetPath, 'utf8'));
let failed = false;

for (const [label, budget] of Object.entries(budgets.routes)) {
  const files = manifest.pages[budget.manifest_key];
  if (!files) {
    console.error(`${label}: 构建清单中没有 ${budget.manifest_key}`);
    failed = true;
    continue;
  }
  const javascript = [...new Set(files.filter((file) => file.endsWith('.js')))];
  const chunks = javascript
    .map((file) => path.join(root, '.next', file))
    .filter((file) => fs.existsSync(file));
  const rawBytes = chunks.reduce((total, file) => total + fs.statSync(file).size, 0);
  const gzipBytes = chunks.reduce(
    (total, file) => total + gzipSync(fs.readFileSync(file)).byteLength,
    0,
  );
  const rawKb = rawBytes / 1024;
  const gzipKb = gzipBytes / 1024;
  const status = gzipKb <= budget.max_gzip_kb ? 'PASS' : 'FAIL';
  console.log(
    `${status} ${label.padEnd(10)} raw=${rawKb.toFixed(1)}KB gzip=${gzipKb.toFixed(1)}KB budget=${budget.max_gzip_kb}KB`,
  );
  if (status === 'FAIL') failed = true;
}

if (!reportOnly && failed) process.exit(1);
