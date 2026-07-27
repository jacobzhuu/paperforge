# PaperForge Web（Next.js 15）

前端信息架构见 `docs/design.md §4.8`。当前实现：Tailwind + 手写 shadcn 风格组件库、全局侧边栏布局、
类型化 API 客户端、HttpOnly cookie 登录态和受保护应用布局，并落地完整论文工作区。

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

## 认证、API 与演示模式

- 登录、注册、邮箱验证、找回/重置密码位于独立认证布局；应用区会通过 `/auth/me` 验证会话。
- 浏览器默认只访问同源 `/api/v1`，Next 通过 `PAPERFORGE_API_INTERNAL_BASE` 转发到 FastAPI；
  所有请求、上传和 SSE 都携带 HttpOnly session cookie。
- 401 会触发统一会话失效和登录回跳；403/404 不会伪装成示例数据。
- 只有开发环境显式设置 `NEXT_PUBLIC_DEMO_MODE=true` 时才允许示例数据回退；生产构建强制关闭。

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
