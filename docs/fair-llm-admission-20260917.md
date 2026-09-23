# 公平 LLM 准入（2026-09-17）

生产保留 dag_parallel。Writer 的本地窗口从 2 调为安全上限 4，实际 provider 并发由共享
Redis 准入动态分配，全局上限仍是 6。没有修改 DAG 依赖、分层屏障、提示词、证据绑定、
Integration、质量门禁或原 A/B/C release gate，也不据此声称真实模型 benchmark 已通过。

## 分配规则

沿用 provider base URL + 凭据摘要分组及 Redis DB 0 的跨 Worker/蓝绿共享租约集合。
Lua 将登记、过期清理、选择和授权作为一次原子操作：

1. 有等待的前台请求时不授权 shadow/background 请求。
2. 前台任务中，优先选择当前 provider 槽位占用最少的任务；占用相同则优先最久未获授权者。
3. 同一任务内部 FIFO。任务身份由 JobContext 的 GenerationJob ID 覆盖注入，调用侧 metadata
   不能伪造身份或优先级；无 GenerationJob 的 JobContext 视为低优先级后台任务。
4. 已发送的请求不抢占。新任务加入后通过后续释放的槽位重新平衡，不取消已付费调用。

单篇 Writer 有充分 ready 节点时可用到 4；两篇有充分排队需求时可在请求完成后趋近 3/3；
多任务竞争时按占用量与历史授权轮转。这里公平的是 provider 槽位，不是 token 数或请求数。
有限的本地线程窗口、DAG 宽度、长短请求、RPM/TPM 等仍会影响实际分配，不能保证每个瞬间
严格均分或所有稿件都提速。其他阶段保留各自已有本地并发上限，同样参与按任务公平准入。

## 可靠性与兼容

- 继续使用 120 秒租约、20 秒续租；公平归属与授权序列随活动租约续期，避免长请求后序列重置。
- 等待记录定期刷新 TTL。正常退出、超时和收到异步取消时撤销等待；进程硬退出靠 TTL 清理。
- 异步取消只取消后续准入。已经发出的 HTTP 调用继续持有 provider 槽位并记录结果，返回后释放。
  execution guard 在调用排空期间继续续租数据库执行权，记账完成后再释放，避免迟到响应失去提交资格。
- Redis 故障不绕过限额。现有全局并发、账户/模型 RPM/TPM 和重试预算继续生效。
- 旧 Worker 无任务身份的租约仍占全局容量，绝不提前删除。升级期间公平排序只约束启用新
  开关的 Worker；旧调用与未启用开关的调用仍按旧方式竞争，但总租约上限保持约束。
- 无身份的同步调用使用 anonymous 前台组。队列记录和归属表均有回收机制，不积累永久队列。

## Trace

现有 LLM call metadata 增加 `scheduler_job_id`、`scheduler_priority` 和
`provider_admission_events`。事件包括 queued、granted、released/withdrawn，以及 HTTP attempt
开始/结束；记录时间戳、授权时任务/全局槽位占用、排队请求数、等待时长与授权理由。
保留已有 local_queue_wait_ms、provider_slot_wait_ms、rate_limit_wait_ms。

`writing_dag.wave` 增加 ready_nodes 和 local_window_limit。本地窗口包含等待 provider 的任务，
不能将其当成实际 HTTP 并发；provider 槽位也可能包含 RPM/TPM 等待，HTTP attempt 时间另列。
这些字段进入已有日志与 shadow 私有调用记录，不另建质量门禁，也不改写原稿/评审结果。

## 配置、回退与验证

生产配置采用：

```dotenv
WRITER_EXECUTION_MODE=dag_parallel
WRITER_CONCURRENCY=4
LLM_FAIR_ADMISSION_ENABLED=true
# LLM_GLOBAL_CONCURRENCY 保持部署现有值 6；frame concurrency 保持 1。
```

代码默认保留公平开关关闭、Writer 窗口 2，便于兼容现有开发环境。
回退时在当前生产 env 中设置 `LLM_FAIR_ADMISSION_ENABLED=false`、`WRITER_CONCURRENCY=2`，
再执行 `./scripts/dev restart`；不修改已持久化的任务模式，也不终止旧部署。

验证使用隔离真实 Redis（26379）和 PostgreSQL（25432），不调用付费模型：

- 原子公平选择、FIFO、新任务加入、超过容量的任务轮转、shadow 优先级。
- 多进程并发始终不超过全局上限，单 Writer 模拟窗口不超过 4。
- 超时/取消清理、过期租约、旧 Worker 释放、长请求续租与序列寿命、Redis 断连关闭准入。
- 已付费调用取消后仍持有租约、保存响应与调用记录。
- 1/4 本地窗口的 Writer 冻结上下文一致；原 DAG 依赖、恢复、顺序提交、shadow 隔离回归。

具体测试及蓝绿部署验收记录保存在 `.paperforge/fair-admission-20260917/`。
本轮不启动付费 A/B/C、不持续监控 benchmark，真实时延与吞吐收益留待后续对照评估。

### 本轮验收结果

- LLM runtime、完整 Worker、API dispatch 回归：822 passed、2 skipped（177.67 秒）。
- 本轮修改文件 Ruff 与 `git diff --check` 通过。
- 已执行 `./scripts/dev restart`，部署 `paperforge-deploy-20260917-130450`，端口 3009。
- Funnel 指向检查、公网与本地 `/login` 逐字节一致性检查通过。
- 两个主 Worker 与短任务 Worker 的 mode=dag_parallel、Writer 上限=4、frame=1、
  fair=true、global limit=6 均已核实；八个相关源码模块的哈希与工作区一致。
- 旧部署 `paperforge-deploy-20260917-120807` 保留；未启动付费 benchmark。
- 机器可读验收结果：`.paperforge/fair-admission-20260917/acceptance.json`。
