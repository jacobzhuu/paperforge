# PaperForge 启动指南（Getting Started）

> 适用阶段：M0（脚手架 + 资产迁移）。本文中标注 **[已实测]** 的步骤在 2026-07-24 实际执行验证过。
> 初始代码审阅报告中的 P1/P2/P3 问题已在本仓库当前版本修复。

## 0. 一键启动 [已实测]

仓库根目录执行：

```bash
./scripts/dev up
```

脚本会自动安装锁定依赖、按需启动 Docker Desktop，并启动全部七个本地服务。健康检查
全部通过后，会自动打开 Web 页面并打印以下地址：

| 服务 | 地址 |
|---|---|
| Web | http://localhost:3000 |
| API | http://localhost:8080 |
| Prometheus metrics | http://localhost:8080/metrics |
| MinIO 控制台 | http://localhost:19001 |

管理命令：

```bash
./scripts/dev status   # 查看所有服务状态
./scripts/dev logs     # 查看最近日志
./scripts/dev down     # 停止服务；保留数据库和对象存储 volume
```

重复执行 `./scripts/dev up` 是安全的。需要阻止脚本自动打开浏览器时，可执行
`PAPERFORGE_OPEN_BROWSER=0 ./scripts/dev up`。以下章节保留各组件的手动启动和排障说明。

## 1. 前置要求

| 工具 | 版本要求 | 说明 |
|---|---|---|
| uv | ≥0.9 | Python 包管理与 workspace；若本机无 Python ≥3.12，uv 会自动下载 |
| Python | ≥3.12（各包 `requires-python`） | 本仓库 `.venv` 实际用 3.14 也可正常工作 |
| Node.js | ≥20 | 前端 |
| pnpm | 9.x | 建议经 corepack：`corepack enable && corepack prepare pnpm@9 --activate` |
| Docker + Compose v2 | 任意近期版本 | 起 pg/redis/minio/texd |

## 2. Python 依赖安装 [已实测]

仓库根目录执行：

```bash
uv sync --all-packages
```

- 会创建根 `.venv` 并以可编辑方式安装 7 个 packages + 3 个 services（uv workspace）。
- 验证安装成果：

```bash
uv run pytest -q        # 预期：全部测试通过
uv run ruff check .     # 预期：All checks passed!
```

`uv.lock` 已提交；CI 使用 `uv sync --all-packages --frozen` 保证依赖可复现。

## 3. 前端依赖安装 [已实测]

```bash
cd apps/web
pnpm install            # 实测 pnpm 9.15：约 15s，无 peer 冲突
```

`pnpm-lock.yaml` 已提交；CI 使用 pnpm 9 和 `--frozen-lockfile`。

## 4. 基建：docker compose [已实测]

```bash
docker compose -f infra/docker-compose.yml up -d --build
```

起四个服务：postgres:16、redis:7、minio、texd（texd 需要本地构建，首次较慢，
构建期会预热 Tectonic 宏包缓存）。

### 各服务健康检查

**postgres**（宿主端口 15432）

```bash
docker compose -f infra/docker-compose.yml exec postgres pg_isready -U paperforge
# 预期：accepting connections
psql postgresql://paperforge:paperforge@localhost:15432/paperforge -c 'select 1'
```

**redis**（宿主端口 16379）

```bash
docker compose -f infra/docker-compose.yml exec redis redis-cli ping   # 预期：PONG
```

**minio**（S3 API 19000 / 控制台 19001，账号 `paperforge` / `paperforge-secret`）

```bash
curl -sf http://localhost:19000/minio/health/live && echo OK
# 浏览器打开 http://localhost:19001 可登录控制台
```

compose healthcheck 直接请求 `/minio/health/live`，不依赖镜像内置 `mc`。

**texd**（宿主回环端口 8081）

texd 同时接入内部服务网络与禁用 masquerade 的宿主入口网络：前者供未来容器化
api/worker 访问，后者只绑定 `127.0.0.1:8081`，不提供外网出口。验证：

```bash
curl -sf http://127.0.0.1:8081/healthz
# 预期：{"status":"ok"}
```

texd 服务进程本身（FastAPI /healthz、/compile 的降级路径）已在无容器环境直跑验证过 [已实测]。

## 5. services/api 本地启动 [已实测]

```bash
cp .env.example .env      # 开发期建议先把 LLM_DEFAULT_PROVIDER 改为 noop（见 §8）
uv run paperforge-api
```

必须从**仓库根目录**启动：`Settings` 的 `env_file=".env"` 按当前工作目录解析。

验证（均为实测输出）：

```bash
curl -s http://localhost:8080/healthz
# {"status":"ok"}

curl -s http://localhost:8080/api/v1/projects
# []

curl -s -X POST http://localhost:8080/api/v1/projects \
  -H 'Content-Type: application/json' \
  -d '{"title":"t","paper_type":"review"}'
# 501 {"detail":"M1 未实现：项目创建将在文献库 MVP 落地"}   ← 预期行为，路由是 M1 前的契约占位

curl -s http://localhost:8080/metrics | head    # Prometheus 文本格式
```

M0 阶段 API 不连数据库（项目路由未接线），所以即使 postgres 未启动，以上命令也全部可用。

## 6. services/worker（ARQ）启动 [已实测]

```bash
uv run arq paperforge_worker.worker.WorkerSettings
```

预期行为：

- 连上 Redis 后进入空转，等待任务队列；无 Redis 时每秒重试 5 次后退出（实测确认）。
- `WorkerSettings.redis_settings` 由 `REDIS_URL` 构建；可连接非默认主机、端口和 DB。
- 当前 6 个 pipeline 阶段（scope/ranking/cards/outline/writing/coverage_hints）与编排入口
  `run_full_pipeline` 全部是 **stub**：函数签名即契约，函数体 `raise NotImplementedError`。
  因此现在向队列投递 `run_full_pipeline` 会立即得到一个 failed job —— 这是 M0 的预期状态，
  实现随 M1–M4 落地。

## 7. apps/web 启动 [已实测]

```bash
cd apps/web
pnpm dev
```

访问 http://localhost:3000 —— 实测首页 200，渲染 M0 占位页（项目定位 + 三硬规则说明）。
`pnpm build` 亦可用于生产构建检查。

## 8. .env 变量说明

| 变量 | 用途 | M0 是否必填 |
|---|---|---|
| `DATABASE_URL` | asyncpg 连接串，`db.session.make_engine` 使用 | 可留默认（M0 API 未连库；M1 起必填） |
| `REDIS_URL` | API/ARQ 队列连接 | 可留默认 |
| `STORAGE_BACKEND` | `filesystem` / `minio`，示例与代码默认均为 `filesystem` | 开发期可留默认 |
| `STORAGE_FS_ROOT` | filesystem 后端根目录 | 用 filesystem 时才需要，默认 `./data/objects` |
| `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` / `MINIO_BUCKET` / `MINIO_SECURE` | MinIO 连接 | 用 minio 后端时必填；默认值与 compose 一致 |
| `LLM_DEFAULT_PROVIDER` | `noop` / `openai`（=任意 OpenAI-compatible） | 开发期建议 `noop`：示例默认 `openai` 且 key 为空，一旦接通 LLM 调用会直接 configuration_error |
| `LLM_OPENAI_BASE_URL` | OpenAI-compatible 端点（DeepSeek 等替换此处） | provider=openai 时必填 |
| `LLM_OPENAI_API_KEY` | API key | provider=openai 时必填 |
| `LLM_ROLE_MODELS` | JSON：角色→模型映射（planner/extractor/reranker/writer/polisher/verifier） | 可留空 `{}`，回退 DEFAULT_ROLE_MODELS |
| `SCHOLAR_CONTACT_EMAIL` / `SCHOLAR_USER_AGENT` | OpenAlex/Crossref polite pool 礼貌标头 | M0 可留空（providers 尚未迁移）；M1 检索前务必填真实邮箱 |
| `SEMANTIC_SCHOLAR_API_KEY` | S2 配额提升 | 可留空 |
| `TEXD_URL` / `TEXD_TIMEOUT_SECONDS` | 编译沙箱地址 | M0–M2 可留默认（尚无调用方） |
| `CORS_ALLOW_ORIGINS` | API CORS 白名单，逗号分隔 | 默认 `http://localhost:3000` 即可 |
| `API_HOST` / `API_PORT` | `paperforge-api` 启动命令监听地址与端口 | 可留默认 |

## 9. 推荐冒烟顺序

```bash
./scripts/dev up
./scripts/dev status
curl -fsS http://localhost:8080/healthz
curl -fsS http://localhost:8081/healthz
curl -fsS http://localhost:19000/minio/health/live
curl -fsS http://localhost:3000
```

## 10. 当前边界（M0）

- 数据库表虽已建模（19 张），但 **alembic 初始迁移尚未生成**，`uv run alembic ...` 流程见
  `packages/db/migrations/README.md`（M0 剩余工作）。
- 五源检索适配器 / snowball / OA 全文 / 重型文档抽取器未迁移（见
  `packages/scholar_gateway/providers/README.md` 与 `packages/ingest/EXTRACTORS_MIGRATION.md`）。
- 端到端"题目→论文"链路自 M2 起才可用；当前可验证的是：单测、API 契约、worker 骨架、
  前端骨架与四件基建。
