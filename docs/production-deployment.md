# PaperForge 生产部署与跨主机迁移

本文适用于 macOS Intel/Apple Silicon + Docker Desktop、Ubuntu 22.04.5 LTS，以及已启用
Ubuntu Pro/ESM 的 Ubuntu 20.04.6 LTS。两个 Ubuntu 版本运行完全相同的应用镜像、Compose、
数据库结构、对象格式和验收套件；差异只在宿主 Docker 工具链的安装来源。

## 1. 支持基线

| 宿主 | Docker 来源 | 版本门槛 |
|---|---|---|
| macOS Intel / Apple Silicon | Docker Desktop | Engine ≥26、Compose ≥2.35.1、Buildx ≥0.23.0 |
| Ubuntu 22.04 | Docker 官方 apt 仓库 | 同上，允许当前更新版本 |
| Ubuntu 20.04 | Ubuntu Pro/ESM 的 `docker.io` | Engine 26.x、Compose 2.35.1、Buildx 0.23.0 |

Focal 不安装已经退出 Docker 当前官方 Ubuntu 支持列表的 Docker CE Focal 包。Compose 与
Buildx 二进制按架构下载到系统 Docker CLI plugin 目录，并用安装脚本内固定的 SHA-256
校验。Focal 预检还要求 5.4/5.15 内核、cgroup v1/v2、overlay2、Docker systemd 开机启动，
以及 `esm-infra`、`esm-apps` 均为 `entitled=yes/status=enabled`。

背景资料：Docker 当前官方列表包含 Jammy、不再包含 Focal；Ubuntu Pro 为 Focal 提供
`esm-infra` 与 `esm-apps` 的延长安全维护。参见 [Docker Ubuntu 安装说明](https://docs.docker.com/engine/install/ubuntu/)、
[Docker Focal 包归档](https://download.docker.com/linux/ubuntu/dists/focal/pool/stable/amd64/)、
[Ubuntu Pro ESM 说明](https://documentation.ubuntu.com/pro-client/en/v30/explanations/about_esm/)。

目标机最低 4 vCPU、8 GiB RAM、50 GiB 可用磁盘。先完成系统升级；Focal 再确认：

```bash
sudo pro status
sudo pro enable esm-infra
sudo pro enable esm-apps
sudo apt update && sudo apt upgrade
```

安装容器运行时：

```bash
cd /opt/paper-forge
sudo PAPERFORGE_DOCKER_USER="$USER" ./scripts/install-ubuntu-container-tools
# 重新登录，使 docker 组生效
```

该脚本不会执行远程 `curl | sh`。Jammy 若已安装冲突的 `docker.io` 会停止并要求先人工处理，
避免静默替换已有运行时。

## 2. 生产配置与启动

配置文件必须位于仓库外、权限必须为 `0600`。不要把密码、Token 或 `.env` 复制进镜像上下文：

```bash
sudo install -d -m 0700 -o "$USER" -g "$(id -gn)" /etc/paperforge
sudo install -m 0600 -o "$USER" -g "$(id -gn)" \
  .env.production.example /etc/paperforge/paperforge.env
export PAPERFORGE_ENV_FILE=/etc/paperforge/paperforge.env
```

编辑所有 `CHANGE_ME`；数据库密码在 `DATABASE_URL` 中必须做 URL 编码。生产硬性要求：MinIO、
精确 HTTPS Origin、Secure Cookie、关闭开发登录、SMTP。LLM 与真实 AI 图像凭据按实际需要配置；
不启用 AI 图像不影响图表、示意图、PDF、DOCX 或 SSE。

```bash
./scripts/ops preflight
./scripts/ops up
./scripts/ops status
./scripts/ops logs web api worker
```

如果目标机不能访问镜像仓库，可先用 `docker load` 导入经过 SHA-256 校验的离线镜像包，在外部
环境文件中设置 `PAPERFORGE_SKIP_BUILD=true`，再执行 `./scripts/ops up`。此模式要求包中包含
`paperforge-api:local`、`paperforge-worker:local`、`paperforge-web:local`、
`paperforge-texd:local`、`paperforge-visuald:local` 及三项基础设施镜像；默认仍会在线构建。
离线包可通过 `PAPERFORGE_POSTGRES_IMAGE`、`PAPERFORGE_REDIS_IMAGE` 和
`PAPERFORGE_MINIO_IMAGE` 把三项基础镜像指向已导入的本地标签。

```bash
./scripts/load-offline-images /srv/paperforge-images-linux-amd64.tar.gz
```

加载器会先校验外层 SHA-256，再校验包内八个镜像归档的大小、SHA-256 和 amd64 架构，任何一项
不一致都会在调用 `docker load` 前停止。

`preflight` 检查操作系统、ESM、架构、内核/cgroup、Docker/Compose/Buildx、资源、端口和认证配置。
`up` 会先构建镜像并运行 Alembic；只有迁移容器成功退出后 API/Worker 才启动。长期服务为
`restart: unless-stopped`。`restart` 会重新构建并协调全栈；`down` 不删除卷。

生产宿主仅监听 `127.0.0.1:3000`。不要给 PostgreSQL、Redis、MinIO、API、texd 或 visuald
增加宿主 `ports`；Docker 发布端口可能绕过常规 UFW 规则。

## 3. 备份与空卷恢复

备份目录必须为空。命令会拒绝存在 `queued/running` 生成任务的数据库，并输出 PostgreSQL
custom dump、全部表计数、Alembic 版本、对象 tar.gz、对象 key/大小/SHA-256 清单与总校验文件；
Redis 队列和密钥不会进入备份。

```bash
./scripts/ops backup /srv/backups/paperforge-2026-07-28
```

恢复只接受校验通过的备份，并要求显式确认空目标。它会拒绝已有业务表或任何 MinIO 对象的
目标，恢复后执行 `alembic upgrade head`，再比较全部表计数、Alembic 版本和每个对象：

```bash
./scripts/ops restore /srv/backups/paperforge-2026-07-28 --target-empty
./scripts/ops up
```

定期在隔离 Compose 项目上完成“备份 → 删除测试卷 → 恢复 → 登录/下载”的演练；不要用生产卷
做破坏性演练。

## 4. filesystem 对象迁入 MinIO

先在旧主机上冻结写入并生成不可变清单：

```bash
uv run paperforge-admin storage-manifest \
  --source-root /absolute/path/to/data/objects \
  --manifest /secure-transfer/objects-manifest.json
```

将源对象目录与清单通过加密通道传到目标机。先启动目标 PostgreSQL/MinIO 和工具镜像：

```bash
export PAPERFORGE_ENV_FILE=/etc/paperforge/paperforge.env
docker compose --env-file "$PAPERFORGE_ENV_FILE" \
  -f infra/docker-compose.prod.yml up -d --wait postgres minio
docker compose --env-file "$PAPERFORGE_ENV_FILE" \
  -f infra/docker-compose.prod.yml build storage-tools
```

以下命令保持 object key 不变，上传后回读验证，已存在且哈希相同的对象会跳过；源文件永不删除。
`--verify-only` 还要求目标 bucket 不含清单外的额外 key：

```bash
docker compose --env-file "$PAPERFORGE_ENV_FILE" \
  -f infra/docker-compose.prod.yml --profile tools run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v /secure-transfer/objects:/source:ro \
  -v /secure-transfer/objects-manifest.json:/manifest.json:ro \
  storage-tools paperforge-admin migrate-storage \
  --source-root /source --manifest /manifest.json --dry-run

# 去掉 --dry-run 执行迁移；完成后改为 --verify-only 再跑一遍
```

数据库先用 custom dump 恢复，再执行：

```bash
docker compose --env-file "$PAPERFORGE_ENV_FILE" \
  -f infra/docker-compose.prod.yml run --rm migrate alembic upgrade head
```

对比备份中的表计数和 Alembic 版本，并确认数据库引用的上传、视觉、编译日志和全部导出对象均能
读取。只有数据库和对象两侧都一致，才进入公网切换。

## 5. Tailscale 验收与公网切换

目标机先使用临时 Tailscale 名称。可用 tailnet 内的 `tailscale serve` 验证登录、项目、上传、
生成、SSE、PDF/DOCX、视觉和下载；此时不要开启公网 Funnel。临时 HTTPS 地址必须先写入
`PUBLIC_APP_URL`、`PAPERFORGE_PUBLIC_APP_URL` 与精确 `CORS_ALLOW_ORIGINS`，再重启栈。

切换窗口：

1. 等待旧机所有 `queued/running` 任务归零；停止旧 Web/API/Worker，Redis 队列不迁移。
2. 完成最后一次数据库 custom dump、对象清单与迁移，并核对表计数/Alembic/对象哈希。
3. 在旧节点关闭 Funnel；在管理台释放原节点名称，再把目标节点改为原名称。
4. 将三个公网 URL/Origin 改成最终 `https://<原名称>.<tailnet>.ts.net`，确认 SMTP 后重启。
5. 目标节点启用持久 Funnel：

```bash
sudo tailscale funnel --https=443 --bg http://127.0.0.1:3000
tailscale funnel status
```

当前 Tailscale 文档要求反向代理目标为 loopback HTTP，并说明 `--bg` 会在重启后恢复。关闭时：

```bash
sudo tailscale funnel --https=443 off
```

命令语法以 [Tailscale Funnel CLI 文档](https://tailscale.com/docs/reference/tailscale-cli/funnel) 为准。

## 6. 七天观察与回滚

旧 Mac、原始 PostgreSQL dump、filesystem 对象和校验清单至少保留七天，期间不删除旧 key。

- 目标机没有新写入：关闭新 Funnel，停止新栈，恢复旧节点名称并重新启用旧 Funnel即可。
- 目标机已经有新写入：禁止直接切回旧快照。先进入维护窗口，冻结新任务，把目标 PostgreSQL
  与 MinIO 对象反向同步并校验到旧机，再执行名称/Funnel 回切。

每次回切都要重新检查精确 Origin、Secure Cookie、SMTP、下载与 SSE；不能以关闭 PDF、DOCX、
视觉或 SSE 来换取 Focal 兼容。

## 7. CI 双基线

常规 CI 固定 Ubuntu 22.04，并验证后端、67 个前端测试、macOS Bash 3.2/Python/Node 流程和
API/Worker/Web 的 amd64/arm64 镜像构建。`Ubuntu compatibility` 工作流在 LXD 内分别启动
Focal/Jammy，使用 `security.nesting` 与 OverlayFS 所需 syscall interception 运行嵌套 Docker，
执行同套测试、Compose 全栈、备份恢复、重启恢复和端口扫描。LXD 设置依据
[官方嵌套 Docker 说明](https://documentation.ubuntu.com/lxd/latest/faq/)。

Focal 作业必须在仓库 Actions secrets 中配置 `UBUNTU_PRO_TOKEN`；该 Token 只注入一次性 LXD
实例。没有 Token 时 Focal 作业明确失败，而不是绕过 ESM 门槛。
