# Evaluator 可信度修复（2026-09-17）

本轮只修复现有 Evaluator 的评审契约、绑定核查和可追踪性；不改写作架构、不运行写作
A/B/C、不修改原有数值 gate、不切换 legacy 默认。实现与回归检查已完成，但真实模型仍有
不稳定判断，因此不能宣布“语义质量已全面验证通过”，更不能据此放行 dag_parallel。

## 线上最小修复

- 继续使用完整 claim/citation/evidence binding 输入，不恢复前 14 条 Evidence 截断。
  `section-review-v3` 为每个 claim 要求恰好一条核查结果，引用只能来自该 claim 的绑定。
- 明确作者自报、表内排序推理、缺口声明与无依据断言的边界。缺值不能外推，实验设置、
  数值方向、教师/学生必须核对；普通“待补证据”说明不按 TODO 处理。原有 placeholder
  检测回归同时验证真正占位符仍被拦截。
- 每条 unsupported/contradicted 都进入失败项，不能被整体 matched 或空失败列表覆盖。
  原始模型理由保留；存在确定性或逐条失败时，展示结论明确为未通过。
- 对明确、等宽 teacher/student 表头添加保守角色核查：只核对独占角色的精确模型名，
  不猜测模糊表格，不硬编码 CFD/ResNet 等 benchmark 名称。不宣称这是通用表格理解器。
- 覆盖缺失、外来证据、格式无效、uncertain 均为 unassessed，不伪装成通过。仅 invalid JSON
  最多一次格式重试，不对语义失败反复抽样直到通过。逐条核查输出预算为 8192；独立证据
  比较维持原有 2000 预算。
- 保存完整输入/提示词 artifact、输入 hash、raw model output、格式尝试次数及验证失败原因；
  复用原有 JobContext/LLM 调用日志。评审版本加入 repair checkpoint scope，旧 v2 状态不冒充
  新评审；共享核查提示词也参与既有 claim verifier 的缓存指纹。

相关代码：`pipelines/review_inputs.py`、`review_contract.py`、`semantic_review.py`、
`quality.py` 和 worker 的 repair scope。未修改 SectionVerdict.acceptable 的既定判据。

## 已完成验证与不能隐去的失败

所有模型验证均为固定文稿/证据的 Evaluator replay，不是重跑写作 benchmark。所有失败保留于
`.paperforge/evaluator-trust-validation-20260917/`，原 cold.json 和九份原稿保持不变。

| 验证 | 结果与解释 | Token | 已知 cost* |
|---|---|---:|---:|
| 首次 12 个定向案例 | 原整章口径 10/12；漏检教师/学生倒置，一次无效 JSON | 32,059 | 0.0174296 |
| 后续 12 个匿名标签案例 | 原整章口径 8/12；倒置已拦截；1 次字段 schema 无效、1 次 uncertain | 32,709 | 0.0180566 |
| 3 个完整章节 | 2 个完成评估，1 个含 uncertain，未作为通过 | 58,386 | 0.0346974 |
| 9 份全文实验 | 0/9 满足精确引文定位契约，全部 unassessed | 673,601 | 0.2912224 |

*沿用当前调用日志的计价配置和单位；上述调用均已计价，不宣称经过账单对账。

后续 12 例中，正常缺口声明和修正后的单句结果被整章 synthesis/充分性判据拒绝，
但其 claim_checks 分别为 gap/supported。原 harness 把整章验收混同于事实正确性，现已
分开统计；历史结果不覆盖，也不据此更改线上验收条件。两个 CUB shot 归属案例的绑定
摘要跨片段，未明确重述 CUB 的 shot 映射，不能充当无歧义金标：一个被模型判 uncertain，
另一个判 supported，仍需完整原表才能裁定。其余 10 个标准较明确案例中，9 个逐条核查
符合预期，1 个因 schema 无效未完成；这不是独立测得的 90% 准确率。

三个完整章节验证了真实上下文下逐条输出与拦截路径。作者自报 HiCA 与有依据的排序推理
能获得对应 supported/inference 判断，但同章其他论断仍可能被拒绝；不能把单个误报的
修复宣传为整个章节已通过。源文本歧义、模型扩大“等基线”的范围等问题仍存在。

全文实验暴露模型将表格行拼成引文、使用省略号或把证据原文当成正文引文的问题。
没有放宽精确定位校验来获得通过率；全文模块移至 `evals/evaluator_trust/`，未接入线上
worker 或新增质量门禁。模型提出的全文问题仅作为未验证候选，不计为新增 hard violations。

## 测试与交付

- 较早的 Worker + agent_writing + claim cache 完整检查：740 passed、2 skipped。
- 最终代码的相关 PostgreSQL 回归：141 passed，覆盖绑定、placeholder、严格输出、角色核查、
  semantic repair、最终质量收敛、现有全文规则和 claim entailment 缓存。
- 本轮涉及文件 Ruff 检查通过。没有自动启动付费 A/B/C；不作整体质量、时延或成本提升结论。
- 部署验收记录见同目录 validation artifact 的 `deployment-acceptance.json`；仅在该记录明确
  成功后视为完成部署。

结论：可追踪性、失败闭合与已确认的局部漏洞已修复；模型语义可信度仍有边界。保留 legacy、
原 hard-violation 判据与 release gate。后续应先解决歧义金标和输出稳定性，再独立验证；不能
把这轮案例修复或未完成全文审阅替代新 A/B/C 的质量结论。
