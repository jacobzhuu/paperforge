# PaperForge

PaperForge 采用证据驱动的科研 Agent 工作流：固定主链路结合模型评审与规则约束的自适应修复，支持持久化修复决策、动作边界恢复及正文快照绑定的质量检查。实际能力、架构边界与验证方式见 [Agent 平台升级](docs/agent-platform-upgrade.md)。

当前升级能力与边界见 [Agent 平台升级](docs/agent-platform-upgrade.md)：局部 LangGraph 修复、项目内混合证据检索、任务详情和 OpenTelemetry/Langfuse 观测；工程上线与人工质量验收分别记录，详见 [本次验收](docs/acceptance/agent-platform-20260918.md)。

> 科研写作 Agent —— **证据驱动、引用可溯、可恢复执行**

PaperForge 从 DeepSearch 的文献综述子系统分离重建而来。系统保留已生成的稿件和执行记录；证据不足或修复预算耗尽时暂停并请求补充材料，未完成的评估不会被标记为质量验收通过。

产出物：按目标模板排版的 **LaTeX + PDF** 论文初稿，支持可溯数据图表、学术示意图与
用户确认后的 AI 概念插图，保留参考文献与证据来源。

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

前置条件：已安装 `uv`、Node.js、pnpm，以及 macOS 上的 Docker Desktop 或 Linux 上由
systemd 管理的 Docker。仓库根目录只需执行：

```bash
./scripts/dev up
```

脚本会安装锁定依赖；macOS 会按需启动 Docker Desktop，Linux 会检查 Docker 服务；随后启动 PostgreSQL、Redis、MinIO、
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
./scripts/dev reap      # 只读检查已排空的旧蓝绿部署；确认后加 --apply 清理
./scripts/dev down
```

`restart` 会先看有没有正在跑的部署（`paperforge-deploy-*` compose 项目）：有就重建
api/worker/web 镜像、在旁边起一套新的、再把 Tailscale Funnel 指过去；没有就照旧重启
本地栈。之所以是「起一套新的」而不是原地重启，见 `docs/getting-started.md` 的部署一节。
`--local` 强制走本地栈，`./scripts/dev deploy` 强制走部署。

详细的手动启动和故障排查见 `docs/getting-started.md`。

## 生产部署

生产基线支持 macOS Intel/Apple Silicon、Ubuntu 22.04.5 LTS，以及已启用 Ubuntu Pro/ESM
的 Ubuntu 20.04.6 LTS。API、Worker 和 Web 使用 Python 3.12 / Node 20 / pnpm 9.15.9
多阶段非 root 镜像；Worker 镜像内置 Pandoc。生产栈只向宿主机发布
`127.0.0.1:3000`，PostgreSQL、Redis、MinIO、API、texd 和 visuald 均只在容器网络内可见。

```bash
sudo ./scripts/install-ubuntu-container-tools   # 仅 Ubuntu；按 Focal/Jammy 自动选择来源
export PAPERFORGE_ENV_FILE=/absolute/path/to/paperforge.env
./scripts/ops preflight
./scripts/ops up
./scripts/ops status
```

统一运维入口还提供 `restart/down/logs/backup/restore`。Ubuntu 20.04 如果未同时启用
`esm-infra` 与 `esm-apps`，生产预检会拒绝启动。完整安装、迁移、Tailscale Funnel 切换与
七天回滚流程见 [`docs/production-deployment.md`](docs/production-deployment.md)。

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
