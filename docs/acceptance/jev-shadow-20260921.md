# Jev citation shadow：修复、校准与发布验收

## 范围与结论

本次实现的是已确认的“修复数据链路并校准 shadow”方案，不实现逐条 fast-path，
不修改 Planner、Router、Evaluator、Quality Gate 或 retry/replan 的决策权。
`soft_check_citations` 仍在 shadow 下返回原生成式 verifier 的结果，Jev 不改变引用或质量结论。

开发集没有任何阈值满足预先定义的全部准入条件，锁定结果为
`threshold=null / insufficient_evidence`。测试集只用于报告，不再次选择阈值。
线上保持 `shadow`、阈值配置 0.90、2 秒超时、模型 `jev-1.13.0`。
配置中的 0.90 **不是已校准合格的替代阈值**。

## 官方语义与数据链路修复

- 官方明确说明 question id 不进入模型推理；仅有 `item_0` 等名称不能绑定输入。
  v2 的每个问题显式引用 `pairs[i].context` 和 `pairs[i].evidence`。
  [TypeSafe API](https://docs.typesafe.ai/api)
- `confidence` 是由概率分布形状计算的确定性统计量，不是经本业务验证的正确率；
  Score 是各等级的概率加权值，不是最大概率等级。公开说明未提供可用于本地精确重算
  confidence 的公式；不擅自用最大概率、熵或其他分数替换它。
  [TypeSafe confidence](https://docs.typesafe.ai/confidence)
- 保留合法的 `confidence=0`，校验完整答案集合、数值范围、概率键、legend；
  概率和允许每等级 0.005 的舍入误差，不进行归一化或补默认 confidence。
- `TYPESAFE_CACHE_ENABLED=false` 现在同时禁用读写；缓存版本升级为 `jev-v2`。
  线上保留原 job-local 缓存策略，离线实验禁用缓存。
- 新增 `TYPESAFE_MODEL_PRICES`，默认固定版本输入 US$0.042/M token、输出免费。
  未知模型或缺失 usage 记为未定价，不伪报零成本。
  [TypeSafe models/pricing](https://docs.typesafe.ai/models)
- shadow 事件按 occurrence index + pair hash 对齐，而非按 cite key 折叠；
  增加原始 score/confidence/probabilities、baseline 对照、缺失原因、延迟、模型、
  request id、cache provenance。事件不新增稿件正文。

真实调用链保持为：

```text
worker._quality
  → 当前文档 citation usages + selected library entries + EvidenceUnits
  → semantic_sources：有证据时按原顺序拼接，否则 abstract fallback
  → citation_pairs：最多 40 条，context[:300] / evidence[:400]
  → DecisionRunner → TypeSafe 原始响应 → 严格解析 → shadow 逐条事件
  → 原 verifier prompt / parser / 2000-token 输出预算
  → 返回原 verifier findings；继续原有质量流程
```

该软检查不等于独立的 claim-entailment gate；不能拿后者的标签充当前者的真值。
五级参考标注中 0/1 为 weak，2 为 partial（不是 weak），3/4 为 mostly/direct support。

## 冻结与盲审

- 只读、repeatable-read 数据库快照；11 个真实项目，434 个去重文本对。
- 保留完整生产批次；按项目拆分 dev 234 / test 200，排除跨项目重复文本泄漏。
- 全部项目语言为中文，不向英文或其他语言泛化。
- 本批 434 条来源均为 EvidenceUnit，无 abstract-only 样本；只评估截断后的实际软检查输入，
  不应把未覆盖命题直接解释成原论文或完整源文错误。
- 434 条先由 `glm-5.3-flash` 以独立盲标提示生成草稿；主审 Codex 阅读并复核
  每项目固定 5 条及全部 3 条不确定草稿，共 58 条。保留原标签与修改理由。
- 主审与草稿合并后锁定标签，之后才运行被测 Jev 和 baseline；没有看到被测输出后改标签。
- 标注模型与 baseline 同为 GLM，存在同源偏差。这是 **AI-assisted reference，不是独立人工 gold**；
  58 条主审也不应描述成逐条人工审完 434 条。
- corpus SHA-256：`119bdea7f90854be12a1d97fcde6ef8196c530ef6aa29c044f7ed7cda33c1f22`
- labels SHA-256：`004bb978f0f03bba1dd55a5c38a74f67502812ac713bed6988ed263dd98ad09c`

私有原始记录位于 `.paperforge/jev-shadow-20260921-v2/`，包含完整请求/响应、
标签、调用记录、预算预留、分组事件、阈值锁及报告。不提交正文或凭证。
初始目录 `.paperforge/jev-shadow-20260921/` 保留：第一次 dev 因价格 JSON 未解码而被
预算保护拦截，实际发送的被测请求为 0；修复后复制相同冻结数据和标注预算记录，
改用 v2 目录，不删除失败记录或偷偷重跑 holdout。

## 开发集结果

| 指标 | Jev | 当前 verifier（GLM） |
| --- | ---: | ---: |
| 成功产出的判断 | 194/234 | 216/234 |
| 对确定性参考标签的二分类准确率 | 72.40%（192 条） | 65.73%（213 条） |
| 主调用错误 | 1/6，超时 | 0/6；但存在漏项 |
| 调用 p50 / p95 | 1.344 / 2.006 秒 | 33.114 / 41.138 秒 |
| 已定价主调用成本 | US$0.003981，1 次未定价 | US$0.037591 |

准确率分母不同，不能据此宣称 Jev 优于 baseline；应在共同完整子集及独立参考上比较。
Jev/baseline 的共同输出 weak 一致率为 51.14%，一致率也不是准确率。
Jev 原始 confidence：8/194 为 0，均值 0.4141，最大 0.90。
194 条原始响应经过 parser 到事件，score/confidence/probabilities 不一致数为 0；
超时批次的 40 条不可审计为成功输出，不作补值。

| confidence 阈值 | 接纳数 / 234 | 接纳集准确率 | 错误率 95% Wilson 上界 | 整批覆盖率 |
| --- | ---: | ---: | ---: | ---: |
| 0.00 | 194 | 72.40% | 34.32% | 83.33% |
| 0.50 | 62 | 77.42% | 34.41% | 0% |
| 0.70 | 7 | 100% | 35.43% | 0% |
| 0.90 | 1 | 100% | 79.35% | 0% |
| 0.95 | 0 | 不可计算 | 不可计算 | 0% |

高阈值小样本的“100%”不具备准入意义。准入要求至少 100 条确定性接纳样本、
错误率上界 ≤5%、weak precision ≥95%、接纳集 weak recall ≥80%、
不确定标签接纳率 ≤5%、item coverage ≥30%、baseline 完整、
相对 baseline 有害替代的单侧错误增量上界 ≤2 个百分点。
声明减少调用还要求整批覆盖率 ≥20%。Wilson 是 pair-level 近似，未校正项目内相关性。

开发集单项目诊断实验：大小 1/10/20 的平均 confidence 分别为 0.530/0.508/0.467；
40 条批次重复三次均值为 0.419，40 个文本对中 2 个出现 weak/non-weak 翻转。
逆序均值为 0.457。旧版未绑定对照在 2 秒内超时，没有可比较输出，
因此本轮不能量化“旧版全零 → 新版”的纯绑定因果收益。
这些重复观测不是额外独立标签，也没有用于重新定义准入指标。

## 测试集与总预算

| 指标 | Jev | 当前 verifier（GLM） |
| --- | ---: | ---: |
| 成功产出的判断 | 200/200 | 133/200 |
| 确定性参考标签准确率，各自可用分母 | 78.24%（193 条） | 74.42%（129 条） |
| 同一完整子集准确率，129 条 | 83.72%（108/129） | 74.42%（96/129） |
| 主调用错误 | 0/5 | 0/5；但漏项 67/200 |
| 调用 p50 / p95 | 1.368 / 1.830 秒 | 36.502 / 37.633 秒 |
| 已定价主调用成本 | US$0.004244 | US$0.033650 |

同一完整子集的结果仍受同源标签、缺失选择偏差和样本规模影响，不代表已通过替代认证。
开发集对应共同完整子集为 174 条，Jev 128/174、baseline 117/174。
测试集 Jev confidence 均值 0.3927、最大 0.83，4/200 为零；
200 条原始响应到事件字段差异为 0。测试集的 7 条不确定参考标签不参与准确率计算。

| 描述性阈值，不用于重新调参 | 接纳数 / 200 | 接纳集准确率 | 错误率 95% 上界 | 整批覆盖率 |
| --- | ---: | ---: | ---: | ---: |
| 0.00 | 200 | 78.24% | 28.10% | 100% |
| 0.50 | 42 | 85.71% | 27.84% | 0% |
| 0.70 | 2 | 100% | 65.76% | 0% |
| 0.90 | 0 | 不可计算 | 不可计算 | 0% |

没有已锁定候选阈值，测试不能授予 fast-path 资格。低阈值可以制造覆盖率，但错误风险超标；
高阈值没有足够覆盖和样本。现有整批 gate 在 0.90 下两组均不会跳过 verifier。

原始 baseline 响应显示：dev 2/6、test 3/5 次 `finish_reason=length`，
均达到 2000 output tokens，部分预算被 reasoning tokens 消耗；这些返回仍是可解析的
不完整 JSON judgements，现有路径未把“缺少 index”当成整批失败。
最后一批测试还存在无效 index，解析阶段被丢弃。没有为美化本次对照而改 baseline。

全部实际 HTTP attempts 为 **118/500**（标注 45、baseline 11、Jev 62，含消融），
保守成本预留 **US$0.861471/5**；未完成预留为 0，2 次 Jev deadline 失败，
674 个成功 Jev 答案的 raw→parser 差异为 0，394 个主实验答案的 raw→event 差异为 0。
预算预留不是账单，超时调用费用未知并保留预留；不将其记作免费。

11 个主 quality pass 的实际生成式 LLM 调用为 **1/pass**，加上 Jev 为 **2/pass**；
shadow 没有省掉任何 baseline 调用。相对只跑 baseline，测试集额外增加约 US$0.004244
模型成本及每 pass 约 1.4 秒的串行 Jev 延迟。单项快不等于任务更快。
本次不运行完整写作 job；不能把 quality-pass calls 当作整任务 calls/task，
也不把 hypothetical savings 写成实际收益。

## 文件与复现

- `packages/llm_runtime/llm_runtime/decision.py`：响应校验、cache switch、价格与调用记录。
- `services/worker/paperforge_worker/pipelines/citation_decisions.py`：共享输入与 v2 问题绑定。
- `services/worker/paperforge_worker/pipelines/quality.py`：shadow 可观测性及 occurrence 对齐。
- `services/worker/paperforge_worker/worker.py`：使用共享证据选择逻辑，保持既有机制。
- worker `config.py` / `context.py`、两个 env example：配置与运行时传递。
- `evals/jev_shadow/`：冻结、盲标、复核合并、锁定、预算保护、消融、报告和测试。
- runtime/quality 的相关测试覆盖合法零、舍入、非法值、超时、取消、缓存与 shadow 不变性。

完整命令及准入定义见 [校准使用说明](../../evals/jev_shadow/README.md)。

## 发布验收

- 相关测试：201 passed / 19 skipped；跳过的是未配置隔离 Redis/PostgreSQL 的集成测试，
  未把生产数据库当测试库。相关 Ruff 检查与 `git diff --check` 通过。
- 首次部署因 PostgreSQL 连接余量保护失败。通过已有 reap 工具确认两次排空后，清理
  5 个历史部署的容器、网络与可重建缓存；镜像保留，用户数据库/文件未删除。
- `./scripts/dev restart` 成功，新部署：`paperforge-deploy-20260921-235331`，端口 3004。
- 新栈健康、对象存储读写、texd、image-provider 路由预检通过。
- Funnel 目标与公网 `/login` 字节一致性通过；内容 SHA-256：
  `58a10b8fcc09cdd155c29efb86d45bef31e02d1a46446569a260f407e636adb8`。
- 容器内核对 Jev/quality/shared-contract 源码 SHA 与工作区一致；
  运行时实际配置为 shadow / 0.90 / 2 秒 / v2，价格正确加载。
- 前一公网部署 `paperforge-deploy-20260921-133756` 保留，未停止可能拥有旧任务的 worker。

## 下一阶段

继续 shadow，不降阈值强行追求覆盖率。下一轮优先解决 baseline 漏项与参考标签独立性，
并用新冻结数据增加高置信度、非弱类和边界案例数量；诊断 smaller-batch 的延迟、错误率、
中文复杂复合断言适配性。若改变 rubric、问题粒度、batch policy 或模型版本，
应新建实验版本和独立 holdout，不能沿用本轮阈值资格。
只有独立测试通过质量、覆盖率与端到端收益门槛后，才另行讨论逐条 fallback 或上线 fast-path。
