# PaperForge 启动指南（Getting Started）

> 适用阶段：M0–M11。本文中标注 **[已实测]** 的步骤已在本仓库对应交付中执行验证。
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
./scripts/dev status   # 查看所有服务状态（含正在生效的部署与 Funnel 指向）
./scripts/dev logs     # 查看最近日志
./scripts/dev restart   # 改完代码用这个（up 会跳过已在运行的进程，不加载新代码）
./scripts/dev down     # 停止服务；保留数据库和对象存储 volume
```

重复执行 `./scripts/dev up` 是安全的。需要阻止脚本自动打开浏览器时，可执行
`PAPERFORGE_OPEN_BROWSER=0 ./scripts/dev up`。以下章节保留各组件的手动启动和排障说明。

### 0.1 有部署在跑时的 `restart`

`restart` 会先检测有没有正在跑的 `paperforge-deploy-*` compose 项目。有的话它做的是
**滚动**而不是重启：重建 api/worker/web 镜像 → 在旁边起一套新项目（新端口）→ 健康检查
通过后把 Tailscale Funnel 指向新端口 → 旧的那套留在原地。

本机 Docker 是 snap 包，曾因 AppArmor 的 `docker-default` 缺少
`snap.docker.dockerd` signal/ptrace 规则而无法停止容器。修复后仍保留蓝绿更新，
让新版本健康检查与 Funnel 切换完成后再独立清理旧版本。

因此有两件事必须成立，`infra/docker-compose.bluegreen.yml` 已经处理：

- **每套部署用不同的 Redis 库号**（`PAPERFORGE_DEPLOY_REDIS_DB`）。旧 worker 停不掉且
  一直在取任务，同库就等于把大约一半的生成任务交给旧镜像跑——表现是「新功能时灵时不灵」，
  很难看出是部署问题。切换时在途的任务留在旧库，由旧 worker 跑完。
- **Funnel 每次都要重新指向**。tailscaled 本身不用重启（转发规则而已），但新容器必然换端口。
  本机 tailscaled 跑在用户态、socket 不在默认路径，直接 `tailscale funnel status` 会报
  「not running」，脚本从进程命令行里取 `--socket=`。
- **公网验收是部署成功条件**。切换后脚本会确认 Funnel 状态里的目标端口等于新容器端口，
  再逐字节比较新容器与公网 `/login` 的响应正文；任一不一致都会让部署命令失败，不能把
  “本地镜像已启动”误报成“公网新版本已生效”。

`--local` 强制走本地栈，`./scripts/dev deploy` 强制走部署。部署状态记在 `.paperforge/deploy.env`。
旧部署可在确认不再承载流量后单独清理；清理命令不得带 `-v`。

## 1. 前置要求

| 工具 | 版本要求 | 说明 |
|---|---|---|
| uv | ≥0.9 | Python 包管理与 workspace；若本机无 Python ≥3.12，uv 会自动下载 |
| Python | 3.12（各包 `requires-python`） | `.python-version` 固定宿主开发基线；生产镜像同样使用 3.12 |
| Node.js | ≥20 | 前端 |
| pnpm | 9.15.9 | 缺失时开发脚本依次尝试 Corepack 与固定版本 npx |
| Docker Engine | ≥26 | macOS 使用 Docker Desktop；Linux 使用 systemd |
| Docker Compose | ≥2.35.1 | Focal 固定为 2.35.1 |
| Docker Buildx | ≥0.23.0 | Focal 固定为 0.23.0 |

生产环境的完整支持矩阵、Ubuntu 安装脚本、外置配置、备份恢复及迁移流程见
[`production-deployment.md`](production-deployment.md)。开发模式仍由本页说明，并继续把基础设施
端口绑定在 loopback、把 API/Worker/Web 作为宿主机热更新进程运行。

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

`pnpm-lock.yaml` 已提交；CI 使用 pnpm 9.15.9 和 `--frozen-lockfile`。

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
- `run_full_pipeline` 已实现检索、全文卡片、大纲、分节写作、证据检查、质量报告、
  视觉建议、导出预检与渲染。默认 `draft+narrative` 保持快速草稿行为；
  `submission` 在内容质量门失败时任务仍可技术成功，但不会进入视觉与投稿导出。
- 综述大纲会加入跨研究比较、方法局限与证据冲突综合章节，并生成可直接进入 PDF 的
  文献方法/证据基础矩阵；正文引用按句绑定，避免段末堆积整段引用。
- `visual_plan` 只建议、不自动生成付费 AI 图。视觉生成结果会预检内容包围盒、留白、
  最低字号和宽高比；短综述不会在降级路径强塞泛化结构图或装饰性插图。最终 PDF 再检查
  真实图片坐标、题注、缺字、引用和书目后置图。

## 7. apps/web 启动 [已实测]

```bash
cd apps/web
pnpm dev
```

访问 http://localhost:3000 —— 未登录会转到登录页。注册并验证邮箱后，可从 Prompt Canvas
新建论文，进入文献库、大纲、写作台、素材中心和导出中心。前端默认只请求同源
`/api/v1`，Next 由 `PAPERFORGE_API_INTERNAL_BASE` 转发到 FastAPI。
`pnpm build` 亦可用于生产构建检查。

### 7.1 草稿与投稿模式

- 项目概览的“一键全管线”可选择 `draft/submission` 与 `narrative/systematic`。
- 投稿前在概览填写并确认独立的发表题名、作者和关键词；项目内部名称不会再代替论文题名。
- 写作台“质量”页签展示快照版本、过期状态、结构化阻断项及句级论断—证据锚点；
  全文证据可人工确认或驳回，确认后需重新生成质量报告。
- 导出中心选择“投稿候选稿”时，只接受当前快照上 `preflight_ready` 的报告；PDF 通过最终
  视觉检查后，产物和报告才标记为 `submission_ready`。历史产物显示为“未评估”。

公共请求示例：

```json
POST /api/v1/projects/{id}/generate
{"quality_profile":"submission","review_style":"systematic"}

POST /api/v1/projects/{id}/exports
{"formats":["pdf","latex_zip"],"quality_profile":"submission"}
```

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
| `AI_IMAGES_ENABLED` | AI 概念插图功能开关 | 默认 `true`；未配置 Yunwu 密钥时自动保持为建议，不阻断全流程 |
| `IMAGE_PROVIDER` | 图像 provider 选择（`yunwu` / `cloudflare` / `openai`） | 默认 `yunwu`：它的 Images API 真正接受尺寸与质量，Cloudflare FLUX 只接受提示词与步数 |
| `YUNWU_API_BASE_URL` / `YUNWU_API_KEY` / `YUNWU_IMAGE_MODEL` / `YUNWU_IMAGE_TIMEOUT_SECONDS` | 默认 provider 的独立配置；支持 Images API 的 Base64 与临时 URL 响应 | 默认 `https://yunwu.ai/v1` + `gpt-image-1`，密钥不会返回前端 |
| `IMAGE_BASE_URL` / `IMAGE_MODEL` | 其他 provider 的端点与模型 | 默认 `https://api.cloudflare.com/client/v4` + `@cf/black-forest-labs/flux-1-schnell` |
| `IMAGE_ACCOUNT_ID` | Cloudflare Account ID（不是 Token） | Cloudflare 生图时与 Token 一起必填；占位留空时 AI 按钮保持禁用 |
| `IMAGE_API_KEY` | 非 Yunwu provider 的密钥；Cloudflare 中填写 Workers AI API Token，不复用文本 LLM 密钥 | 真实 AI 生图时必填；接口不返回密钥、Account ID 或内部端点 |
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

- 一键全管线、双模式质量门、五种文本产物、M8 图文链路与 M9 个人账号数据隔离已实现；开发环境可以在
  图像密钥为空时完整验证图表、示意图和无图导出。
- 真实图像冒烟需要补齐当前 provider 的凭据（默认 Yunwu：`YUNWU_API_KEY`；Cloudflare 另需
  `IMAGE_ACCOUNT_ID` + `IMAGE_API_KEY`）并开启 `AI_IMAGES_ENABLED`。普通视觉建议不会自动
  生图；“跑通全流程”只通过 Yunwu 自动生成一张论文摘要图。启动检查或缺配置时不触发外部调用，
  摘要图保留为可重试建议且不阻断导出。
- AI 插图的提示词由文本模型（线上为 DeepSeek）在**规划/起草**阶段写成，落进
  `AIImageSpec.refined_prompt`；文本模型不可用时退回字段拼接，插图照样能生成。生成确认框展示的
  与真正发出去的始终是同一句（都走 `AIImageSpec.render_prompt()`）。
- worker 只依赖统一的 `ImageProvider` 协议。现有 Yunwu/Cloudflare/OpenAI 适配器均通过注册表装配；
  未来接入 Gemini 或本地 ComfyUI 时，只需新增适配器并注册，无需修改视觉业务管线。
