# Evaluator 输入修复与框架章节并行（2026-09-17）

本轮不重新生成旧九稿、不调用付费模型、不改变原 release gate，也不将线上默认改为 DAG。

## 实现

- `section-review-v2` 从 PaperIR 的句级 claim/citation/evidence/source binding 构造输入。实际引用证据从当前项目全集解析，不受子问题池、前 14 条或每条 700 字限制；同文不同 ID 不合并。子问题证据库存与 repair 路由保持独立。
- 未知 Evidence、错配 work、未知 citation、无 Evidence ID 的 citation 和无效 source_ref 显式记录，不能用其他材料伪造绑定。质量门中原有 grade、数值定位、可比性等判断不变。
- 输入保留完整正文、绑定、证据内容哈希及选择原因。JSON 输入超过 64,000 字符时不调用模型，记为 `unassessed/input_budget_exceeded`，不静默裁剪。该上限是输入保护，不是新的质量放行标准。
- 对象存储保存完整输入和实际 system/user prompt、角色及采样参数。`review.input`、`review.result` 与 LLM ledger 通过输入哈希关联，artifact 单独按其内容哈希寻址。保存失败不能被当作成功评审。
- 普通“相对效果量仍待补充”“相关数值待补充”不再触发 placeholder；显式 TODO/TBD、方括号占位、孤立“待补充”及既有失败 skeleton 模板仍保留检查。按块判断，避免全文展平丢失边界。

框架首轮生成改为：正文恢复 → Integration → 串行 polish → 冻结正文及上下文 → 摘要/引言/结论受限调度 → 串行恢复、组装及既有全局 QA。首轮框架循环原本并不 `register`；上一轮报告中的相反描述已更正，不改历史数据及结论。

`WRITER_FRAME_CONCURRENCY` 新参数默认 **1**，范围 1–2；仅 `dag_parallel` 新任务可显式选择 2，运行模式和宽度进入 checkpoint，旧任务恢复默认仍为 1。同一快照的框架上下文存入 checkpoint，恢复时复用。未知框架任务保守串行；显式 frame 依赖先验证再调度。

任务隔离上下文、局部结果与事件；模型共用现有 limiter。结果按大纲拓扑顺序落库；提交前检查来源和正文快照，并保留章节乐观锁。停止后排空已准入任务，正常失败不阻止其他已付费结果保存；遇到正文/人工编辑冲突时暂停，不覆盖用户内容。

polish 仍按章节串行，提示与输出协议不变。新增每次候选的 accepted/changed/reason 和阶段计数；原 `polished_count` 仍表示尝试数。框架 trace 分别记录准入等待、生成 span、顺序提交等待、持久化耗时；provider 等待沿用 LLM ledger，未测量时间不假称数据库耗时。

## 零付费审阅

执行入口（输出目录必须是新目录）：

```bash
uv run python -m evals.agent_writing.replay_evaluator \
  --source .paperforge/agent-benchmark-20260916 \
  --output .paperforge/evaluator-review-20260917-final
```

脚本只读取现有 JSON/稿件，校验旧 `cold.json` 与九稿的 SHA256，不查询数据库、不调用模型。输入覆盖统计按“每个章节内证据 ID 去重，再跨章节累计”。54 个 section reviews 的有效绑定可见证据由 433 增至 827；保留 2 处绑定缺口。两稿普通缺口叙述的 placeholder 误报消除。

`audit.json` 中 placeholder-only hard projection 只重新执行本轮改变的 placeholder 规则；其他历史 hard violation 值保留，**不是完整的新 Evaluator 判定**。旧语义结果仅供参考，新语义结果全部为未评审。完整匿名稿件、证据材料和未填写评分表复制到 `blind-packet/`；条件映射继续留在原审阅目录，不进入盲审包。

新语义评审需要真实模型调用，人工盲审需要审稿人填写。二者完成前不得宣称质量验证通过。

## 新 A/B/C：frames-v2（未启动）

| 条件 | 正文模式 | 框架并发 | 评审器 |
|---|---|---|---|
| A_legacy | legacy | 1 | section-review-v2 |
| B_dag_frames_serial | dag_parallel，width=2 | 1 | section-review-v2 |
| C_dag_frames_parallel | dag_parallel，width=2 | 2 | section-review-v2 |

A→B 衡量现有正文 DAG 相对 legacy 的整体收益；B→C 只衡量新增框架并行的边际收益。三组同 fixture、角色模型配置、缓存策略和 polish；至少三轮、ABC/BCA/CAB 轮转，评估数据库必须为独立 `_eval` 数据库。真实写作不触发语义 repair，评审在写作计时结束后执行，保持既定计时边界。

显式付费入口（**本轮不执行**）：

```bash
PAPERFORGE_EVAL_DATABASE_URL='<isolated *_eval URL>' \
uv run python -m evals.agent_writing.run --protocol frames-v2 \
  --project-id '<fixture project UUID>' --repeats 3 --cache-mode cold \
  --output '<new directory>/cold.json' --live
```

已有输出拒绝覆盖。旧 `report.py` 与旧实验保持原样；新 `frames_protocol.py` 验证三组配置及统一 Evaluator 版本，并复用原 gate：B→C 写作中位耗时下降 ≥15%、Token 增长 ≤10%；A→B 和 B→C 每对三个盲审分项降幅均 ≤0.2，hard violation Counter 不增加，全部运行完成，至少三对，人工审阅完成。A→C 总收益只作诊断，不能救回失败的 B→C gate。

按照旧框架耗时估算，新框架并行的边际收益可能不足 15%；必须如实报告失败，不调整门槛。

## 验证与部署

回归覆盖长证据、后置证据、ID/归属错误、短句绑定、source_ref、输入稳定性、完整 prompt artifact、超预算不调用模型、placeholder 正反例、框架输入等价与实际重叠、乱序提交、停止排空/恢复、正文编辑冲突、polish guard 原语义，以及新实验不能套用旧条件或降低 gate。

相关测试和静态检查后使用 `./scripts/dev restart` 蓝绿部署。线上仍使用 `legacy` 与框架并发 1；Funnel 目标及公网 `/login` 字节一致性为验收项。保留旧部署以承接可能的在途任务。

### 本轮验收记录

- Worker、DB、LLM runtime、benchmark 协议和部署脚本相关回归：882 passed，5 skipped；最后输入 artifact 与冻结上下文调整后的定向复核：86 passed。Ruff 检查与 14 个改动 Python 文件的格式检查通过。
- 最终离线产物：`.paperforge/evaluator-review-20260917-final/`；54 个输入均未超过输入保护上限，付费调用数 0。原九稿与 `cold.json` 的 SHA256 校验通过。
- 已部署 `paperforge-deploy-20260917-020521`，端口 3004、Redis DB 0。部署日志：`.paperforge/evaluator-frames-deploy-20260917.log`。
- Funnel 目标检查及公网 `/login` 字节一致性通过，页面 SHA256：`7c07a5eed0c09e3f5e2bcc2c3bae1cd1d0a1ea242a07f77e90f9e3299ae297f8`。
- 新 worker 实际读取配置：`writer_execution_mode=legacy`、`writer_frame_concurrency=1`、`review_version=section-review-v2`。7 个相关源文件与工作区逐字节哈希相同，清单存于离线目录的 `deployed-source-sha256.json`。
- 前一部署 `paperforge-deploy-20260916-220357` 的 worker/worker-short 均保留且健康。没有启动新的付费评审或写作 benchmark。
