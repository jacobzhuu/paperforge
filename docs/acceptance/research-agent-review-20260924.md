# Research Agent 静态审查与 Pi 对照（2026-09-24）

审阅基线：`839578c`，分支 `feat/glm-5.3-flash-migration`。仅使用独立测试库和离线模型替身；本次没有访问或修改生产数据，也没有部署。本报告记录的是当前工作区修复后的验证状态，公网仍运行此前发布版本。

## 已核实问题与修复

1. **Citation shadow 超时与迟到写回。** 基线使用 `created_at` 判断 `running` 是否超过一小时。排队时间长、刚开始执行的请求会被误判；执行晚于超时标记完成时，原代码又能无条件覆盖 `interrupted`。新增迁移 `0036_shadow_attempt` 记录 `started_at` 和随机 `attempt_id`。超时仅依据执行开始时间；没有可信开始时间的旧 `running` 行标为费用未知的 `interrupted`。新 worker 的结果写回必须同时匹配 `running` 和当前 attempt；迁移还加了数据库触发器，防止蓝绿发布期间旧 worker 的无条件迟到写回改变 `interrupted`。中断行不会自动重新入队或重跑。
2. **提案派生产物完整性。** 基线只验证派生素材行存在且属于本项目，未核对表格解析内容、图像字节或对象键。现在提案保存表格和图的内容哈希、类型及对象键；图和复现包采用带内容哈希的对象键。预览图和提交确认时重新读取图像并校验 SHA-256，提交时对派生素材加行锁并核对解析内容、类型、对象键和图像字节。缺失或变化返回 409，不创建新文稿。恢复时复用已保存计算结果之前检查图像；已存在的派生行必须与预期内容一致。旧版缺少派生哈希的提案会拒绝确认，需重新分析。
3. **验收链接。** `docs/agent-platform-upgrade.md` 原来指向未纳入 Git 的 `acceptance/agent-platform-20260918.md`；本地虽有该文件，GitHub 上链接失效。链接已改为已提交的 `research-agent-20260923.md`。本次没有上传本地未跟踪的历史资料、私有验收目录或会话令牌。

## 默认引擎与续跑

收尾核对发现，基线中的前端初值和 API 在省略 `engine` 时都默认为 Pi。两处现均改为 `deterministic`；前端把 Pi 标为“实验功能，使用模型额度”，用户必须主动选择。直接计算任务不绑定规划模型。创建任务时仍将引擎写入 `ResearchAnalysis.input_json`。人工澄清和暂停/失败后的续跑复用该输入，不接收新的引擎选择；worker 依照保存的 `engine` 分流，不根据新的默认值改写旧任务。累计 `model_calls` 上限 20 次、累计执行时间上限 600 秒沿用原有记录。

本轮模型替身回归确认：省略引擎的任务完成统计，Pi 入口未被调用，模型调用账本 0 条、累计回合 0；显式 Pi 任务在暂停后续跑，仍走 Pi 入口，累计回合从 1 增至 2，累计执行时间没有清零。另有真实 Pi Node 进程配模型替身的原有工具循环回归；上述测试均无付费调用。

## 本轮离线复测：Pi 与 deterministic 对照

运行 `uv run python -m evals.research_agent.compare_offline`。两臂使用相同 CSV、确认后的字段、分组和单位，调用相同受限计算进程。Pi 臂启动真实固定版本的 Node/Pi Agent Core，但模型响应由脚本替身给出；每例执行 `read_table_summary`、`compute_descriptive`、`read_results`、`propose_patch`，共 5 次模型替身响应。数字正确性比较完整 `records` 和统计口径 `policy`，包括缺失值与单样本标准差。以下为一次本机运行的端到端墙钟时间，受进程启动和机器负载影响，不是稳定性能基准。

| 数据例 | 数字与口径一致 | deterministic 成功 / 耗时 | Pi + 模型替身成功 / 耗时 | 模型费用 |
| --- | --- | ---: | ---: | --- |
| 两组均值 | 是 | 1/1，1.918 秒 | 1/1，3.897 秒 | 两臂均 0（离线） |
| 含缺失值 | 是 | 1/1，1.986 秒 | 1/1，3.959 秒 | 两臂均 0（离线） |
| 每组单样本 | 是 | 1/1，1.802 秒 | 1/1，3.747 秒 | 两臂均 0（离线） |

本轮离线任务成功率两臂均为 3/3，仅代表三个合成样例。Pi 替身路径约多 1.9–2.0 秒，主要多出一次读表计算和 Node 工具循环；脚本响应不能证明真实模型会稳定选对工具。

## 历史真实模型记录

此前 [2026-09-23 验收](research-agent-20260923.md)留下独立的**真实模型**证据：同一组 `group,time_ms` 合成输入，deterministic 与 Pi 均得到组均值 2、3；Pi 的一次恢复任务成功，5 次真实模型调用，4251 输入 tokens、1029 输出 tokens，配置单价估算费用 0.003141（币种和供应商账单未在该记录中核对）。旧记录没有可比的两臂耗时，也没有足够样本估计真实模型成功率。本轮没有新增付费调用，因为没有针对这次对照的明确预算授权。

**默认启用建议：暂不建议把 Pi 设为默认。** 对目前受控描述统计，固定计算器给出相同数值，无模型费用，离线样例更快。Pi 可继续作为用户主动选择的实验选项；评估默认切换需要在明确预算下，预先定义数据集、计时边界、失败及费用统计，并测试真实模型的工具选择、恢复和研究质量。现有证据不支持“Pi 更准确”或“Pi 更稳定”的结论。

## 回归与边界

- `PAPERFORGE_TEST_DATABASE_URL=... uv run pytest -q services/worker/tests/test_citation_shadow.py services/worker/tests/test_research_runtime.py services/worker/tests/test_quality_pipeline.py`：42 passed。新增 PostgreSQL 用例覆盖很早入队但刚执行、不明开始时间、超时和旧 attempt 迟到写回。
- `PAPERFORGE_TEST_DATABASE_URL=... uv run pytest -q services/api/tests/test_research_api.py`：本轮完整研究接口回归 15 passed。覆盖默认引擎零模型调用、显式 Pi 暂停续跑及累计预算、预览后表格内容/图元数据/图字节变化、派生行或图对象删除、恢复时正常复用及被篡改图像拒绝、重复确认、文稿冲突和跨项目访问。最后调整“直接计算不绑定模型”后，受影响的两项 API 用例再次运行：2 passed。
- `cd apps/web && npm test -- --run tests/research-panel.test.tsx`：3 passed，包括前端默认直接计算和主动选择 Pi 的请求值；`npm run lint`（TypeScript）通过。
- 独立空库 `DATABASE_URL=... uv run alembic upgrade head` 与 `uv run alembic check`：升至 `0036_shadow_attempt`，无迁移漂移。
- 独立升级库先 `alembic upgrade 0035_research_analysis`，插入一条旧版 `running` shadow，再 `alembic upgrade head`：状态为 `interrupted:execution_uncertain`。另一独立迁移库对中断行执行旧 worker 风格的无条件 `UPDATE status='completed'`，数据库保留 `interrupted` 与原错误结果。两项均通过；未触碰生产库。
- `uv run ruff check packages/db/db/models/research.py packages/db/migrations/versions/0036_shadow_attempt.py services/worker/paperforge_worker/citation_shadow.py services/worker/paperforge_worker/orchestration/research_graph.py services/api/paperforge_api/routers/research.py services/api/tests/test_research_api.py services/worker/tests/test_citation_shadow.py evals/research_agent/compare_offline.py` 与 `git diff --check`：通过。上述文档的相对 Markdown 链接逐个检查，通过。

## 未验证项与证据边界

未验证：生产环境真实模型的多次任务成功率、真实模型与 deterministic 的可比耗时、供应商账单费用，以及私有对象存储面对特权外部写入的强制不可变性。提交时哈希校验能识别已发生的对象变化，但对象存储本身未开启 WORM。离线模型替身证明工具路径和数值口径，不代表真实模型稳定性；工程验收不等同于科研质量提升。迁移和代码尚未部署，因此不声称公网已运行此修复。
