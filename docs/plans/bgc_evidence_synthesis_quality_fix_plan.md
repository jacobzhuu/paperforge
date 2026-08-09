# BGC 综述测试：证据抽取与跨文献综合质量修复方案

> 状态：**规划稿，本轮未修改任何代码**
> 审计基准：工作树 `/data/zhuzy/projects/paper-forge`（= `/home/zhuzy/projects/paper-forge`，同 inode），2026-07-30
> 取证对象：项目 `01a7c8c2-09ea-409d-a397-f4601da2ecee`「深度学习驱动的生物合成基因簇识别、分类与产物预测」（`paper_type=review`，`language=zh`）
> 取证来源：`paperforge-prod-postgres-1` 生产库、MinIO 导出件、`generation_job` / `job_event` / `llm_call_log` 实际记录、Tectonic 编译日志
> 代码版本核对：运行该任务的镜像为 `paperforge-worker:deploy-20260730-130600`；已逐文件 diff 确认 `pipelines/{outline,evidence}.py` 与当前工作树**完全一致**，因此下文所有行号对当前代码有效
> 前置文档：[`problem_driven_literature_review_depth_optimization_plan.md`](problem_driven_literature_review_depth_optimization_plan.md)（M-A/M-B/M-D/M-E 已落地：`qdecomp`/`evidence`/`qmatrix`/`synthesis` 四阶段、5 张新表均已存在）

**结论提要**：本次质量问题**不是范式问题，也不是模型能力问题**。问题驱动骨架、证据分级、可比性门控、句级证据绑定这套机制已经实现并接线；本次跑挂在三个具体缺陷上——一个 JSON 序列化异常打断了 EVIDENCE 阶段、结构化实验模型仍是上一轮投毒攻击专题的硬编码、跨研究比较章节没有证据契约。三者叠加使 27 篇论文里只有 4 篇产出了证据，护栏又因为唯一约束冲突整段崩溃，于是所有降级路径同时暴露。

---

## 1. 取证事实（全部来自真实运行记录）

### 1.1 任务时间线

| 时间(UTC) | 事件 | 记录 |
|---|---|---|
| 07:43:46 | 项目创建 | `paper_project.created_at` |
| 07:56:04 | `kind=full` 任务 `2afefe15-8ee4-4205-8756-2b6807c7d3bc` 启动 | `generation_job` |
| 07:56:44 | QDECOMP 完成：core question + 5 个子问题，`generator=llm:deepseek-v4-pro` | `job_event` seq 4 |
| 07:56:55 | SEARCH：4 条检索式 × 5 provider = 20 条 `search_run` | `search_run` |
| 08:15:54–55 | 3 条 `structured_extraction` 落库 | `structured_extraction.created_at` |
| 08:18:17 | **`reranker` output_truncated** | `llm_call_log` |
| ~08:18 | **`evidence.failed`** | `job_event` seq 23 |
| 08:18:17 | 文档 `e843d9d6` 建立，9 个 section | `paper_document` |
| 08:51:33 | **`writer` 2× output_truncated + 1× timeout**；任务 `succeeded` | `llm_call_log` / `generation_job` |
| 09:57:24–40 | 独立 `compile` 任务导出 7 件产物，`readiness_status=unassessed` | `export_artifact` |

### 1.2 关键计数

| 对象 | 实测值 | 应有值 |
|---|---|---|
| `library_entry` | 140 candidate + **27 selected** | — |
| `fulltext_attempt` | 25 acquired / 7 failed；`ingest.fulltext` 报 `parsed=25, coverage=0.9259` | — |
| `literature_card` | 27 张，**25 张 `fulltext_used=true`**，每张 2–6 条 `quotable_points` | — |
| **`evidence_unit`（本项目）** | **仅 4 篇论文 × 32 条 = 128 条**（`lai2025deciphering` / `lu2026bgc` / `riosmartinez2022deep` / `yang2021deep`），`project_id` 全为 `NULL` | ≥25 篇 |
| **`experiment_result`** | **全库 0 行** | >0 |
| `evidence_measurement` | 全库 4 行（本项目 0 行） | >0 |
| `structured_extraction` | 全库 6 行；本项目 3 行，全部 `main_results=0`、`metrics=[]`、`numeric_records=0` | — |
| `question_evidence_link` | 120 candidates → 63 links（`llm_classified=15`, `fallback_classified=48`） | — |
| `comparison_clusters`（SYNTH） | **0**（5 个子问题全部 `answer_status=partial`） | >0 |
| **`quality_report`** | **0 行** | 1 行 |
| **`claim_evidence_anchor`** | **0 行** | >0 |
| `numlint` 告警 | 19 条 `unsourced_numbers` | — |

### 1.3 三条被吞掉的异常（`generation_job.error_json` + `job_event`）

```
evidence.failed  TypeError: Object of type UUID is not JSON serializable
                 [SQL: UPDATE structured_extraction ...]
                 (紧随 evidence.progress {"done":1,"total":27,"units":32} 之后)

quality          IntegrityError: duplicate key value violates unique constraint
                 "uq_claim_evidence_report_claim_cite"
                 Key (quality_report_id, claim_hash, cite_key)=(bc0e5862-…, 57e5c9eb…)

numlint          unsourced_numbers × 19
```

### 1.4 成稿实测特征

`paper_section` 中 s1–s5 = 5 个子问题（`model=deepseek-v4-pro`，`cite_keys` 各 2–4 个，全部落在那 4 篇有证据的论文上），s6 = 跨研究比较章节：

- **`model = NULL`** —— s6 是确定性回退产物，LLM 从未成功写过这一节。
- s6 `cite_keys` = **27 个（整个文献库）**，`question_id` / `evidence_ids` 均缺失。
- s6 正文 = 7 篇论文的卡片摘要逐条罗列，按 bibtex key **字母序** `almeida2020supporting → almeida2020toucan → ancajas2024advances → blin2023antismash → blin2025antismash → carroll2021accurate → chen2020rna`；其中 3 段是**英文原文**；最后一段是 `chen2020rna` 的 **RNA 二级结构预测（E2Efold）**内容。
- s6 表格 27 行 = **每条 `evidence_unit` 一行**。`Deep self-supervised learning…` 出现 6 次、`BGC-MAC and BGC-MAP…` 出现 11 次、`Deciphering…` 出现 7 次。
- 表格列内容：`任务/数据集` 恒为「未报告」，`关键指标与数值` 恒为「未结构化」，`证据等级` 直接印内部枚举 **`A_located_structured` / `B_located_prose`**，`定位` 出现原文噪声——
  `p.4, 64 subsequences, each trained in parallel in batches of 256 scratch for a particular application. This, however, tends to, figure:3`
- 与此同时 s1–s5 正文里**确实**有真实数值：`AUROC 在 RiPP、糖类和萜类上超过 0.97`、`AUROC 降至 0.857`、`精确率从 0.378 提升至 0.420`、`召回率从 0.593 提升至 0.617`、`F1 分数达到 65.62%`。这些数字进了散文，却一条也没进结构化表。

### 1.5 无关论文的入选路径（`library_entry.rank_reason_json` 实测）

| bibtex_key | 主题 | score | `facet_coverage` | `method` | 入选层 |
|---|---|---|---|---|---|
| `chen2020rna` | RNA 二级结构预测（E2Efold） | 0.251 | **1.0** | `deterministic_v1` | **foundational** |
| `deshpande2024hybrid` | 肝细胞癌病理分级 | 0.320 | **1.0** | `deterministic_v1` | recent |
| `deangeli2025ghostbuster` | 基因优先级排序 | 0.338 | **1.0** | `deterministic_v1` | recent |
| `lu2026bgc`（真正相关） | BGC 分类与产物匹配 | 0.653 | 1.0 | `deterministic_v1+llm_rerank` | recent |

三篇无关论文的 `title_match` 只有 0.025–0.05，却全部拿到 **`facet_coverage=1.0`**，且都**没有经过 LLM 重排**。

### 1.6 检索实测（`search_run`）

| 现象 | 证据 |
|---|---|
| 只发了 **4 条**检索式 | `search_queries(max_queries=4)`（`scope.py:358`） |
| 其中 2 条是**中英混排** `deep learning biosynthetic gene cluster product prediction 基于序列特征的BGC识别模型` | openalex/europe_pmc `hit_count=0`；crossref 返回 73655/30678 泛化命中 |
| arXiv 对 3 条不同查询返回**同一个** `hit_count=626779` | 说明查询未被解析，返回的是泛化结果集——`chen2020rna`、`deshpande2024hybrid` 都来自 arXiv |
| `semantic_scholar` **4/4 全部 failed** | `search_run.status='failed'` |
| 6 个 subtopic 只用了 **2 个**；5 个 sub_question **一个都没参与**查询构造 | `scope.py:381` `[:2]` |

### 1.7 编译实测（Tectonic 日志）

s6 的 longtable **每一行**都溢出，且量值恒定：

```
warning: sections/06-s6:28: Overfull \hbox (33.45613pt too wide) …
… 连续 27 行 …
warning: sections/06-s6:58: Overfull \hbox (23.57263pt too wide) in paragraph at lines 23--58
```

6 列时 `renderer.py:187-188` 给出 `0.2 + 5×0.144 = 0.92\linewidth`，**没有扣除 5 个列间距**（`2\tabcolsep = 12pt` 每个，共 60pt，而预留的 `0.08\linewidth` 只有约 37pt）。差值约 23–33pt，与日志完全吻合。

---

## 2. 根因分析

### 2.1 RC1（最高优先）—— EVIDENCE 阶段被一个 UUID 序列化异常整体中断

`evidence.py:272` 把 `uuid.UUID` 对象塞进将要写入 JSONB 的 payload：

```python
records.append({
    …,
    "evidence_unit_id": evidence_unit_ids.get(candidate.text),   # ← uuid.UUID
})
```

`_structured_payload` 把 `records` 放进 `payload["main_results"]`，随后 `evidence.py:284` `extraction.payload_json = payload` —— asyncpg 无法把 `UUID` 序列化进 JSON，抛 `TypeError`。

三个连锁后果：

1. **异常只在 `records` 非空时触发**，即**恰好只有指标正则命中的论文会炸**。前 3 篇（`records` 为空）安全落库，第 4 篇 `yang2021deep`（`sx=0`）触发异常。
2. `extract_evidence_units` 的主循环（`evidence.py:130`）**没有 per-work 隔离**，异常直接冒到 `_run_stage`，整阶段判失败 → **剩余 23 篇论文一条证据都没产出**。
3. `_run_stage`（`worker.py:126-136`）按 gate-free 设计把异常降级为一条 warning，管线继续跑 QMATRIX/SYNTH/OUTLINE/WRITE，**下游完全不知道证据层只覆盖了 15%**。

RC1 单独解释了：证据面窄（4/27）、`experiment_result` 全库 0 行、比较簇为 0、s1–s5 只引用 4 篇论文、s6 只能退回卡片摘要。**这是用户列出的 7 个问题里 4 个的共同上游。**

### 2.2 RC2 —— 结构化实验模型是上一轮投毒攻击专题的硬编码，BGC 域下必然全空

`evidence.py:334-410` `_structured_payload` 的字段与正则**全部**绑定「序列推荐投毒攻击」：

```python
"task": "sequential_recommendation" if "sequential recommendation" in lower else "",
"attack_goal": … r"target(?:ed)?\s+(?:item|promotion)" …
"threat_model": … r"poison(?:ing)?|injected profile" …
"victim_models": … r"\b(?:GRU4Rec|SASRec|BERT4Rec|BST|NARM|Caser|NextItNet)\b"
"datasets": … r"\b(?:MovieLens(?:-\d+[MK])?|Amazon[- ]?(?:Beauty|Books|Games)|Steam|Yelp)\b"
"baselines": … r"\b(?:Random Attack|Bandwagon|AUSH|DLAttack|LOKI|DDSP|BC-P)\b"
```

数据库里可以直接看到污染：3 条属于上一轮项目的 `structured_extraction` 记着 `task=sequential_recommendation`、`victim_models=["NARM","BERT4Rec"]`；3 条属于本项目的 BGC 论文则是 `task=""`、`victim_models=[]`、`datasets=[]`。`evidence_measurement` 的表结构本身也带 `attack_goal` / `threat_model` / `victim_model` / `attack_budget_json` 五个专题列。

指标白名单（`evidence.py:_METRIC_RE`、`_metric_from_header`）只认
`accuracy|precision|recall|f1|auc|bleu|rouge|mrr|map|ndcg@N|hr@N`
—— **缺 AUROC、AUPRC、F-measure、macro-F1、MCC、Tanimoto、top-k accuracy**，而这些正是 BGC 文献的主力指标。而且正则要求数值紧跟指标名（`(?:=|:|of|was|is|reached|达到|为)?\s*`），`AUROC of 0.98` 会先匹配到 `auc` 再卡在 `ROC of`，`achieving an F-measure of 0.982` 完全不匹配。

**注意：6 条 `structured_extraction` 里 `main_results` 全为 0** —— 这套抽取器在**它自己的原始专题上也一条数值都没抽出来**。它不是「换了领域失效」，而是从未产出过可用数据。

`comparability_key`（`packages/db/db/repositories/evidence.py:51`）另有一处语义反转：docstring 写「unknown protocol is never treated as equal」，实现却把 `None` 归一为字符串 `"unknown"`、把 `None` protocol 归一为 `{"unknown": True}` ——**所有未知维度互相相等**。一旦 measurement 开始产出，可比性门控会反过来批准不可比的横向比较。

### 2.3 RC3 —— 跨研究比较章节没有证据契约，回退路径直接生产垃圾

`outline.py:424` `review_synthesis_section` 有三个结构性缺陷：

| 缺陷 | 位置 | 后果 |
|---|---|---|
| `cite_keys` = 全部纳入论文 | `outline.py:317` / `:534` `[card.cite_key for card in cards]` | 白名单包含 RNA、肝癌等无关论文；写作端合法引用它们 |
| 不带 `question_id` / `evidence_ids` | 章节字典缺字段 | `writing.py:_section_evidence`（`:540`）返回 `[]` → prompt 走 `Literature cards:` 分支，把 **27 张卡片**（`WRITING_CARD_CONTEXT_CHAR_BUDGET=36_000`）全部塞进去 → writer `output_truncated` ×2 + `timeout` ×1 |
| 表格按 `evidence_unit` 建行 | `outline.py:450` `for evidence in evidence_rows`，只按 `evidence_id` 去重 | 一篇论文有多少条证据就出多少行；`_clean(card.methods[0])` 让重复行的方法列**完全一致**，视觉上就是纯重复 |

而 writer 失败后的确定性回退（`writing.py:993`）在 `evidence` 为空时执行：

```python
for key in sorted(allowed):          # ← 字母序遍历整个文献库
    text = str(card.get("summary") or card.get("title") or "").strip()
```

于是 `MAX_PARAGRAPHS_PER_SECTION=8` 截断在字母序第 7 篇，产出「almeida→chen」的逐篇罗列，并把 `chen2020rna` 的 RNA 内容原样搬进正文。**s6 的每一个症状——无关论文、中英混杂、逐篇罗列——都是这一个回退分支的直接输出。**

`outline.py:487` 另外把 `str(evidence.get("grade"))` 原样写进表格单元格，全仓库（含 `apps/web`）没有任何 grade → 人读标签的映射层。

### 2.4 RC4 —— QUALITY 阶段因唯一约束冲突整段崩溃，所有护栏从未运行

`replace_claim_evidence`（`packages/db/db/repositories/quality.py:116`）逐条 `session.add`，**没有去重也没有 ON CONFLICT**；表上有 `uq_claim_evidence_report_claim_cite (quality_report_id, claim_hash, cite_key)`。

`build_claim_evidence`（`quality.py:175`）对每个 `(sentence, cite_key)` 产出一条 anchor，`claim_hash` 取句子哈希。本次 s3/s4 共享了 24 条相同证据、s6 回退又把证据/摘要原文当正文，**同一句子 + 同一 cite_key 必然在文档里出现多次** → `IntegrityError` → 整个 QUALITY 事务回滚。

因此：`quality_report` 0 行、`claim_evidence_anchor` 0 行、`readiness_status=unassessed`，**R4（证据分级）/R5（可比性）/R6（定位）三条红线一次都没执行**。这就是内部枚举、无关论文、英文原文能一路走到 PDF 的直接原因。

更麻烦的是可交付性判定被绕过：`can_render_after_quality("scholarly", "unassessed")` 返回 False，全流程任务确实跳过了导出；但用户随后手动触发的独立 `compile` 任务照样产出了 7 件产物，`readiness_status` 仍是 `unassessed`。**质量门在独立导出路径上不存在。**

### 2.5 RC5 —— 相关性过滤：facet 被拆成单词，纳入标准从未被消费

`ranking.py:378` `_facet_coverage` 计算「命中了几个概念面」，但 facet 是 `_tokens()` 对关键词组求**并集**后的**单词集合**。于是 facet「生物合成基因簇识别与分类」实际展开为 `{biosynthetic, gene, cluster, bgc, secondary, metabolite, detection, identification, prediction, genome, mining, classification}` —— 一篇论文只要出现 `prediction` 或 `classification` 就算命中整个 facet。RNA 论文和肝癌论文因此都拿到 `facet_coverage=1.0`。

配套问题：

- `AUTO_SELECT_MIN_TOPIC_EVIDENCE = 0.05`（`ranking.py:128`），而 `topic_evidence` 取四路信号的 **max**，`facet_coverage=1.0` 直接顶满。
- `balanced_selection_indices` 的 foundational 配额（`ranking.py:173`，`limit*0.25`）在「2018 年前 BGC 深度学习论文本来就少」的情况下，把 2020 年的 arXiv RNA 论文顶了进来。
- `rerank_with_llm` 只覆盖 `top_n=40`，且本次一批 `output_truncated`（`max_output_tokens=2000` 要求返回全部相关 index）。三篇无关论文的 `method` 都停留在 `deterministic_v1`，**语义复核根本没跑到它们**。
- `scope_json.scope_summary` 里明明白白写了纳入/排除标准（「排除仅使用传统规则或同源性方法……以及纯湿实验验证而无计算建模的文献」），但**全仓库没有任何代码消费它**。`curate` 阶段（`worker.py:276`）只是 `_assign_keys`（分配并核验 bibtex key），没有任何筛选逻辑。

### 2.6 RC6 —— 任务边界与证据路由：中文子问题在路由器里退化为无条件放行

`qmatrix.py:243` `_terms` 对中文用 `[㐀-鿿]{2,}` 贪婪匹配**整段连续汉字**，不做二元组切分。中文子问题「深度学习方法在BGC识别任务中相比传统规则/同源性方法有何性能优势与局限？」得到的是几个长汉字串，与英文证据文本的交集**恒为 0**。

于是 `_rank_candidates` 的 `overlap ≈ 0`，判定完全落到：

```python
if overlap >= MIN_LEXICAL_SCORE or unit.kind in expected_kinds:   # qmatrix.py:146
```

而 `_evidence_kind`（`evidence.py:632`）把含数字的片段一律判为 `experimental_fact`，本次 63 条 link 对应的证据**全部**是 `experimental_fact`；5 个子问题的 `expected_evidence_kinds` 也**全部**包含 `experimental_fact`。结果：**每条证据对每个子问题都无条件入选候选**，`score` 只剩 grade bonus + 0.15。

实测后果：s3、s4 各拿到 **24 条 = `MAX_CANDIDATES_PER_QUESTION` 上限**（LLM 把所有候选都判为相关）；16 条证据被路由到 2 个子问题、7 条到 3 个、2 条到 4 个。**BGC 识别 / 分类 / 产物预测 / 数据集 / 应用泛化这五个任务之间没有任何边界。**

### 2.7 RC7 —— 证据等级通胀与 locator 污染

`_fulltext_grade`（`evidence.py:584`）：

```python
if object_ref and (page or section):
    return "A_located_structured"
```

而 `object_ref` 可以来自 `_object_reference(text)` —— 一个在**正文散句里**搜索 `table|figure|equation|algorithm` + 数字的正则。一句「as shown in Figure 2」就把普通散文升级为「A 级结构化定位证据」。实测 63 条 link 里 **45 条是 A 级**，而其中不少 locator 是 `Introduction, figure:1`（引言里提到图 1）。

`section_path` 直接取 `[[SECTION=…]]` 标记内容并截 300 字符；PDF 标题识别是启发式的，于是把正文噪声当成章节名写进了 locator 列。

### 2.8 RC8 —— 输出语言从未被约束

`cards.py:_SYSTEM_PROMPT_ZH`（`:28`）与 `writing.py:_SYSTEM_PROMPT_ZH`（`:45`）虽然本身是中文，但**没有一句要求输出语言**。模型对英文输入倾向于用英文作答，于是 `almeida2020toucan` / `blin2023antismash` / `carroll2021accurate` 的卡片摘要是英文，被回退路径原样搬进正文。

同时 `card_source_hash`（`cards.py:82`）= `hash(title + abstract + fulltext)`，**不含 language** —— 英文项目抽的卡片会被中文项目原样复用。QUALITY 侧也没有任何语言一致性检查（全仓库 grep 无 `language_mismatch` / `latin_ratio` 类实现）。

### 2.9 RC9 —— 表格排版：列宽公式漏算列间距

`renderer.py:187-188`，6 列时列宽和为 `0.92\linewidth`，但 5 个列间距共 `10\tabcolsep = 60pt`（`@{}` 只消掉了首尾两个半间距）。预留的 `0.08\linewidth ≈ 37pt` 不够，恒定溢出 23–33pt。叠加两个放大因素：`研究` 列印**完整题名**（最长 95+ 字符），`定位` 列印**未限长的 section_path**。

`MAX_TABLE_ROWS_IN_PDF = 40` 对「每条证据一行」的表来说约束不到（本次 27 行），一旦证据层修好（预期 300+ 条），会静默截断到 40 行。

---

## 3. 目标不变量（新增红线，与既有 R1–R6 并列）

| 编号 | 名称 | 定义 |
|---|---|---|
| **R7** | 任务本体归属 | 每个 `evidence_unit` 必须绑定一个 `task_id`（来自项目任务本体），且只能被路由到 `task_id` 相符的子问题。任务不符 → 不入 `question_evidence_link`。 |
| **R8** | canonical paper 唯一性 | 正文主表中一篇 canonical paper **只能出现一行**；逐条证据只能出现在 evidence ledger（附录/独立导出件）中。 |
| **R9** | 核心论文闭包 | 跨研究综合章节的 `cite_keys` 与 `evidence_ids` 必须 ⊆「已纳入核心论文 × 已链接证据」的闭包。任何超出即为 blocker。 |
| **R10** | 内部状态不出稿 | 正文 IR 中不得出现内部枚举（`^[A-D]_[a-z_]+$`、`supports/contradicts/not_comparable`、`comparability_key` 哈希）。渲染前断言，命中即 blocker。 |
| **R11** | 语言一致性 | 项目语言为 `zh` 时，正文段落的拉丁字符占比不得超过阈值（术语/缩写除外）；超阈值 → 自动改写或 blocker。 |
| **R12** | 阶段完整性可见 | 任何证据类阶段（ingest/evidence/qmatrix/synth/quality）的失败或部分完成，必须进入 `readiness_status` 与导出件元数据，不能只留一条 warning。 |

---

## 4. 数据模型改造

### 4.1 新增：任务本体

**`task_definition`**（全局参考数据，随迁移种子化，可由项目覆盖）

| 列 | 说明 |
|---|---|
| `id` / `slug` | 如 `bgc.identification`、`bgc.classification`、`bgc.product_structure_prediction`、`bgc.product_activity_prediction`、`benchmark.dataset_construction`、`tool.engineering`、`validation.wetlab` |
| `domain` | `bgc` / `recsys_attack` / … 用于隔离专题词表 |
| `label_i18n_json` | 人读名称（zh/en），**表格与正文只能使用它** |
| `metric_whitelist_json` | 该任务合法指标及归一名（`AUROC`/`AUPRC`/`F1`/`macro-F1`/`F-measure`/`MCC`/`precision`/`recall`/`Tanimoto`/`top-k accuracy`/…） |
| `dataset_whitelist_json` | `MIBiG`(+版本) / `antiSMASH-DB` / `IMG-ABC` / `OrthoDB` / `Aspergillus niger` / … |
| `dimension_schema_json` | 该任务的可比维度定义（见 4.3），取代硬编码的 attack_* 槽位 |
| `exclusion_cues_json` | 反例线索（如 `RNA secondary structure`、`histopathology grading`）用于 SCREEN 阶段 |

**`project_task_profile`**：`project_id` × `task_id`，`is_core`（是否本综述的核心任务），`order_index`。由 QDECOMP 产出、前端可编辑。

`research_question` 增列 `task_id`（FK，nullable，跨任务问题为 NULL 并显式标注）。

### 4.2 改造：证据分层

`evidence_unit` 增列：

| 列 | 说明 |
|---|---|
| `task_id` | FK task_definition，nullable；**RC6 的执行依据** |
| `topical_status` | `on_topic` / `off_topic` / `uncertain`；SCREEN 与抽取阶段共同写入 |
| `anchor_strength` | `structured_cell`（真的落在表格单元格/公式编号上） / `object_mention`（正文提到图表号） / `prose_only`。**`A_located_structured` 只允许 `structured_cell`**，修 RC7 |
| `locator_display` | 归一后的人读 locator（`p.6, Tab.3`），与原始 `section_path` 分离 |

新增 **`paper_evidence_rollup`**（物化视图或表）：`project_id × work_id` → 主表一行所需的聚合字段（`task_ids`、`datasets`、`splits`、`model_families`、`headline_metric`、`headline_value`、`evidence_grade_best`、`evidence_unit_count`）。**这是 R8 的执行载体**：正文主表只读它，逐条证据只出现在 ledger。

### 4.3 改造：实验结构化（`experiment_v2`）

`structured_extraction.schema_version` 升到 `experiment_v2`，payload 与 `experiment_result` 列都去掉 attack 专题槽位，改为任务参数化：

| 组 | 字段 |
|---|---|
| 任务 | `task_id`、`task_variant`（如 `binary detection` / `multi-class product class`） |
| 数据 | `dataset`、`dataset_version`、`split_strategy`（random / cluster-based / temporal / leave-one-genome-out）、`train_size` / `val_size` / `test_size`、`positive_count` / `negative_count`、`negative_sampling`、`label_source` |
| 模型 | `model_family`（CNN/BiLSTM/Transformer/GNN/CRF/PLM-embedding）、`architecture_detail`、`pretrained_backbone`（如 ESM2-650M）、`input_representation`（nucleotide / amino acid / Pfam domain / domain graph）、`param_count` |
| 训练 | `loss_function`、`optimizer`、`lr`、`batch_size`、`epochs`、`regularization` |
| 结果 | `metric_name`(归一) / `value` / `unit` / `ci_low` / `ci_high` / `std`、`baseline_name` / `baseline_value` / `delta`、`record_kind`(`main_result` / `ablation` / `baseline` / `case_study`) |
| 溯源 | `evidence_unit_id`（**存为 `str`**）、`source_location`、`anchor_strength` |

`comparability_key` 重写：
- 槽位改为 `task_id | dataset | dataset_version | split_strategy | metric_name`（+ `dimension_schema_json` 声明的任务专有维度）；
- **未知维度不再归一为 `"unknown"`**，而是加入 `evidence_unit_id` 盐，保证两个未知协议**不相等**（兑现原 docstring 承诺，修 RC2 的语义反转）；
- 保留旧列一个迁移周期，双写。

### 4.4 新增：纳入筛选审计

**`eligibility_decision`**：`project_id × work_id`，`decision`(`include`/`exclude`/`uncertain`)、`criterion_hits_json`、`anchor_facet_hit`(bool)、`reason`、`decided_by`(`deterministic`/`llm`/`user`)、`model`。前端可覆盖，落库可审计。

`paper_project.scope_json` 的纳入/排除标准提升为结构化 `eligibility_criteria`：`required_anchor_facets`（必须命中，如「biosynthetic gene cluster」概念面）、`required_method_facets`、`exclusion_domains`。

---

## 5. 模块改造清单

| 文件 | 改动 | 关联根因 | 规模 |
|---|---|---|---|
| `pipelines/evidence.py:272,284` | UUID → `str`；payload 序列化前统一走 JSON-safe 编码器 | RC1 | 极小 |
| `pipelines/evidence.py:130-200` | 主循环加 per-work `try/except`，单篇失败记 `evidence.work_failed` 并继续；阶段结束汇报 `works_ok / works_failed / coverage` | RC1 | 小 |
| `pipelines/evidence.py:334-410` | `_structured_payload` 重写为 `experiment_v2`，维度由 `task_definition.dimension_schema_json` 驱动 | RC2 | 大 |
| `pipelines/evidence.py:_METRIC_RE, _metric_from_header` | 指标表按任务参数化；放宽指标—数值邻接（支持表格行、括号、`of/at/=`、中文）；补 AUROC/AUPRC/F-measure/macro-F1/MCC/Tanimoto/top-k | RC2 | 中 |
| `pipelines/evidence.py:584-594,648` | `anchor_strength` 三态；A 级只给 `structured_cell`；`locator_display` 归一 | RC7 | 中 |
| `pipelines/evidence.py:632` | `_evidence_kind` 引入 `task_id` 判定与更细的 kind 分类，不再让含数字即 `experimental_fact` | RC6 | 中 |
| `packages/db/db/repositories/evidence.py:38-71` | `comparability_key` 重写（去 attack 槽位、未知不相等） | RC2 | 中 |
| `packages/db/db/repositories/quality.py:116` | `replace_claim_evidence` 先按 `(claim_hash, cite_key)` 去重，再 `ON CONFLICT DO NOTHING` | RC4 | 极小 |
| `pipelines/quality.py:175` | `build_claim_evidence` 输出前去重；R7/R8/R10/R11 校验；语言一致性检查 | RC4/R10/R11 | 中 |
| `pipelines/qmatrix.py:243` | `_terms` 中文改二元组切分（对齐 `ranking.py:_tokens` 的既有做法与注释） | RC6 | 小 |
| `pipelines/qmatrix.py:146` | 去掉 `or unit.kind in expected_kinds` 的无条件放行；改为 task 硬约束 + 相关性阈值 + top-K 三重门 | RC6 | 小 |
| `pipelines/qmatrix.py` | 新增 `task_id` 不符即拒；记录 `rejected_by_task` 计数 | RC6/R7 | 中 |
| `pipelines/synthesis.py` | 比较簇按 `experiment_v2` 维度分组；`not_comparable` 显式给出**差异维度名**（而非哈希） | RC2 | 中 |
| `pipelines/outline.py:424-548` | `review_synthesis_section` 重写：消费 synthesis bundle；`cite_keys`/`evidence_ids` 收窄到核心论文闭包；主表读 `paper_evidence_rollup`（一篇一行）；grade 走 display label；无比较簇时输出「证据不足」骨架而非罗列 | RC3/R8/R9/R10 | 大 |
| `pipelines/outline.py` | 新增 `evidence_ledger_section`（附录，逐条证据 + locator），并在 PaperIR / LaTeX 模板中支持 appendix 区 | R8 | 中 |
| `pipelines/writing.py:993` | 删除「无证据就按字母序倒卡片摘要」的回退；改为「章节目标 + 证据缺口声明 + `\todo{}`」 | RC3 | 小 |
| `pipelines/writing.py:426-520` | prompt 组装按证据簇裁剪；卡片分支只在 `paper_type=original` 的 Related Work 保留；writer 失败时**用更小上下文重试**而不是原 prompt 重跑 | RC3 | 中 |
| `pipelines/writing.py:45,74` + `cards.py:28,44` | prompt 显式声明输出语言 | RC8 | 极小 |
| `pipelines/cards.py:82` | `card_source_hash` 纳入 `language` | RC8 | 极小 |
| `pipelines/ranking.py:378` | `_facet_coverage` 改 phrase-level 匹配（多词关键短语整体命中）；facet 分 anchor / supporting 两类，**anchor facet 未命中即不可自动入库** | RC5 | 中 |
| `pipelines/ranking.py:128,173` | `AUTO_SELECT_MIN_TOPIC_EVIDENCE` 提高并拆成「anchor 分下限 + 总分下限」；foundational 配额受 anchor 分约束 | RC5 | 小 |
| `pipelines/ranking.py:rerank_with_llm` | 分批（每批 ≤20 候选，`max_output_tokens` 相应上调）；截断后**按批重试**而不是整体放弃 | RC5 | 中 |
| **新建** `pipelines/screen.py` | SCREEN 阶段：结构化 `eligibility_criteria` 逐候选判定，落 `eligibility_decision` | RC5 | 大 |
| `pipelines/scope.py:358-397` | `max_queries` 提到 12+；每个 sub_question 生成 1–2 条**纯英文**查询；禁止中英混排（`_has_latin` 判据改为「不含 CJK」而非「含拉丁」——`BGC` 让当前判据失效）；subtopics 不再 `[:2]` | RC-检索 | 中 |
| `packages/scholar_gateway` | 修 `semantic_scholar` 适配器（本次 4/4 失败）；arXiv 适配器对含 CJK 的查询直接拒绝而不是发出去 | RC-检索 | 中 |
| `packages/latex_render/renderer.py:180-200` | 列宽扣除 `(n-1)·2\tabcolsep`；>4 列改 `tabularx` + `\small`；`研究` 列改「一作+年份」短引；locator 列限长 | RC9 | 中 |
| `packages/latex_render/compile.py` | 编译后 lint：`Overfull > 5pt` 计入 `layout_checks_json`；`scholarly` profile 下为 blocker | RC9/R12 | 小 |
| `worker.py:1255-1382` | `curate` 之后插入 `screen`；证据类阶段的失败/部分完成汇总进 `readiness_status`；独立 `compile` 任务也要过 `can_render_after_quality` | RC1/RC4/R12 | 中 |
| `apps/web/lib/labels.ts` + 证据面板 | grade / stance / task 的 display label；新增「纳入筛选」与「证据台账」视图 | R10 | 中 |

**明确不改动**：R1/R2/R3 引用真实性三防线、PaperIR 核心 schema、texd/visuald、账号与导出存储布局。

---

## 6. 实施 Milestone

### M0 —— 止血（0.5–1 天，P0，无新依赖，无迁移）

1. `evidence.py:272` UUID → `str`；payload 走统一 JSON-safe 编码。
2. `evidence.py` 主循环 per-work 隔离 + `works_ok/works_failed/coverage` 上报。
3. `replace_claim_evidence` 去重 + `ON CONFLICT DO NOTHING`。
4. `writing.py:993` 删掉字母序卡片摘要回退，换成证据缺口声明。
5. `worker.py`：证据/质量阶段失败进 `readiness_status`；独立 `compile` 任务补上质量门。

**验收**：同一项目重跑 —— `evidence_unit` 覆盖 ≥ 90% 已获全文论文（当前 4/25）；`experiment_result > 0`；`quality_report` = 1 行；`claim_evidence_anchor > 0`；s6 不再出现任何逐篇卡片摘要段落。

> **这是投入产出比最高的一步**：五处改动、约 60 行，直接解开用户列出的 7 个问题中的 4 个的上游。**在拿到重跑结果之前不要开始 M2 之后的任何工作。**

### M1 —— 任务本体与证据路由（3–5 天，P0）

- `task_definition` / `project_task_profile` 迁移 + BGC 七任务种子数据；`research_question.task_id`；`evidence_unit.{task_id,topical_status,anchor_strength,locator_display}`。
- QDECOMP 产出任务画像并把子问题绑定到任务。
- `qmatrix.py`：`_terms` 中文二元组；去无条件放行；task 硬约束。
- `evidence.py`：`anchor_strength` 三态，A 级收紧到 `structured_cell`。

**依赖**：M0。**验收**：单条证据平均归属子问题数 ≤ 1.3（当前 2.33）；无子问题拿到 `MAX_CANDIDATES_PER_QUESTION` 上限；A 级证据占比从 71% 降到与真实表格/公式命中率一致；RNA / 肝癌类论文的证据 `topical_status=off_topic` 且不进任何 link。

### M2 —— 实验与方法结构化（5–8 天，P0）

- `experiment_v2` schema + `experiment_result` 列改造 + `comparability_key` 重写（双写一个周期）。
- 指标/数据集表按任务参数化；表格 cell 级抽取接 `parse_markdown_table`；prose 抽取放宽邻接约束。
- 抽出数据集、划分方式、模型结构、损失函数、训练配置、指标、消融——即用户第 5 项要求的完整维度集。

**依赖**：M1。**验收**：G1 黄金集上指标值召回 ≥ 70%、数值精确率 ≥ 95%；本项目 `experiment_result ≥ 60` 行、`comparison_clusters ≥ 3`；主表「关键指标与数值」列「未结构化」占比 ≤ 15%。

### M3 —— canonical paper 分层 + evidence ledger（3–5 天，P0）

- `paper_evidence_rollup`；正文主表一篇一行，列 = `[研究(短引+年份), 任务, 数据集/划分, 模型/输入表示, 主指标与数值, 证据强度(人读), 证据条数]`。
- 附录 `evidence_ledger` 章节 + PaperIR/LaTeX appendix 支持；逐条证据带归一 locator。
- display-label 映射层（后端 + 前端）；渲染前 R10 断言。

**依赖**：M2。**验收**：主表行数 == 纳入论文数（当前 27 行 / 4 篇）；正文与 PDF 中内部枚举出现次数 = 0；ledger 条数 == `question_evidence_link` 去重后条数。

### M4 —— 跨研究综合章节证据契约（3–5 天，P0）

- `review_synthesis_section` 重写为消费 synthesis bundle；R9 闭包约束。
- 章节 prompt 按比较簇裁剪；writer 失败走**缩小上下文重试**阶梯。
- 综合内容严格限定为一致 / 差异 / 冲突 / 不可比 / 缺口五类判定，`not_comparable` 报差异维度名。

**依赖**：M2、M3。**验收**：s6 `model != NULL`；s6 引用集 ⊆ 核心论文集；连续 attribution 句 ≤ 2；跨文献段落（引用 ≥2 篇且带 stance 判定）占比 ≥ 60%；writer `output_truncated` / `timeout` 次数 = 0。

### M5 —— 纳入筛选与检索覆盖（4–6 天，P1）

- `eligibility_criteria` 结构化 + `eligibility_decision` + 新建 SCREEN 阶段（插在 `curate` 之后）。
- `_facet_coverage` phrase-level + anchor facet 强制；阈值拆分；foundational 配额受约束。
- `search_queries` 扩到 12+ 条并由 sub_question 驱动；禁止中英混排；rerank 分批；修 `semantic_scholar`。

**依赖**：M1（需要 task/anchor facet 定义）。**验收**：selected 中 off-topic 比例 ≤ 3%（人工抽检 30 篇）；每个 sub_question 至少 5 篇论文有证据；provider 成功率 ≥ 4/5；`search_run` 中 `hit_count=0` 的查询数 = 0。

### M6 —— 排版与导出质量（2–3 天，P1，可与 M5 并行）

- 列宽公式修正 + >4 列 `tabularx` + 短引 + locator 限长。
- 编译后 layout lint（`Overfull > 5pt`）进 `layout_checks_json`，`scholarly` 下为 blocker。
- `MAX_TABLE_ROWS_IN_PDF` 语义改为「主表按论文数、ledger 允许跨页不截断」。

**依赖**：M3。**验收**：`compile_log` 中 `Overfull > 5pt` 行数 = 0；longtable 续页表头正常重复；ledger 表跨页不丢行。

### M7 —— 语言与成稿卫生（1–2 天，P1）

- 所有生成 prompt 显式声明输出语言；`card_source_hash` 加 language。
- QUALITY 增 R11 语言一致性检查（拉丁字符比例，白名单术语/缩写豁免）。

**依赖**：M0。**验收**：zh 项目正文英文段落数 = 0；卡片语言与项目语言一致率 100%。

### M8 —— 评测与回归（3–5 天，P1，与 M5/M6 并行）

- 黄金集 G1：20 篇 BGC OA 论文，人工标注章节结构、表格、指标数值、数据集与划分；另 10 篇明确无关论文作为负例。
- G2：本项目的 5 个子问题人工标注「应归属证据」，用于路由精度评估。
- 12 项自动化指标落库 + 导出中心面板；本次运行冻结为回归 fixture。

**验收**：见 §7。

---

## 7. 测试与验收标准

### 7.1 自动化指标（由 QUALITY 阶段落库，前端展示）

| # | 指标 | 定义 | 本次实测基线 | 目标 |
|---|---|---|---|---|
| 1 | 证据层覆盖率 | 有 ≥1 条 `evidence_unit` 的论文 / 已获全文论文 | **4/25 = 16%** | ≥ 90% |
| 2 | 结构化数值产出 | `experiment_result` 行数 | **0** | ≥ 60 |
| 3 | 指标值召回（G1） | 抽出的 metric-value 对 / 人工标注 | 不可测 | ≥ 70% |
| 4 | 数值精确率（G1） | 抽出值与原文一致的比例 | 不可测 | ≥ 95% |
| 5 | 主表分层正确性 | 主表行数 == 纳入论文数 | **27 行 / 4 篇（6.75×）** | == 1.0 |
| 6 | 内部标签泄漏 | 正文 IR + PDF 中内部枚举出现次数 | **≥27**（每行一个 grade） | 0 |
| 7 | 证据路由发散度 | 单条证据平均归属子问题数 | **2.33** | ≤ 1.3 |
| 8 | 任务不符路由率 | `task_id` 不匹配却被链接的比例 | 不可测（无 task_id） | 0 |
| 9 | off-topic 入库率 | 人工抽检 30 篇 selected 中主题无关比例 | **≥3/27 = 11%** | ≤ 3% |
| 10 | 逐篇罗列段落比例 | 连续 attribution ≥3 句 或 段内单一 cite_key 的 attribution 段 | s6 **100%** | ≤ 15% |
| 11 | 跨文献综合段落比例 | 引用 ≥2 篇且带 stance 判定 | s6 **0%** | ≥ 60% |
| 12 | 语言一致性 | zh 项目中英文段落数 | **3** | 0 |
| 13 | 质量门可用性 | `quality_report` 是否产出 | **0 行** | 1 行/次 |
| 14 | 比较簇数 | SYNTH `comparison_clusters` | **0** | ≥ 3 |
| 15 | 排版溢出 | `Overfull > 5pt` 的行数 | **≥28** | 0 |
| 16 | A 级证据真实性（G1） | `anchor_strength=structured_cell` 的 A 级证据占全部 A 级的比例 | 不可测 | ≥ 90% |

### 7.2 回归测试

- **单测**：`evidence.py` payload JSON-safe（含 UUID 用例）；per-work 失败隔离；`replace_claim_evidence` 重复 `(claim_hash, cite_key)` 幂等；`comparability_key` 未知维度不相等；`_facet_coverage` phrase-level（RNA/肝癌两篇真实标题+摘要作为负例 fixture）；`_terms` 中文二元组；`search_queries` 拒绝中英混排（用 `基于序列特征的BGC识别模型` 作为回归 case）；`review_synthesis_section` 一篇一行；`_render_table` 6 列宽度和 + 列间距 ≤ `\linewidth`；R10 断言。
- **端到端**：本次运行冻结为 fixture（scope + 27 篇题录摘要 + 25 份全文），断言 §7.1 全部指标；无 LLM 模式下也必须跑通且不产生逐篇罗列。
- **执行环境**：按 `MEMORY.md`，本机 `uv run pytest` / `pnpm test` 不可用，须走一次性容器（`paperforge-worker:local` + 独立 `paperforge-postgres:local`，`PYTHONPATH` 覆盖所有 `packages/*` 与 `services/*`，`-p no:cacheprovider`）。容器用后无法停止/删除，需向用户说明遗留。
- **部署验证**：`./scripts/dev restart` 滚新 blue/green 项目并重指 Funnel；注意每个部署独立 Redis db，旧 worker 仍在轮询。

### 7.3 人工验收（M4 完成后）

同一 BGC 主题重跑一次，检查：跨研究章节是否只讨论核心论文、是否呈现真实的一致/差异/冲突/不可比/缺口、主表是否一篇一行且指标列有值、附录 ledger 是否可逐条溯源到页码或表号、PDF 表格是否无溢出与跨页丢行。

---

## 8. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 任务本体的领域绑定 | 换到新领域又要重新硬编码 | `task_definition` 是**数据**不是代码；BGC 与 recsys_attack 作为两套种子并存，验证「加领域 = 加数据行」 |
| 证据量上升后成本上升 | EVIDENCE 从 4 篇变 25 篇，抽取成本约 6× | 复用 `source_hash` 跨项目缓存；`experiment_v2` 主体仍是确定性抽取，只在表格解析失败时兜 LLM |
| 严格化与 draft-first 冲突 | R7–R11 可能让「永远能出稿」失效 | 全部实现为**降级改写 + 显式标注**；`scholarly` profile 下为 blocker，`draft` 仍只告警 |
| 存量项目迁移 | 5 张表增列 + `comparability_key` 语义变更 | 双写一个周期；旧项目 `task_id` 为 NULL 时退回当前行为；`experiment_v1` 记录保留 |
| SCREEN 阶段误杀 | 边缘相关论文被排除 | 三态判定（include/exclude/uncertain），`uncertain` 仍入库但标注；前端可覆盖，决策全程可审计 |
| PDF 表格 6 列本质拥挤 | 即使修好宽度也难读 | 主表列数压到 6 以内 + 短引；超宽内容一律进 ledger（可跨页、可 landscape） |

---

## 9. 推荐路线

1. **先做 M0，立刻重跑，再决定后续。** 本次的 7 个症状里有 4 个（证据面窄、未结构化、s6 退化、护栏失效）挂在五处共约 60 行的缺陷上。在拿到重跑数据之前投入 M2 之后的工作，有很大概率是在为一个已经消失的问题做设计。
2. **M1 优先于 M2。** 任务本体是「证据路由」和「实验结构化」共同的前提：没有 `task_id`，指标白名单没法参数化，路由也没法做硬约束。
3. **M3（分层）与 M4（综合章节契约）必须一起交付。** 主表一篇一行、逐条证据进 ledger、综合章节只能引用核心论文闭包——三者是同一个不变量的三个面，分开做会留下不一致的中间态。
4. **M5 的检索/筛选改造独立且可并行**，但要放在 M1 之后（需要 anchor facet 与任务定义）。
5. **RC4 的护栏修复价值高于任何深度提升。** 一个崩掉的质量门比一个浅显的综述危害更大——它让内部枚举、无关论文、英文原文全部无声地走到成稿。
6. **保持 draft-first。** 所有新红线以「降级改写 + 显式标注」实现；`scholarly` profile 承担严格度，`draft` 不变。

---

## 附录 A：关键源码索引

| 主题 | 位置 |
|---|---|
| UUID 进 JSONB（RC1） | `services/worker/paperforge_worker/pipelines/evidence.py:272`，写入点 `:284` |
| EVIDENCE 主循环无隔离 | `evidence.py:130-200`，`if not candidates: continue` 在 `:139` |
| 硬编码投毒专题 payload（RC2） | `evidence.py:334-410` |
| 指标正则与表头正则 | `evidence.py:_METRIC_RE`（`:41`）、`_metric_from_header`（`:597`） |
| 证据等级通胀（RC7） | `evidence.py:584-594` + `_object_reference` `:648` |
| kind 判定过粗 | `evidence.py:632-645` |
| `comparability_key` 未知相等（RC2） | `packages/db/db/repositories/evidence.py:38-71`，归一在 `:51` |
| claim anchor 无去重（RC4） | `packages/db/db/repositories/quality.py:116-139` |
| anchor 生成 | `pipelines/quality.py:175-231` |
| 就绪门 / profile 分支 | `pipelines/quality.py:424-470` |
| 路由中文失效（RC6） | `pipelines/qmatrix.py:243-247` |
| 路由无条件放行 | `pipelines/qmatrix.py:146`，上限 `:19` |
| 综合章节表按证据建行（RC3） | `pipelines/outline.py:450`；grade 泄漏 `:487`；全库 cite_keys `:317`/`:534` |
| 问题驱动章节 | `pipelines/outline.py:323-372` |
| 章节键归一 | `pipelines/outline.py:595-636` |
| 卡片摘要回退（RC3） | `pipelines/writing.py:993-1005`；上限 `MAX_PARAGRAPHS_PER_SECTION=8` `:28` |
| 章节证据取值 | `pipelines/writing.py:540-556` |
| prompt 组装与预算 | `pipelines/writing.py:426-520`，预算常量 `:28-34` |
| 写作系统提示词（无语言约束，RC8） | `pipelines/writing.py:45,74`；卡片侧 `pipelines/cards.py:28,44` |
| 卡片缓存键无 language | `pipelines/cards.py:82` |
| facet 单词化（RC5） | `pipelines/ranking.py:378-383`；`_tokens` `:334` |
| 自动入库阈值 / 分层配额 | `pipelines/ranking.py:128`、`:173-176` |
| LLM 重排（截断即整体放弃） | `pipelines/ranking.py:254-320` |
| 检索式构造（中英混排、只用 2 个 subtopic） | `pipelines/scope.py:358-397`，判据 `:386` |
| 表格列宽漏算列间距（RC9） | `packages/latex_render/latex_render/renderer.py:180-200`；上限 `:32-33` |
| longtable 降级 | `packages/latex_render/latex_render/compile.py:126-175` |
| 全流程编排 / 阶段降级 | `services/worker/paperforge_worker/worker.py:1255-1382`；`_run_stage` `:93-136`；交付判定 `:188-198`；`curate` `:276` |

## 附录 B：取证 SQL（复现用）

```sql
-- 阶段失败与降级
SELECT seq, event_type, payload_json FROM job_event
WHERE job_id='2afefe15-8ee4-4205-8756-2b6807c7d3bc' AND event_type LIKE '%.failed';
SELECT jsonb_pretty(error_json) FROM generation_job
WHERE project_id='01a7c8c2-09ea-409d-a397-f4601da2ecee' AND kind='full';

-- 证据层覆盖（每篇论文的证据/结构化产物）
SELECT le.bibtex_key,
       (SELECT count(*) FROM document_file df WHERE df.work_id=le.work_id)      AS files,
       (SELECT count(*) FROM structured_extraction se WHERE se.work_id=le.work_id) AS sx,
       (SELECT count(*) FROM evidence_unit e WHERE e.work_id=le.work_id)        AS units
FROM library_entry le
WHERE le.project_id='01a7c8c2-09ea-409d-a397-f4601da2ecee' AND le.status='selected'
ORDER BY units DESC;

-- 结构化实验数据是否为空（含跨领域污染）
SELECT left(w.canonical_title,60) AS title, se.payload_json->>'task' AS task,
       jsonb_array_length(coalesce(se.payload_json->'main_results','[]')) AS main_results,
       se.payload_json->'victim_models' AS victim_models
FROM structured_extraction se JOIN scholarly_work w ON w.id=se.work_id;
SELECT count(*) FROM experiment_result;              -- 期望 >0，实测 0

-- 证据路由发散度
SELECT n_questions, count(*) AS n_units FROM (
  SELECT l.evidence_unit_id, count(DISTINCT l.research_question_id) AS n_questions
  FROM question_evidence_link l JOIN research_question q ON q.id=l.research_question_id
  WHERE q.project_id='01a7c8c2-09ea-409d-a397-f4601da2ecee' GROUP BY 1) t
GROUP BY 1 ORDER BY 1;

-- 无关论文的入选路径
SELECT le.bibtex_key, le.relevance_score, jsonb_pretty(le.rank_reason_json)
FROM library_entry le
WHERE le.project_id='01a7c8c2-09ea-409d-a397-f4601da2ecee'
  AND le.bibtex_key IN ('chen2020rna','deshpande2024hybrid','deangeli2025ghostbuster');

-- 跨研究章节的证据契约缺失
SELECT s->>'key', jsonb_array_length(s->'cite_keys') AS n_cites,
       s ? 'question_id' AS has_question, s ? 'evidence_ids' AS has_evidence
FROM outline o, jsonb_array_elements(o.tree_json->'sections') s
WHERE o.project_id='01a7c8c2-09ea-409d-a397-f4601da2ecee';

-- s6 是确定性回退产物（model 为 NULL）
SELECT section_key, model, jsonb_array_length(cite_keys_json) AS n_cites
FROM paper_section WHERE document_id='e843d9d6-338a-49fd-a575-68e3afb0cb82' ORDER BY order_no;

-- 质量门是否运行
SELECT count(*) FROM quality_report        WHERE project_id='01a7c8c2-09ea-409d-a397-f4601da2ecee';
SELECT count(*) FROM claim_evidence_anchor WHERE project_id='01a7c8c2-09ea-409d-a397-f4601da2ecee';
```
