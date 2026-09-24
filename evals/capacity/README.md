# 可重复容量与恢复验证

这些脚本只允许固定的本机隔离端口/数据库，不连接公共服务，不继承付费模型凭证。须在仓库根目录使用已同步的 `.venv`，有 Docker、空闲端口 25432/26379/29000/28080–28084。结果是基础设施 fixture，不是完整 Agent 基准。详细结论见 [容量审阅](../../docs/capacity-audit.md)。

```bash
./scripts/capacity init
# 容器版本使用已构建 paperforge-api:capacity、paperforge-worker:capacity；见脚本开头。
./scripts/capacity-containers start
./scripts/capacity run --seconds 120 --active 30 --jobs 10 --output evals/capacity/results/limited-30.json
./scripts/capacity run --seconds 120 --active 30 --jobs 20 --streams-per-job 5 --output evals/capacity/results/limited-100-sse.json
.venv/bin/python evals/capacity/backup.py
```

备份脚本要求没有 queued/running fixture，创建独立恢复库与桶，保留恢复产物供检查。它是静止数据恢复校验；生产一致备份需要停止新写入并排空任务、协调数据库与对象存储，不应直接把本脚本用于生产。

硬杀测试需使用可验证 PID 的宿主进程 fixture，不能与容器版同时占用端口：

```bash
# 先确认隔离任务已结束；只停止 pf-capacity-* 测试进程。
./scripts/capacity-containers stop
./scripts/capacity start
./scripts/capacity faults --help
./scripts/capacity faults --output evals/capacity/results/hard-kill.json
./scripts/capacity stop
```

`faults.py` 配置原任务 1800 秒、约 5 秒后 SIGKILL，等真实租约过期并显式 resume；恢复 fixture 改为 10 秒。不能称为“已跑完 30 分钟长任务故障恢复”。渲染脚本 `renderers.py` 使用独立 texd/visuald 28083/28084，各容器限制 1 CPU/1 GiB，执行小文档/简单图表 10 请求，包含真实 503 背压处理。

更长的 fixture soak 可使用 `--seconds 28800 --jobs 50 --submit-interval 576`；设置 `CAPACITY_JOB_SECONDS=1800` 后重新启动 fixture Worker 才会改变执行时间。到达时间加任务执行时间可能超过测试窗口，需扩展窗口或 `--allow-incomplete`（后者不能当作全部完成验收）。本次**未执行** 8 小时 soak、真实付费模型容量试验或全部依赖故障矩阵。

原始 JSON 记录请求延迟、状态、队列等待、完成数、SSE 错误、资源配置。`observed_completions_per_hour` 只是短 fixture 在观察窗口内的算术外推，禁止作为真实 Agent 产能表述。重跑会新增隔离数据；比较时记录已有数据规模、镜像 ID、CPU/内存限额和运行时长。

镜像构建与独立渲染服务的准备命令（不要对公共容器执行这些命令）：

```bash
docker build --network host -f services/Dockerfile --target api -t paperforge-api:capacity .
docker build --network host -f services/Dockerfile --target worker -t paperforge-worker:capacity .
docker build --network host -f services/texd/Dockerfile -t paperforge-texd:capacity .
docker build --network host -f services/visuald/Dockerfile -t paperforge-visuald:capacity .
docker run -d --name pf-capacity-texd --cpus 1 --memory 1g --read-only --tmpfs /tmp \
  -v pf-capacity-tex-cache:/root/.cache/Tectonic -v pf-capacity-font-cache:/var/cache/fontconfig \
  -p 127.0.0.1:28083:8081 paperforge-texd:capacity
docker run -d --name pf-capacity-visuald --cpus 1 --memory 1g --read-only --tmpfs /tmp \
  -p 127.0.0.1:28084:8082 paperforge-visuald:capacity
.venv/bin/python evals/capacity/renderers.py
```

先构建 API/Worker 镜像，再执行上面的 `capacity-containers start`。容器版默认创建 API、Worker 和 gateway；渲染服务独立测试。`capacity init` 不清空已有状态，所有恢复库与桶也都保留；持续运行时需要管理这些测试数据的磁盘占用。硬杀脚本只操作经过命令行核对的测试 PID。
