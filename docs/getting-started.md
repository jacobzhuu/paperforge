# PaperForge 启动指南（Getting Started）

> 适用阶段：M0–M9。本文中标注 **[已实测]** 的步骤已在本仓库对应交付中执行验证。
> 初始代码审阅报告中的 P1/P2/P3 问题已在本仓库当前版本修复。

## 0. 一键启动 [已实测]

仓库根目录执行：

```bash
./scripts/dev up
```

脚本会自动安装锁定依赖、按需启动 Docker Desktop，并启动全部八个本地服务。健康检查
全部通过后，会自动打开 Web 页面并打印以下地址：

| 服务 | 地址 |
|---|---|
| Web | http://localhost:3000 |
| API | http://localhost:8080 |
| Prometheus metrics | http://localhost:8080/metrics |
| MinIO 控制台 | http://localhost:19001 |
| visuald | http://localhost:8082/healthz |

管理命令：

```bash
./scripts/dev status   # 查看所有服务状态
./scripts/dev logs     # 查看最近日志
./scripts/dev restart   # 改完代码用这个（up 会跳过已在运行的进程，不加载新代码）
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

- 会创建根 `.venv` 并以可编辑方式安装所有 workspace packages/services，包括
  `packages/visuals` 和 `services/visuald`。
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

起五个服务：postgres:16、redis:7、minio、texd 和 visuald（后两者需本地构建；
texd 构建期会预热 Tectonic 宏包缓存）。

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

**visuald**（宿主回环端口 8082）

visuald 与 texd 一样使用无出站网络、只读根文件系统和资源限额；只接受结构化规格和
服务端内联的已解析数据，不执行 Python/DOT/Mermaid 源码、不读取远程 URL。

```bash
curl -sf http://127.0.0.1:8082/healthz
# 预期：{"status":"ok"}
```

**Tectonic 包缓存必须可写**（`texdcache` 命名卷）。容器根文件系统是 `read_only`，
而 Tectonic 在缓存**未命中**时要先在缓存目录建临时文件——建不了就直接失败。
最坏的表现不是编译报错，而是「编译成功但引用全是 `[?]`」：BibTeX 打不开 `.bst`，
Tectonic 把它降级成一行 warning 后照常产出 PDF。命名卷首次挂载会用镜像里构建期
预热好的缓存做种，所以既保住预热成果，又让未命中不再是硬失败。

改过 compose 之后要重建容器才会挂上卷：

```bash
docker compose -f infra/docker-compose.yml up -d texd
```

代码侧还有一层兜底：`.bst` 仍然取不到时，导出会把 `\bibliography{refs}` 换成
由库内元数据确定性生成的内联 `thebibliography` 重编一次，并在导出中心标注降级
（见 `latex_render.compile.compile_with_repair` 的 `inline_bibliography`）。

## 5. services/api 本地启动 [已实测]

```bash
cp .env.example .env      # 开发期建议先把 LLM_DEFAULT_PROVIDER 改为 noop（见 §8）
uv run paperforge-api
```

必须从**仓库根目录**启动：`Settings` 的 `env_file=".env"` 按当前工作目录解析。

健康检查不需要登录；业务 API 需要先完成 §5.1 的账号初始化或页面注册：

```bash
curl -s http://localhost:8080/healthz
# {"status":"ok"}

curl -s http://localhost:8080/api/v1/projects
# 401 {"detail":{"code":"authentication_required"}}

curl -s http://localhost:8080/metrics | head    # Prometheus 文本格式
```

### 5.1 首次建库或从匿名项目升级

已有数据升级前必须备份数据库和对象存储。应用迁移后，匿名项目先由禁用占位账户持有，
不会在管理员认领前暴露给新注册用户：

```bash
uv run alembic upgrade head
uv run paperforge-admin migrate-objects --dry-run
uv run paperforge-admin bootstrap-admin \
  --email admin@example.com \
  --claim-legacy \
  --migrate-objects
```

`bootstrap-admin` 通过安全交互读取密码。如果需要把存量项目拆给多个已创建账户，可传入
UTF-8 CSV：`--project-map project-owners.csv`，列名为 `project_id,email`。对象迁移不会删除
旧路径；新路径通过大小与 SHA-256 校验并切换数据库键后，旧对象至少保留七天。

本地注册/重置邮件保存在 `data/auth-outbox/` 的权限受限文件里。生产环境禁止此模式，必须
配置 HTTPS、`AUTH_COOKIE_SECURE=true`、`AUTH_EMAIL_MODE=smtp` 和 SMTP 参数。

## 6. services/worker（ARQ）启动 [已实测]

```bash
uv run arq paperforge_worker.worker.WorkerSettings
```

预期行为：

- 连上 Redis 后进入空转，等待任务队列；无 Redis 时每秒重试 5 次后退出（实测确认）。
- `WorkerSettings.redis_settings` 由 `REDIS_URL` 构建；可连接非默认主机、端口和 DB。
- `run_full_pipeline` 已实现检索、入库、卡片、大纲、分节写作、视觉建议和渲染。
  `visual_plan` 在 write 后执行，仅建议不生成付费 AI 图，失败时不阻断正文与导出。

## 7. apps/web 启动 [已实测]

```bash
cd apps/web
pnpm dev
```

访问 http://localhost:3000 —— 未登录会转到登录页。注册并验证邮箱后，可从 Prompt Canvas
新建论文，进入文献库、大纲、写作台、素材中心和导出中心。前端默认只请求同源
`/api/v1`，Next 由 `PAPERFORGE_API_INTERNAL_BASE` 转发到 FastAPI。
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
| `TEXD_URL` / `TEXD_TIMEOUT_SECONDS` | 编译沙箱地址 | PDF/LaTeX 导出必需，默认 `http://localhost:8081` |
| `VISUALS_ENABLED` | 视觉资产/API/自动建议总开关 | 默认 `true` |
| `VISUALD_URL` / `VISUALD_TIMEOUT_SECONDS` | 确定性图表/示意图渲染与位图规范化服务 | 开启 visuals 时需要，默认 `http://localhost:8082` |
| `AI_IMAGES_ENABLED` | AI 概念插图功能开关 | 默认 `false`，不影响图表/示意图 |
| `IMAGE_PROVIDER` / `IMAGE_BASE_URL` / `IMAGE_MODEL` | 独立图像 provider 选择/端点/模型 | 默认 `cloudflare` + `https://api.cloudflare.com/client/v4` + `@cf/black-forest-labs/flux-1-schnell` |
| `IMAGE_ACCOUNT_ID` | Cloudflare Account ID（不是 Token） | Cloudflare 生图时与 Token 一起必填；占位留空时 AI 按钮保持禁用 |
| `IMAGE_API_KEY` | 图像 provider 密钥；Cloudflare 中填写 Workers AI API Token，不复用文本 LLM 密钥 | 真实 AI 生图时必填；接口不返回密钥、Account ID 或内部端点 |
| `IMAGE_TIMEOUT_SECONDS` / `IMAGE_MAX_RETRIES` | 图像调用超时与有界重试次数 | 默认 `180` 秒、`2` 次；鉴权/审核/参数错误不会重试 |
| `CORS_ALLOW_ORIGINS` | API CORS 白名单，逗号分隔 | 默认 `http://localhost:3000` 即可 |
| `API_HOST` / `API_PORT` | `paperforge-api` 启动命令监听地址与端口 | 可留默认 |
| `AUTH_COOKIE_SECURE` | 使用 `__Host-paperforge_session` 安全 cookie；生产必须为 `true` | 本地 HTTP 为 `false` |
| `AUTH_SESSION_DAYS` / `AUTH_IDLE_DAYS` | 会话绝对过期 / 空闲过期，默认 30 / 7 天 | 可留默认 |
| `AUTH_RATE_LIMIT_ENABLED` | 登录、注册、找回密码 Redis 限流；不可用时失败关闭 | 应保持 `true` |
| `PUBLIC_APP_URL` | 验证与重置链接的站点根地址 | 生产必须为 HTTPS |
| `AUTH_EMAIL_MODE` / `AUTH_EMAIL_OUTBOX_DIR` | 本地权限受限投递箱或生产 SMTP | 本地 `file`，生产 `smtp` |
| `SMTP_*` | 认证邮件发件配置 | 生产必填 |
| `PAPERFORGE_API_INTERNAL_BASE` | Next 服务端同源代理的 FastAPI 内部地址 | 本地默认 `http://localhost:8080` |
| `NEXT_PUBLIC_DEMO_MODE` | 显式开发演示数据开关；生产构建强制关闭 | 默认 `false` |

## 9. 推荐冒烟顺序

```bash
./scripts/dev up
./scripts/dev status
curl -fsS http://localhost:8080/healthz
curl -fsS http://localhost:8081/healthz
curl -fsS http://localhost:8082/healthz
curl -fsS http://localhost:19000/minio/health/live
curl -fsS http://localhost:3000
```

## 10. 当前边界

- 一键全管线、五种文本产物、M8 图文链路与 M9 个人账号数据隔离已实现；开发环境可以在
  `IMAGE_API_KEY` 为空时完整验证图表、示意图和无图导出。
- 真实 Cloudflare 图像冒烟需要手动补齐 `IMAGE_ACCOUNT_ID`、`IMAGE_API_KEY` 并开启
  `AI_IMAGES_ENABLED`；系统不会在自动建议、启动检查或缺配置时触发外部调用。
- worker 只依赖统一的 `ImageProvider` 协议。现有 Cloudflare/OpenAI 适配器均通过注册表装配；
  未来接入 GPT Image、Gemini 或本地 ComfyUI 时，只需新增适配器并注册，无需修改视觉业务管线。
