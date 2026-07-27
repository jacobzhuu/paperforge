# PaperForge

> AI 科研论文生成系统 —— **成稿优先(draft-first)、引用真实(citation-authentic)、流程宽松(gate-free)**

PaperForge 从 DeepSearch 的文献综述子系统分离重建而来。设计哲学相对旧系统完全反转：
旧系统「溯源优先、拒绝产出不合格报告」，PaperForge「成稿优先、引用真实、流程宽松」——
任何阶段失败都**降级而不阻断**，永远能拿到当前最好的稿子。

产出物：可直接投稿级排版的 **LaTeX + PDF** 论文初稿，支持可溯数据图表、学术示意图与
用户确认后的 AI 概念插图，全部参考文献真实可溯。

设计方案全文见 `docs/design.md`（源自 DeepSearch/plans/standalone-paper-generation-system-design-2026-07-24.md）；
前端设计原则与改造计划见 `docs/ui-design.md`。

## 两条产品管线
- **综述论文(review)**：题目 → 检索 → 文献库 → 大纲 → 分节写作 → 引用校验 → LaTeX/PDF
- **研究型论文(original)**：素材摄取 → 相关工作检索 → IMRaD 写作 → 数字一致性 lint → LaTeX/PDF
  （支持「成果写作模式」与「纯生成模式」；纯生成模式下实验结果以 `\todo{}` 占位，**不编造数据**）

## 引用真实性三条硬规则（取代全链路溯源）
- **R1 入库核验**：唯一 repository 查询只放行 selected、已核验、有 key、未撤稿的文献
- **R2 写作约束**：第一轮报告越权 key 并重写，第二轮移除并留编辑器标记；PaperIR 再做二道检查
- **R3 参考文献确定性生成**：key 核验入库时持久化，pybtex 导出只消费该 key，**LLM 永不手写参考文献**

## 技术栈
| 层 | 选型 |
|---|---|
| 后端 | Python 3.12 · FastAPI · Argon2id + opaque session · ARQ(Redis) worker · PostgreSQL 16 · SQLAlchemy 2 · Alembic · MinIO |
| 前端 | Next.js 15 (App Router) · TypeScript · Tailwind · shadcn/ui · Tiptap · Monaco · KaTeX |
| 编排 | SSE 进度流 · Tectonic 沙箱编译 LaTeX→PDF |
| LLM | OpenAI-compatible 多 provider，按角色路由 |
| 视觉 | ChartSpec/DiagramSpec · Matplotlib · Graphviz · Pillow · 可注册 ImageProvider · Cloudflare FLUX.1-schnell |

## 仓库结构
```
paper-forge/
├── apps/web                  # Next.js 15 前端
├── services/api              # FastAPI：项目/文献库/大纲/章节/导出 REST + SSE
├── services/worker           # ARQ：检索、摄取、卡片、大纲、写作、编译各管线任务
├── services/texd             # Tectonic 编译沙箱（HTTP 微服务，无外网）
├── services/visuald          # 图表/示意图渲染与位图规范化（无外网）
├── packages/scholar_gateway  # 检索适配器/规范化/去重/雪球/OA 全文/缓存（迁移自 DeepSearch）
├── packages/ingest           # PDF/文档解析、section 切块（迁移）
├── packages/llm_runtime      # provider seam、角色路由、JSON 校验、成本记账（迁移+扩展）
├── packages/paper_ir         # PaperIR schema、引用样式、BibTeX、i18n（迁移+新写）
├── packages/latex_render     # PaperIR → LaTeX 工程渲染、模板库、编译修复
├── packages/visuals          # 视觉规格、visuald 客户端、ImageProvider
├── packages/db               # 模型/仓储/迁移（约定迁移，模型新写）
├── packages/observability    # JSON 日志/指标（迁移）
└── infra/                    # postgres + redis + minio + texd + visuald compose
```

## 一键启动本地开发环境

前置条件：已安装 `uv`、Node.js、pnpm 和 Docker Desktop。仓库根目录只需执行：

```bash
./scripts/dev up
```

脚本会安装锁定依赖、按需启动 Docker Desktop，并启动 PostgreSQL、Redis、MinIO、
Tectonic、visuald、API、worker 和 Web。成功后会自动打开并打印：

- Web：http://localhost:3000
- API：http://localhost:8080
- Metrics：http://localhost:8080/metrics
- MinIO：http://localhost:19001
- visuald：http://localhost:8082/healthz

常用管理命令：

```bash
./scripts/dev status
./scripts/dev logs
./scripts/dev restart   # 改完代码用这个
./scripts/dev down
```

详细的手动启动和故障排查见 `docs/getting-started.md`。

## 实施状态（路线图 M0–M10）
M0–M10 主链路、个人账号数据隔离与 Cloudflare AI 生图适配已实现；详见 `docs/roadmap.md`、`docs/getting-started.md` 与
`docs/visual-generation-optimization-plan.md`。

## 账号初始化与存量数据迁移

升级已有环境前先备份 PostgreSQL 和对象存储，并进入维护窗口。迁移会创建账号、会话和
一次性令牌表，把所有匿名项目临时归入一个禁用占位账户；随后必须显式指定管理员邮箱认领：

```bash
uv run alembic upgrade head
uv run paperforge-admin bootstrap-admin \
  --email admin@example.com \
  --claim-legacy \
  --migrate-objects
```

命令会安全地交互读取密码，不接受命令行明文密码。对象迁移执行“复制 → SHA-256 校验 →
切换数据库键”，不会删除旧对象；确认运行七天稳定后再按备份策略清理旧路径。执行前可检查：

```bash
uv run paperforge-admin migrate-objects --dry-run
```

本地注册邮件写入 `data/auth-outbox/` 的权限受限文件。生产必须配置 HTTPS、
`AUTH_COOKIE_SECURE=true` 和 SMTP；浏览器会话只存在 HttpOnly cookie 中。

## 学术伦理与合规
本系统为**写作辅助工具**，产出为初稿。不伪造实验数据；全文仅经 OA/官方 API 渠道获取，
不绕 paywall；导出可附 AI 辅助声明。详见 `docs/design.md` §8。

## 迁移来源说明
代码采用**拷贝式迁移**，新仓库不 import DeepSearch，无运行时依赖。两系统仅共享「人」不共享代码。

## 生成产物在哪

导出的 PDF / docx / LaTeX 工程 / Markdown / BibTeX 落在对象存储里，
路径由 `.env` 的 `STORAGE_FS_ROOT` 决定（默认 `./data/objects`），私有文件始终带用户和项目
命名空间：

```
data/objects/users/<user_id>/projects/<project_id>/exports/v<文稿版本>/
  pdf-<hash>.pdf          docx-<hash>.docx
  latex_zip-<hash>.zip    markdown-<hash>.md
  markdown_bundle-<hash>.zip
  bibtex-<hash>.bib       compile_log-<hash>.log
```

也可以从界面「导出中心」下载，或走 API：

```bash
curl -b cookies.txt -s localhost:8080/api/v1/projects/<id>/exports
curl -b cookies.txt -sO localhost:8080/api/v1/projects/<id>/exports/<aid>/download
```

`data/` 已在 `.gitignore` 中，产物不会进版本库。
