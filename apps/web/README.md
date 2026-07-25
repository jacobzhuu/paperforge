# PaperForge Web（Next.js 15）

前端信息架构见 `docs/design.md §4.8`。当前实现：Tailwind + 手写 shadcn 风格组件库、全局侧边栏布局、
类型化 API 客户端（后端 501/不可达时自动降级到示例数据），并落地两大核心页面。

## 页面状态
| 页面 | 路由 | 状态 |
|---|---|---|
| 项目列表 + 新建向导 | `/projects` | ✅ 已实现：卡片列表 + 四步向导（类型→主题/贡献点→模板/语言→模式） |
| 文献工作台 | `/library?project=<id>` | ✅ 已实现：检索结果表（相关性排序、勾选入库）、卡片详情抽屉、雪球扩展、DOI/BibTeX 导入、检索统计面板、检索源能力筛选 |
| 大纲编辑器 | `/outline` | 🚧 导航占位（M3） |
| 写作工作台（核心） | `/write` | 🚧 导航占位（M4） |
| 素材中心（研究型） | `/assets` | 🚧 导航占位（M5） |
| 导出中心 | `/export` | 🚧 导航占位（M6） |
| 设置 | `/settings` | 🚧 导航占位（M2+） |

## 目录
```
app/                    App Router 页面（每页一个 route）
components/ui/          手写 shadcn 风格基础组件（button/card/input/table/dialog/drawer/tabs…）
components/layout/      侧边栏、页头、占位页
components/projects/    新建向导
components/library/     工作台、卡片抽屉、导入弹窗、检索统计、源筛选
lib/                    types / api（含降级）/ mock / labels / utils / sourceCapabilities
```

## API 与降级
- 基址由 `NEXT_PUBLIC_API_BASE` 指定，默认 `http://localhost:8080`；请求走 `/api/v1`。
- 后端返回 `501`/`404` 或不可达时，`lib/api.ts` 自动回退到 `lib/mock.ts` 的示例数据，
  并在页面顶部显示"示例数据预览"提示；对应 REST 端点落地后无需改前端即切换为真实数据。

## 技术栈
Next.js 15 (App Router) · TypeScript · Tailwind CSS · shadcn 风格组件 · lucide-react。
后续核心页将接入 Tiptap（写作）、Monaco/KaTeX（LaTeX/公式）。

## 开发
```bash
pnpm install
pnpm dev      # http://localhost:3000
pnpm lint     # tsc --noEmit
pnpm build
```
