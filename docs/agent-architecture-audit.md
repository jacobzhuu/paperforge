# PaperForge Agent 架构审计与实现

> 历史审计。2026-09-18 后的 LangGraph、混合检索、OTLP 能力与边界以 [Agent 平台升级](agent-platform-upgrade.md) 为准；本页旧结论不代表当前运行配置。

本审计以实际代码为准，覆盖 API/Worker 入口、模型运行时、数据库仓储、证据与写作管线、人工交互和部署脚本。项目的准确定位是：**证据驱动、固定主流程内具备有界自适应修复能力的科研写作 Agent**。

## 当前能力与边界

Next.js 调用 FastAPI；ARQ/Redis 分发任务；Worker 调用领域管线；PostgreSQL 是任务、事件、证据与正文的事实源；对象存储保存全文和导出件，texd/visuald 提供渲染。SSE 读取数据库事件，Redis Pub/Sub 只负责唤醒。

主链路为范围规划、问题分解、检索、筛选、全文获取、证据抽取、问题—证据矩阵、综合、大纲、写作、质量检查与导出。它没有由模型任意重排全部阶段。

| 能力 | 实际实现 | 不能声称的能力 |
| --- | --- | --- |
| Agentic workflow | 写前补证、写后质量收敛、语义评审修复 | 开放式全流程自主 Agent |
| Planning/replanning | 问题/查询/大纲规划；依据评审和尝试历史选择修复路线 | 学习型规划器、任意工具发现 |
| Tool use | 学术检索、解析、证据抽取、写作、编译等 Python 领域接口 | MCP 或模型原生 tool-call 协议 |
| 模型路由 | `llm_runtime/config.py` 按角色选模型、思考与重试策略 | 在线学习的模型路由 |
| Retry/repair | 网络/截断重试、证据补充、局部重写、质量回滚 | 无界自我改进 |
| Evidence grounding | 来源、等级、定位、句级证据、蕴含核验、用户素材 | 零幻觉保证 |
| Human-in-the-loop | 锁定问题、绑定任务、编辑正文、确认 PDF、审核插图、暂停续跑 | 通用规划审批平台 |

Working memory 已有滚动摘要、术语表、核验缓存；semantic memory 的基础是领域任务词表、研究问题、证据关系与综合结果；artifact memory 包括来源文件、卡片、正文 IR 和导出产物。任务事件原本提供部分 episodic memory，本次将语义修复的决策与执行状态也持久化，并真正用于恢复。没有跨项目自动经验学习或向量记忆库。

## 本次实现

```mermaid
flowchart TD
    E[模型语义评审 + 当前证据状态] --> P[规则约束的 Planner]
    M[(PostgreSQL checkpoint: 尝试历史/预算)] --> P
    P --> X[Executor: 补证 / 重新综合 / 重写]
    X --> M
    X --> E
    E -->|通过 / 无新路线 / 预算耗尽| Q[核对正文快照并更新质量报告]
    Q -->|最新质量门通过| O[导出]
    Q -->|严格模式未通过| H[保留正文并 needs_input]
```

- `services/worker/paperforge_worker/orchestration/semantic_repair.py`：类型化状态、纯策略规划器、动作依赖和可恢复执行器。
- `services/worker/paperforge_worker/worker.py`：保留原入口和领域实现，通过 `_converge_section_semantics` 连接评审与执行；`_quality_after_semantics` 修正导出前质量一致性。
- `services/worker/paperforge_worker/pipelines/semantic_review.py`：沿用已有病因与证据库存选路规则。

Planner 不增加一次 LLM 调用。它仍在 `retrieve / resynthesize / rewrite` 中选路。Executor 将补证展开成加挂已有证据、补检索、重建矩阵三个可记录边界；综合和补证完成后都必须重写正文。原有并发与框架章节刷新策略保留。

`semantic_repair` checkpoint 采用版本 1，绑定项目和实际 document ID，记录轮数、各章节尝试路线、修复指令及哈希、评审、动作状态和简短结果。每次规划先预留一轮，再执行副作用。默认最大修复轮数仍为 2；新指令可以再次选择 rewrite，但不会重置总预算。不同文档不会继承尝试历史。

保持原事件与结果字段，新增 `semantic_repair.planned`、`action_started`、`action_completed`、`interrupted` 事件（后三者也带 `semantic_repair.` 前缀）。结果增加 `rounds_used`、`stop_reason`、`unassessed`。无新 REST API、数据库表、服务或运行时依赖。

## 恢复、质量与失败语义

正常暂停发生在动作边界时，已完成动作跳过，pending 动作继续。动作内暂停、进程崩溃、外部调用异常或完成记录未提交时，数据库可能只有 started：恢复将该动作及未执行的依赖标为 interrupted，先重新评估持久化产物，保留已花掉的轮数和路线，不盲目重放。

这不是 exactly-once：外部请求和 checkpoint 不能原子提交。恢复粒度是动作边界，不是单个 HTTP 请求或重写中的每一句。评审本身在中断后可能重做。其他质量/补证循环没有因此获得完整的内部状态恢复。旧 checkpoint 缺少新字段时按旧入口初始化。

正文修复会使数据库中的旧质量报告 stale；本次还修正 Worker 内存和 checkpoint 中可能残留的旧放行结果：语义修复之后比较报告与当前正文哈希，变化则重新评估；submission 始终按最终稿运行投稿评估。重评异常清除旧 checkpoint 放行状态，严格模式保留正文并等待处理。draft 仍允许交付带明确质量问题的草稿。

暂停/取消信号显式穿透修复异常处理。语义评审不可用记录为 unassessed，不将其当作通过，也不另加破坏既有交付策略的硬门槛。语义判断不代替引用、数字、可比性等质量门。

## 为什么没有引入更多框架

- **LangGraph：暂缓。** 现有 ARQ、PostgreSQL checkpoint、SSE 已提供基础状态编排。只有局部图迁移能明确改善恢复语义、维护成本时再做对照试验。参考[官方持久化说明](https://docs.langchain.com/oss/python/langgraph/persistence)。
- **MCP：按实际外部消费者引入。** 先有接入需求，再包装检索/证据接口；内部领域函数不需要为了协议而走网络。参考[官方架构](https://modelcontextprotocol.io/docs/learn/architecture)。
- **Multi-Agent、向量数据库、跨项目经验学习：本批不加。** 模型角色分工不是多个自主 Agent，内容缓存也不是学习。引入前需证明独立决策需求、检索收益与项目隔离策略。

## 验证与维护

```bash
# 对独立 PostgreSQL 测试库运行；夹具会清空该库的业务表。
PAPERFORGE_TEST_DATABASE_URL=postgresql+asyncpg://paperforge:paperforge@127.0.0.1:25432/paperforge_test \
  REDIS_URL=redis://127.0.0.1:26379/0 uv run pytest -q
uv run ruff check .
uv run python scripts/ontology_literal_lint.py
uv run python -m evals.agent_repair.run
```

新增测试覆盖语义重写后严格导出阻断/放行、失败重评清除旧放行、停止信号传播、真实数据库事件与续跑 checkpoint、动作中断不重放、预算继承、文档隔离和新指令重写。离线评测输出选路、动作顺序和恢复后的重复动作计数；它验证控制流，不证明模型科研判断准确率或真实成本收益。

部署执行 `./scripts/dev restart`，Funnel 目标及公开 `/login` 与新部署逐字节相同是必需验收。旧部署保留以完成在途任务。新状态是增补字段，旧任务不迁移到新 worker；不承诺回滚旧代码后仍可理解新语义修复状态。

本次验证记录：隔离 PostgreSQL + Redis 上，全量后端 **1300 passed、5 skipped**。跳过项为 3 个需要 `PAPERFORGE_TEST_TEXD_URL` 的真实 texd 回归和 2 个需要本机 pandoc 的导出测试；新增恢复与质量一致性测试全部实际执行。Ruff、领域词表检查、4 个离线策略场景均通过。未运行真实 LLM 质量提升对照实验。

上线排查还修复了 `scripts/dev` 的 Tailscale socket 发现：先匹配实际进程名，避免把查询命令自身误认成 tailscaled。相关部署脚本测试 **25 passed**，覆盖误匹配、两种 socket 参数写法与默认路径。

## 可用于简历的技术描述

- 构建基于 FastAPI、ARQ、PostgreSQL 的科研 Agent 工作流，支持任务 checkpoint、SSE 事件重放和人工干预。
- 实现模型评审与领域规则结合的有界 replanning，按证据缺口选择补检索、重新综合或局部重写。
- 实现持久化修复决策、动作恢复与预算继承，通过故障注入和真实数据库契约测试验证。
- 构建带来源定位的证据链、句级引用核验和正文快照绑定的质量门，避免旧质量结果放行改写后稿件。

不填写未经真实对照实验测量的准确率提升或节省比例，不声称使用未引入的 LangGraph、MCP 或 Multi-Agent。
