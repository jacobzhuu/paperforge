# Evaluator shadow evaluation v1

本轮不改变线上 `legacy`、Writer/DAG/Subagent、既有 quality gate 或 A/B/C release gate。
Shadow 只记录观察结果，没有写作、修复、发布、导出放行或切换默认配置的调用路径。

## 实现与隔离

- ARQ 的 `after_job_end` 在前台结果已保存后捕获新完成写作任务的稿件。只使用任务绑定的
  document，不用“最新文档”代替。捕获采用 PostgreSQL repeatable-read；正文、证据、输入
  hash、实现 hash、基线质量报告和 claim anchors 一起冻结到独立 `evaluator_shadow_run` 表。
- 基线必须匹配同一 document snapshot，否则标记不可比较。Shadow 不更新 GenerationJob、
  PaperDocument、PaperSection、QualityReport、原 checkpoint 或线上 LLM 账本。
- 私有对象存储路径：`projects/<project-id>/evaluator-shadow/<run-id>/`。
  保存 `input.json`、每次重复的 events/calls；现有 review-inputs artifact 保存精确提示词、
  证据和模型参数。原始 JSON、格式重试、schema 失败、耗时、token/cost 均保留。
  模型调用复用现有 shared LLM limiter，但使用独立预算和 trace。
- 后台由现有主 Worker 的 ARQ cron 消费持久化 outbox，不依赖当前 Codex 会话。短任务队列
  不启动 shadow cron。PostgreSQL 锁和 running 状态跨 Worker/蓝绿部署限制最多一个 shadow run。
  仅在没有 queued/running 前台任务时开始；每次章节调用前再次检查，有前台任务则保存进度
  并让出。已开始的单次 provider 请求不能抢占，仍可能短暂共享一个 LLM 配额；不承诺零资源竞争。
- 同一 job/version、同项目相同 snapshot 去重；已完成重复不会再调用模型。崩溃/超时标记
  interrupted，不自动重放可能已计费的请求。暂停恢复保留预算；实现 hash 变化时拒绝混用版本。
  捕获失败只写 Worker 日志，不改变前台结果。捕获窗口不是生成事务的一部分：极端进程崩溃
  或数据库故障可能漏采，不能声称全量审计覆盖。

## 有限采样和费用边界

默认启用 `EVALUATOR_SHADOW_ENABLED=true`；关闭后停止新增捕获和后台消费，不删除历史结果。
首批最多 `EVALUATOR_SHADOW_DOCUMENT_LIMIT=10` 个独立快照（配置硬上限 20），到上限即不再采集。
每稿最多 6 节，优先摘要/结论、绑定问题、数字密度和 claim 覆盖；完整正文仍保存供复核。
每节 `EVALUATOR_SHADOW_REPEATS=3` 次，使用完全相同的冻结输入、独立模型响应。
这是有风险偏重的代表性样本，不是随机总体抽样；表格无独立 claim 的部分不能声称已逐格核验。

每稿最多 18 次章节评估；包含格式/运行时重试的独立调用预算 72，保守预留 token 预算
2,000,000（按请求字节上界计数，不是账单 token），预算时限 1800 秒。ARQ 单次执行也限制
1800 秒。不因失败增加重复次数，不执行写作、检索、修复或旧 benchmark。暂停等待也受持久化
预算时限约束，长时间等待可能导致余下评估 unassessed，不能据此放宽可信标准。

## 统计定义与可信门槛

独立协议 `shadow-trust-v1`，不是对原 A/B/C gate 的事后修改。全部达到后只输出
`recommend_abc_review=true`，含义是可以建议审阅新实验协议；`automatic_actions` 永远 false。

| 项目 | 门槛 |
|---|---|
| 样本 | 至少 10 份稿件、3 个项目、20 个章节，每节 3 次完整重复 |
| verdict 一致性 | ≥95%；结构化维度和逐 claim 状态完全相同，重复失败不算一致 |
| 评估失败率 | ≤2%；无效输出、未完成评估、异常均保留，缺失重复另阻止达标 |
| 来源疑点 | 模型 uncertain 所在评估比例 ≤5%；这是疑点代理量，不冒充独立确认的来源矛盾 |
| 语义金标覆盖 | 独立标注至少 50 个 violation、50 个 clean claim，不以重复调用增加金标样本数 |
| 语义 FP/FN | 各 ≤5%，门槛采用将未评估计入不利情况的上界；实测 FP/FN 与 unknown 分列 |
| 确定性金标 | 至少 20 个确定性错误正例，零漏检、无未完成项 |

金标绑定 `input_hash + claim_id + layer`，必须提供 reviewer、rationale、可在冻结 claim/证据中
核验的 source_quote。同层重复标注拒绝；ambiguous 独立统计，不强行当 clean/violation。
现有检查不是 gold，与其不同只能叫分歧。无金标时 FP/FN 为 null，绝不默认为零。
未有足够错误正例、完整重复或跨项目覆盖时保持不达标，不伪造样本或自动扩大付费批次。

确定性结果（绑定错误、明确表头角色错误、真正 placeholder）与 LLM 原始判断分别统计。
报告按相同 claim 定位两者分歧；LLM 即使声明 acceptable 也不能清除确定性记录。
与线上检查的比较保留同快照 blockers/anchors，按完全相同的 claim 文本列出状态差异；
绑定资格和语义蕴含本来就不同，差异本身不计 FP/FN。全文语义实验仍未接入线上门禁。

## 一次性读取结果

运行环境有对应数据库配置时：

```bash
uv run python -m evals.evaluator_shadow.report --output .paperforge/shadow-review-001
# 完成独立标注后，重新输出到新目录，历史文件不覆盖：
uv run python -m evals.evaluator_shadow.report --gold /path/to/gold.json \
  --output .paperforge/shadow-review-002
```

线上容器自带同一只读命令，无需复制凭据或安装 eval 代码：

```bash
source .paperforge/deploy.env
docker exec "${PAPERFORGE_DEPLOY_PROJECT}-worker-1" python -m paperforge_worker.shadow_report \
  --output /tmp/shadow-review-001
mkdir -p .paperforge/shadow-review-001
docker exec "${PAPERFORGE_DEPLOY_PROJECT}-worker-1" cat /tmp/shadow-review-001/runs.json \
  > .paperforge/shadow-review-001/runs.json
docker exec "${PAPERFORGE_DEPLOY_PROJECT}-worker-1" cat /tmp/shadow-review-001/report.json \
  > .paperforge/shadow-review-001/report.json
```

输出 `runs.json`（冻结输入和全部结果）及 `report.json`（一致性、失败率、歧义、分层 confusion、
费用、线上差异和每项门槛）。命令只读取一次数据库，不 tail、不轮询、不调用模型。
数据含私有稿件和证据，应按项目数据管理。提供金标时先将 JSON 放入容器可读路径，再加 `--gold`。

金标格式示例（标注者可为明确声明的独立 AI 审阅，不冒充人工）：

```json
[{
  "input_hash": "从 runs.json 复制的输入 hash",
  "claim_id": "s1:0:0:0",
  "layer": "llm",
  "label": "violation",
  "reviewer": "independent-ai-review/version",
  "rationale": "具体核查理由",
  "source_quote": "冻结证据中的连续原文"
}]
```

`layer` 为 `llm` 或 `deterministic`；`label` 为 `violation`、`clean` 或 `ambiguous`。
本轮只验证实现、隔离与统计逻辑，不把 mock 测试称作真实模型质量成绩，不主动启动旧稿付费重测
或新 A/B/C，也不等待线上样本积累。部署验收与测试记录位于 `.paperforge/evaluator-shadow-20260917/`。


## 本次交付验收

- 完整回归：755 passed、2 skipped；最终专项回归：56 passed；Ruff 与 diff whitespace 检查通过。
- 新部署：`paperforge-deploy-20260917-034754`，公网入口端口 3006。Funnel 指向和公网 `/login`
  字节一致性验证通过；两个主 Worker 的 shadow 配置和代码 hash 均已核对。
- 线上 writer=legacy、frame concurrency=1；原 Evaluator/quality 模块 hash 与上一部署一致。
  旧部署 `paperforge-deploy-20260917-032301` 保留，未停止在途任务。
- 一次性初始结果保存在 `.paperforge/evaluator-shadow-20260917/initial-report/`。
  未启动旧稿付费重测或新 A/B/C；不等待样本积累，不持续监控。
