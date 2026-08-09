# 第二轮修复审阅：证据链路零产出根因与优化计划

> 状态：**已实施（N0–N6，2026-07-31）**；本文件保留审阅结论与验收标准
> 实施摘要：跨语言 QMATRIX 桥接 + 零候选降级、INGEST NUL/隔离、无证据不成文、本体数据化（recsys 种子）、SCREEN uncertain 隔离、A 级收紧、证据矩阵诊断面板、ledger key / R11 gate / tabularx
> 审阅基准：工作树 `/home/zhuzy/projects/paper-forge`，2026-07-31
> 取证对象：项目 `21016fe8-0b97-44f6-81d5-507750b252ab`「近3年序列推荐攻击的文献综述」（`paper_type=review`，`language=zh`）
> 取证任务：`73074ffd-c506-49fd-ac40-15cf7f7fa327`（`kind=full`，`status=succeeded`，2026-07-30 16:35–16:50 UTC）
> 取证来源：`paperforge-prod-postgres-1` 生产库 `job_event` / `evidence_unit` / `paper_section` / `search_run` 实际记录
> 前置文档：[`bgc_evidence_synthesis_quality_fix_plan.md`](bgc_evidence_synthesis_quality_fix_plan.md)（下称「上一轮计划」）
> 用户报告：证据矩阵板块几个功能不可用；没有抽取到任何证据；正文没有引用任何文章

**结论提要**：上一轮 M0「止血」**确实生效了**——EVIDENCE 阶段不再整段崩溃（证据覆盖率从 16% 升到 96.7%），质量门第一次真正运行（`quality_report` 1 行、`claim_evidence_anchor` 92 行），交付门第一次真正拦截（`readiness_status=needs_revision`）。

但本轮暴露了三个**新的**阻断性缺陷，它们叠加后使整条证据链在 QMATRIX 处彻底断流：

1. **QMATRIX 跨语言零召回**：M1 移除了 `or unit.kind in expected_kinds` 这条无条件放行分支，却没有补上任何跨语言桥接。中文子问题的词元集与英文证据的词元集**交集恒为 0**，实测 5 个子问题 × 332 条证据的最大 overlap = `0.0000`。`MIN_LEXICAL_SCORE=0.02` 这道门 100% 拒绝。这一条同时解释了用户报告的两个现象。
2. **INGEST 缺少 per-document 隔离**：M0 只给 EVIDENCE 补了单篇隔离，INGEST 里同类缺陷原封不动。一个 PDF 里的 `0x00` 字节抛出 `CharacterNotInRepertoireError`，直接终结整个 INGEST 阶段——21 篇已下载的全文只有 8 篇完成解析。
3. **无证据仍照写不误**：`allowed` 引用白名单为空时，写作阶段没有任何闸门，LLM 用参数化记忆写出 5323 字、0 引用的「综述」。证据缺口声明只存在于确定性回退分支，LLM 分支上不存在。

此外，上一轮 RC2 批评的「专题硬编码」问题**换了个领域重现**：`evidence.py` 现在硬编码 BGC（MIBiG / antiSMASH / DeepBGC / `bgc.*` task slug），而本轮测试是序列推荐攻击领域，于是 `task_id` / `topical_status` 全库为空、`experiment_result` 全库仅 1 行。`task_definition` 表已建好并种入 7 行，但**没有任何代码读它**。

---

## 1. 上一轮改动的验收核对

### 1.1 已生效（有实测支撑）

| 上一轮项 | 目标 | 本轮实测 | 判定 |
|---|---|---|---|
| M0-1 UUID → `str` + JSON-safe 编码 | EVIDENCE 不再抛 `TypeError` | `evidence.completed` 正常产出，无序列化异常 | ✅ |
| M0-2 per-work 隔离 + 覆盖率上报 | 单篇失败不终结阶段 | `works_ok=29, works_failed=0, coverage=0.9667`（上轮 4/27=16%） | ✅ |
| M0-3 `replace_claim_evidence` 去重 | 不再 `IntegrityError` | `claim_evidence_anchor` **92 行**（上轮 0 行） | ✅ |
| M0-4 删除字母序卡片摘要回退 | s6 不再逐篇罗列 | s6 `model=deepseek-v4-pro`（上轮 `NULL`），无卡片摘要段落 | ✅ |
| M0-5 阶段失败进 `readiness_status` | 降级可见 | `quality.blocked` → `evidence_stage_incomplete`，`readiness=needs_revision`，导出被拦 | ✅ |
| M1-8 `_terms` 中文二元组 + 去无条件放行 | 路由不再发散 | 代码已落地（`qmatrix.py:157-161,258-264`） | ✅ 落地，但见 §2.1 |
| M2-11 `comparability_key` 未知加盐 | 未知维度不相等 | `evidence.py:392-398` 传 `unknown_salt` | ✅ |
| M3-13 综合主表一篇一行 | 主表行数 == 论文数 | `outline.py:456-508` 按 `cite_key` 聚合 | ✅ 落地（本轮无数据可验） |
| M4/R9 引用闭包 | 章节引用 ⊆ 证据闭包 | `outline.py:341-347` 严格取自 bundle | ✅ 落地，但见 §2.3 |
| M5-15 检索式构造 | 12 条上限、纯英文、子问题驱动 | 6 条查询**全部纯英文**（上轮有中英混排），`hit_count=0` 的查询 = 0 | ✅ |
| M7-20/21 输出语言声明 + 缓存键含 language | 中文项目不出英文段 | 正文 9 节全中文，无英文段落 | ✅ |
| M5 SCREEN 阶段 | 纳入筛选可审计 | `eligibility_decision` 30 行落库 | ✅ 落地，但见 §2.6 |

**这一轮止血是成功的。** 上一轮 7 个症状里，「证据面窄」「护栏失效」「s6 逐篇罗列」「中英混排」四个的**直接机制**都已消除。

### 1.2 未生效或未实施

| 上一轮项 | 状态 | 实测证据 |
|---|---|---|
| M1-9 A 级证据收紧到 `structured_cell` | ❌ 未生效 | 15 条 `A_located_structured`，`anchor_strength` **全部为 `prose_only`**；全项目 332 条无一条 `structured_cell` / `object_mention` |
| M1-6/7 task 本体真正接线 | ❌ 未生效 | `research_question.task_id` 全 NULL；`project_task_profile` **0 行**；`evidence_unit.task_id` 全库 0 条非空 |
| M2-10 实验结构化去专题化 | ❌ 未生效 | `evidence.py` 硬编码 BGC；`experiment_result` **全库 1 行**、`evidence_measurement` 全库 8 行 |
| M3-12 `paper_evidence_rollup` | ❌ 未实施 | 全仓库无该表/模型/视图 |
| M5-16 anchor facet 强约束 / rerank 分批 | ❌ 未实施 | `rerank_with_llm` 仍单次整体调用；ranking 不区分 anchor / supporting facet |
| M5-17 `semantic_scholar` 修复 | ❌ 未生效 | 本轮 **6/6 全部 failed**（上轮 4/4 failed） |
| M6-18 `tabularx` | ⚠️ 改为 longtable | 列宽扣了列间距（`renderer.py:187-190`），但未用 `tabularx`，locator 列无限长 |
| M7-22 R11 语言一致性 | ⚠️ 部分 | `quality.py:682-711` 有实现，但挂在 `worker._quality` 后置，不在 `apply_readiness_gate()` 内 |

---

## 2. 本轮根因分析

### 2.1 N-RC1（阻断，最高优先）—— QMATRIX 跨语言词元交集恒为 0

这是用户报告的**两个问题的共同上游**。

`qmatrix.py:258-264` 的 `_terms` 对中文取二元组、对英文取单词：

```python
def _terms(text: str) -> set[str]:
    latin = re.findall(r"[a-z][a-z0-9@.-]{1,}", (text or "").casefold())
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", text or "")
    cjk_bigrams = [run[index : index + 2] for run in cjk_runs for index in range(len(run) - 1)]
    return set(latin + cjk_bigrams)
```

本项目的 5 个子问题是**纯中文**（无一个拉丁词元），30 篇文献是**纯英文**。于是：

```
question_terms  = {全中文二元组}      # 18–32 个，latin 词元数 = 0
unit_terms      = {全英文单词}        # latin 词元
overlap = |question_terms ∩ unit_terms| / |question_terms| ≡ 0
```

`_rank_candidates`（`qmatrix.py:160`）唯一的准入判据是：

```python
if overlap >= MIN_LEXICAL_SCORE:   # MIN_LEXICAL_SCORE = 0.02，qmatrix.py:20
    ranked.append((unit, score))
```

**实测验证**（对生产库 332 条证据文本逐条计算）：

| 子问题 | 词元数 | 其中拉丁词元 | 最大 overlap | 通过 `>=0.02` 的证据数 |
|---|---|---|---|---|
| 主要攻击方法…技术原理和假设 | 27 | **0** | **0.0000** | **0** |
| 数据集和评价指标…效力如何比较 | 32 | **0** | **0.0000** | **0** |
| 防御或鲁棒训练策略 | 26 | **0** | **0.0000** | **0** |
| 攻击者的能力、知识和预算 | 27 | **0** | **0.0000** | **0** |
| 不足与未来方向 | 18 | **0** | **0.0000** | **0** |

与 `job_event` seq 31 完全吻合：`{"links": 0, "candidates": 0, "rejected_by_task": 0}`。

注意 `rejected_by_task=0`——task 硬约束**没有**开火（`question.task_id` 为 NULL 时 `qmatrix.py:149` 短路）。**杀死候选的纯粹是这道词法阈值。**

**为什么上一轮没暴露**：上一轮的 BGC 项目同样是中文问题 + 英文证据，overlap 同样是 0，但当时 `qmatrix.py:146` 写的是

```python
if overlap >= MIN_LEXICAL_SCORE or unit.kind in expected_kinds:
```

而 `_evidence_kind` 把含数字的片段一律判为 `experimental_fact`，子问题的 `expected_evidence_kinds` 也都含 `experimental_fact`——所以**所有证据都从 `or` 分支无条件放行**。上一轮计划正确地诊断出这是「无条件放行」，也正确地删掉了它；但**删掉的同时也删掉了跨语言场景下唯一的通路**。一个「全通过」的 bug 被改成了「全拒绝」的 bug。

**连锁后果**（全部实测）：

```
qmatrix   links=0, candidates=0
   ↓
synth     5/5 answer_status=insufficient_evidence, comparison_clusters=0
   ↓
outline   9 个 section 全部 cite_keys=[] / evidence_ids=[]；evidence_ledger_section 返回 None
   ↓
write     writing.py:234  allowed = {} （空集）
   ↓
成稿      5323 字，cite_key_count=0，9/9 节 model=deepseek-v4-pro 但零引用
   ↓
quality   no_citations × 6, unused_library(30 篇未被引用)
```

**这就是「没有抽取到任何证据」和「正文没有引用任何文章」的完整因果链。** 注意措辞上的区分：证据**抽出来了**（332 条落库），断在了**证据与问题的对齐**这一步。

### 2.2 N-RC2（阻断）—— INGEST 无 per-document 隔离，且不清洗 NUL 字节

`job_event` seq 13：

```
ingest.failed  DBAPIError
  asyncpg.exceptions.CharacterNotInRepertoireError:
  invalid byte sequence for encoding "UTF8": 0x00
  [SQL: UPDATE ...]
```

两处缺陷叠加：

1. **不清洗**：`fulltext.py:191` 把 `parsed.text[:MAX_FULLTEXT_CHARS]` 直接赋给 `DocumentParse.extracted_text`，`fulltext.py:293/296` 把 `chunk.text` 直接赋给 `DocumentChunk.text`。PDF 文本抽取器在遇到损坏字体表/嵌入对象时会产出 `\x00`，Postgres 的 `text` 类型**不接受 0x00**。全仓库 `packages/ingest` 内 grep 不到任何 NUL 过滤。
2. **不隔离**：`fulltext.py:140` 的 `for document in result.documents:` 主循环**没有 per-document `try/except`**——与上一轮 RC1 在 EVIDENCE 里的缺陷**完全同型**。M0 只修了 EVIDENCE 那一处。

**实测代价**：

| 指标 | 实测 |
|---|---|
| `fulltext_attempt` status=acquired | **21** |
| `document_parse` status=parsed | **8** |
| `literature_card` `fulltext_used=true` | **7 / 30** |
| 证据等级分布 | `D_abstract_only` **219**（66%）/ `B_located_prose` 91 / `A_located_structured` 15 / `C` 7 |

**13 篇已经下载完成的 PDF 因为另一篇 PDF 里的一个字节而没有被解析。** 即便 N-RC1 修好，证据层也有三分之二只是摘要句。

### 2.3 N-RC3（阻断）—— 引用白名单为空时，写作阶段没有闸门

`writing.py:234`：

```python
allowed = {key for key in section.get("cite_keys", []) if key in whitelist}
```

outline 给出 `cite_keys=[]` → `allowed` 为空集。随后 `writing.py:304-306` 把这件事**如实告诉模型**：

```python
f"\n只能使用：{', '.join(sorted(allowed)) or '（无可用引用键，请给空数组）'}"
```

`_evidence_context_block`（`writing.py:574-575`）返回字符串 `"(no question-aligned evidence)"`。**然后就没有然后了**——没有任何代码检查「`allowed` 为空是否还应该调用 LLM 写这一节」。模型收到一个题目、一段「没有证据」的说明，于是凭参数化记忆写出通顺的教科书式段落。

证据缺口声明**只存在于确定性回退分支**（`writing.py:1003-1016` 的 `deterministic_paragraphs`），该分支仅在 `runner.enabled == False` 或 LLM 调用失败时才走到。本轮 LLM 一次都没失败，所以缺口声明一次都没输出。

实测成稿 s1 开头确实写了「……全文证据严重不足」（这是模型自己顺着 prompt 说的，不是系统保证的），但紧接着照样写了 550 字无源论述；s6 全节 420 字、`stance_summary` 全部是 `background`、0 引用。

**这比上一轮的「逐篇罗列卡片摘要」更危险**：上一轮的垃圾一眼可辨且每段都挂着 cite_key；本轮的产物是**流畅、可信、完全不可溯源**的文本。

### 2.4 N-RC4 —— 专题硬编码换了个领域重现；`task_definition` 建好了但没人读

上一轮计划 §2.2 批评 `_structured_payload` 硬编码「序列推荐投毒攻击」，并给出原则：「`task_definition` 是**数据**不是代码」。本轮实现把硬编码**从 recsys 换成了 BGC**，而没有改成数据驱动：

| 位置 | 硬编码内容 |
|---|---|
| `evidence.py:503-513` `_topical_status` | on-topic 线索 = `biosynthetic gene cluster` / `bgc` / `secondary metabolite`；off-topic = `rna secondary structure` / `histopathology` / `hepatocellular carcinoma` |
| `evidence.py:516-528` `_infer_task_id` | 不含 BGC 线索直接 `return None`；否则返回 `bgc.*` slug |
| `evidence.py:434` datasets 兜底正则 | `MIBiG` / `antiSMASH-DB` / `IMG-ABC` |
| `evidence.py:446` baselines 正则 | `antiSMASH` / `DeepBGC` / `GECCO` / `clusterFinder` / `BiG-SLiCE` |
| `evidence.py:540-542` `_dataset_version` | 只认 `MIBiG` 版本号 |
| `evidence.py:567-577` `_split_strategy` | `leave-one-genome-out` 等基因组学划分 |
| `qdecomp.py:76-93` `_infer_task_id` | 不含 `bgc`/`biosynthetic`/`生物合成`/`基因簇` 直接 `return ""` |
| 迁移 `0011_task_ontology.py:22-80` | 只种了 7 行，`domain` **全部为 `bgc`**；无 `recsys_attack` |

**在序列推荐攻击这个领域，上述每一条都恒定落空**，实测后果：

- `evidence_unit.task_id`：全库 **684 条全部 NULL**
- `evidence_unit.topical_status`：全部 `uncertain`（既没识别出 on-topic，也没识别出 off-topic）
- `research_question.task_id`：全部 NULL → `project_task_profile` **0 行** → R7 任务本体归属**从未执行**
- `experiment_result`：**全库 1 行**；`evidence_measurement` 全库 8 行；本项目 0 行
- 数据集/指标白名单没有 `HR@K`、`NDCG@K`、攻击成功率(ASR)、MovieLens、Amazon Beauty、Steam、Yelp——而这些正是本领域的主力维度

**根本问题**：`task_definition` / `project_task_profile` 两张表、`evidence_unit.task_id` 外键、`qmatrix` 的 task 硬约束**全部建好了**，但没有任何一段代码从表里 `SELECT`。整套本体是一具空壳。

### 2.5 N-RC5 —— 证据等级通胀（上一轮 RC7）在数据上完全没有改善

`evidence.py:601-610`，卡片 `quotable_points` 分支构造候选时：

```python
candidates.append(
    EvidenceCandidate(
        text=text[:MAX_EVIDENCE_TEXT_CHARS],
        page=page,
        section_path=section,
        paragraph_index=paragraph,
        object_ref=object_ref,                      # ← 来自散文正则 _object_reference(text)
        grade=_fulltext_grade(page, section, paragraph, object_ref),
    )                                                # ← 没有传 anchor_strength，落默认 "prose_only"
)
```

而 `_fulltext_grade`（`evidence.py:746-756`）**完全不看 `anchor_strength`**：

```python
if object_ref and (page or section):
    return "A_located_structured"
```

于是一句「as shown in Figure 2」仍然把普通散文升级为 A 级。M1-9 声称「A 级只给 `structured_cell`」，但这个约束**没有写进 `_fulltext_grade`**。

**实测**：本项目 15 条 `A_located_structured`，`anchor_strength` **100% 是 `prose_only`**。全项目 332 条证据里 `structured_cell` 和 `object_mention` 各 0 条。上一轮 §7.1 指标 16「A 级证据真实性 ≥ 90%」实测为 **0%**。

### 2.6 N-RC6 —— SCREEN 有判定无执行；纳入库 63% 离题

`screen.py:44-65` 的三态判定本身工作正常，但 `uncertain` **不做任何事**：

```python
if entry.user_pinned:
    outcome.pinned_preserved += 1
elif decision == "exclude":
    await set_entry_status(session, entry, "excluded")
# uncertain 既不排除，也不降权，也不阻止其进入证据抽取
```

**实测**：`include=8`（`anchor_facet_hit=true`）、`uncertain=22`（`anchor_facet_hit=false`）、`exclude=0`。22 篇没命中任何 anchor facet 的论文**原样留在 selected 里**，全部进了 CARDS 和 EVIDENCE，贡献了绝大部分 219 条 `D_abstract_only`。

人工核对 30 篇 selected（按 `relevance_score` 降序）：

| 区间 | 篇数 | 主题 |
|---|---|---|
| 0.53–0.64 | 11 | ✅ 真正相关（Sim4Rec、DARTS、Diversity-aware Poisoning、DV-FSR、LoRec、Profile Pollution…） |
| 0.26–0.34 | **19** | ❌ 离题：云 IDS 对抗防御、GCC 金融欺诈检测、IoT 视觉系统、乳腺癌影像、以太坊区块链、提示词注入、图像/视频质量评价、自动驾驶轨迹后门…… |

**off-topic 入库率 = 19/30 = 63%**（上一轮 11%，目标 ≤3%）——比上一轮**更差**。

配套原因在 `ranking.py:176`：

```python
take(eligible, limit)     # 前三档配额填不满时，无条件填到 limit=30
```

`eligible` 的门槛只有 `AUTO_SELECT_MIN_TOPIC_EVIDENCE = 0.15`，而这批离题论文得分 0.26+，全部够格。**没有绝对分数下限，也没有「宁缺毋滥」的收敛机制**：真正相关的只有 11 篇，系统硬凑到 30 篇。

另：`semantic_scholar` 本轮 **6/6 全部 failed**，M5-17 未生效。

### 2.7 N-RC7 —— 句子降级留下空 run 与悬空连接词

`writing.py:895-904`，句子不满足 R4/R6 时被抹白：

```python
else:
    sentence["text"] = ""
    sentence["cite_keys"] = []
    sentence["evidence_ids"] = []
    sentence["downgraded_reason"] = "R6_locator_missing" if not located else "R4_grade_missing"
```

`writing.py:905-910` 重算 `paragraph["text"]` 时**过滤掉了**空句，但 `to_ir_section`（`writing.py:131-149`）遍历的是 `sentence_rows` 原始列表：

```python
for index, sentence in enumerate(sentence_rows):
    runs.append(TextRun(v=str(sentence.get("text") or "")))   # ← 空串照样成为一个 run
    ...
    if index < len(sentence_rows) - 1:
        runs.append(TextRun(v=" "))                            # ← 再补一个分隔空格
```

**实测**（`paper_section.body_ir_json`）：

| 章节 | 被抹白的句子 | 保留的句子 | 抹白率 |
|---|---|---|---|
| introduction | 3 | 10 | 23% |
| s1 | 2 | 10 | 17% |
| s2 | 3 | 8 | 27% |
| s3 | 5 | 11 | 31% |
| s4 | 4 | 20 | 17% |
| s5 | 2 | 9 | 18% |
| **s6** | **4** | **8** | **33%** |
| conclusion | 1 | 12 | 8% |
| **合计** | **24** | **92** | **21%** |

后果不只是冗余节点。s6 实际产出：

```
段落2 runs = ["", " ", "", " ", "类似地，攻击预算的度量单位尚未统一，……"]
```

**「类似地」的前指句被静默删除了**，读者看到一个没有先行语的类比连接词。删句时既不修复语篇连接，也不合并/删除空段落。

### 2.8 N-RC8 —— 证据矩阵面板：接线正确，但空态即等于「功能不可用」

排查结论：**这不是前后端接线 bug。** 七个交互控件全部对应真实且已实现的 FastAPI 端点；`list_evidence_units`（`packages/db/db/repositories/evidence.py:171-178`）通过 `library_entry` 关联项目并显式允许 `evidence_unit.project_id IS NULL`，取数逻辑是对的。

用户感知到「不可用」的真实成因是下面五条：

| # | 现象 | 机制 |
|---|---|---|
| 1 | 立场下拉、条件说明输入、保存按钮**根本不存在** | 这三个是**逐行控件**（`evidence-matrix-workbench.tsx:261-303`），渲染在 `links.map(...)` 内。`question_evidence_link=0` → 0 行 → 0 个控件。用户看到的是「按钮没了」，不是「按钮点了没反应」 |
| 2 | 面板不在用户访问的页面上 | 证据矩阵在 `/projects/<id>/evidence`；用户给的链接是 `/projects/<id>/write`。write 页只用 `getEvidenceUnits` 给引用角标做 tooltip |
| 3 | 「重算综合」点了像没反应 | `POST /synthesis/generate` 存在且能跑，但**全仓库没有任何 GET synthesis 端点**，矩阵页也不显示 `answer_status` 或 bundle。任务跑完 UI 无任何可见变化 |
| 4 | 空态文案误导 | `getEvidenceMatrix` 用 `withFallback(..., undefined)`，`load()` 没有 `.catch()`。「后端报错」和「尚未生成」显示**同一句话**：「尚未生成问题—证据矩阵」 |
| 5 | 「332 条证据未对齐」只报数不报因 | 徽标（`:117-119,191`）能算出未对齐数，但没有任何界面告诉用户**为什么**被拒（overlap 不足？task 不符？等级不够？） |

换句话说：**证据矩阵面板忠实地反映了后端 0 链接的事实**，它缺的是「零结果可诊断性」，而不是接线。

---

## 3. 新增/修订不变量

沿用上一轮 R1–R12，补充：

| 编号 | 名称 | 定义 |
|---|---|---|
| **R13** | 跨语言可路由 | 证据路由不得依赖问题与证据同语言。项目语言 ≠ 证据语言时，必须存在显式桥接（问题英文检索式 / 对比维度术语 / 向量召回），且 QMATRIX 必须上报 `bridge_source`。 |
| **R14** | 阶段级 per-item 隔离 | 任何「按论文/文档循环」的阶段（ingest / cards / evidence / qmatrix）都必须有 per-item `try/except`，单项失败只记 `<stage>.item_failed` 并继续；阶段仅在**成功率低于阈值**时判失败。 |
| **R15** | 无证据不成文 | `allowed` 引用白名单为空的正文章节，**禁止**调用 LLM 自由写作；只能输出证据缺口骨架。`scholarly` profile 下计 blocker。 |
| **R16** | 本体数据化 | 任何领域词表（task / 指标 / 数据集 / 基线 / 排除线索）只能来自 `task_definition` 等数据表，代码中不得出现领域字面量。新增 lint 断言。 |
| **R17** | 存储层字符安全 | 写入 Postgres `text`/`jsonb` 的字符串一律先过 `sanitize_pg_text()`（剥离 `\x00` 及其他非法控制符）。 |
| **R18** | 段落完整性 | 句子被降级删除后，段落不得留下空 run、连续空格或悬空连接词；整段被删空则移除该段。 |

---

## 4. 优化计划

### N0 —— 拆弹（0.5–1 天，P0，无迁移，约 120 行）

目标：让**同一个项目重跑**就能产出非零证据链接和带引用的正文。

| # | 改动 | 文件:行 | 说明 |
|---|---|---|---|
| N0-1 | **跨语言桥接（最小可用版）** | `qmatrix.py:139,258-264` | `_terms(question)` 改为消费 `question.text` + `comparison_dimensions_json` + （新增列）`search_query`。本项目 `comparison_dimensions` 里已含 `Movielens`、`Amazon`、`HR`、`NDCG` 等拉丁术语，立即产生非零 overlap |
| N0-2 | **零候选熔断 + 降级放行** | `qmatrix.py:88-92` | 某子问题 `candidates == 0` 时：① 记 `qmatrix.zero_candidates` warning；② 按 `grade` + `kind` 取 top-K 作为**降级候选**交给 LLM 判定（LLM 天然跨语言），并在 link 上标 `routing_mode="degraded_lexical_bypass"`。**绝不静默产出 0** |
| N0-3 | **NUL 清洗** | 新增 `packages/db/db/text_safety.py`；`fulltext.py:191,273,296` | `sanitize_pg_text()` 剥离 `\x00` 与非法控制符；`DocumentParse.extracted_text` / `DocumentChunk.text` / `EvidenceUnit.text` 写入前统一过滤 |
| N0-4 | **INGEST per-document 隔离** | `fulltext.py:140` | 主循环包 `try/except`，单文档失败记 `ingest.document_failed` 并继续；阶段末上报 `documents_ok / documents_failed / parse_coverage` |
| N0-5 | **无证据不成文闸门** | `writing.py:234-278` | `allowed` 为空且 `section.kind == "body"` 时**跳过 LLM**，直接走 `deterministic_paragraphs` 的证据缺口分支；`generator` 记为 `evidence_gap_skeleton` |
| N0-6 | **空 run 与悬空句清理** | `writing.py:905-911,131-149` | `enforce_sentence_evidence_rules` 末尾**物理移除** `text==""` 的句子；`to_ir_section` 跳过空 run；整段为空则不生成 ParagraphBlock |

**验收**（同一项目 `21016fe8` 重跑）：

- `question_evidence_link > 0`，且 5 个子问题**没有一个**为 0
- `document_parse status=parsed` == `fulltext_attempt status=acquired`（本轮 8 / 21）
- 正文 `cite_key_count > 0`；`no_citations` 告警 ≤ 1
- `body_ir_json` 中 `text` 为空串的 run 数 = 0（本轮 24）
- 无证据章节输出证据缺口骨架而非无源论述

> **这是本轮唯一应当立刻做的事。** N0-1/N0-2 两条约 40 行，直接解开用户报告的两个问题。**拿到重跑结果之前不要开始 N2 之后的任何工作。**

### N1 —— 证据路由的正确解法（2–3 天，P0）

N0-1 是权宜之计（依赖 LLM 恰好在对比维度里写了英文术语）。正解是让子问题自带英文检索面。

- `research_question` 增列 `search_query`（英文检索式）、`term_aliases_json`（中英术语对照）。SCOPE 的 `sub_questions` schema 已预留 `search_query`（`scope.py:385` 已在消费），只是没落库、且 LLM 未稳定产出——在 SCOPE prompt 中改为**必填字段**。
- QMATRIX 打分改为三路并集：中文二元组 ∩ 中文证据、英文词元 ∩ 英文证据、**术语别名跨语言映射**。上报 `bridge_source` 满足 R13。
- 候选检索从纯词法改为「词法 + grade + kind」的加权召回，`MIN_LEXICAL_SCORE` 从硬阈值改为**相对阈值**（取该问题候选分布的分位数），避免绝对阈值在新语言/新领域上再次全拒。
- 全阶段推广 R14：`cards` / `evidence` / `qmatrix` 统一 per-item 隔离与成功率阈值判定。

**验收**：`bridge_source` 分布可见；单条证据平均归属子问题数 ≤ 1.3；无子问题命中 `MAX_CANDIDATES_PER_QUESTION` 上限；无子问题为 0。

### N2 —— 本体数据化（3–5 天，P0）

兑现上一轮 M1/M2 的原始意图：**把领域知识从代码搬进 `task_definition`**。

- `task_definition` 补种 `recsys_attack` domain：`recsys.poisoning_attack` / `recsys.profile_pollution` / `recsys.model_extraction` / `recsys.adversarial_defense` / `recsys.robust_training` / `benchmark.dataset_construction`，带 `metric_whitelist_json`（`HR@K` / `NDCG@K` / `MRR` / `ASR` / `ER@K` / `Recall@K`）、`dataset_whitelist_json`（MovieLens-1M/20M、Amazon Beauty/Books/Games、Steam、Yelp、LastFM）、`exclusion_cues_json`（`intrusion detection`、`histopathology`、`blockchain`、`prompt injection`、`image quality assessment`…）。
- `evidence.py` 的 `_topical_status` / `_infer_task_id` / `_structured_payload` 的 datasets/baselines/split 正则**全部改为从表加载**，按 `project_task_profile` 选定 domain。删除所有 BGC 字面量。
- `qdecomp.py:76-93` `_infer_task_id` 同样改为查表；SCOPE prompt 增加 `task_id` 字段并给出候选 slug 清单。
- 新增 lint：`evidence.py` / `qdecomp.py` / `ranking.py` 内出现领域字面量（`MIBiG`/`antiSMASH`/`MovieLens`…）即 CI 失败（R16）。
- `_METRIC_RE` 由 `metric_whitelist_json` 动态编译；放宽指标—数值邻接（支持 `HR@20 of 0.184`、表格单元格、括号、中文「达到/为」）。

**验收**：`evidence_unit.task_id` 非空率 ≥ 80%；`topical_status` 非 `uncertain` 率 ≥ 80%；本项目 `experiment_result ≥ 60` 行；换领域只需新增数据行，代码零改动（用 BGC + recsys 两套种子做回归）。

### N3 —— 纳入筛选真正生效（2–3 天，P0）

- SCREEN 的 `uncertain` 从「无操作」改为**降权 + 隔离**：`library_entry.status='candidate_uncertain'`，不进 CARDS/EVIDENCE，前端「纳入筛选」视图可一键提升。
- `ranking.py:176` 的兜底 `take(eligible, limit)` 加**绝对分数下限**与 anchor facet 强约束：宁可只选 11 篇，不要凑满 30 篇。选够数量不是目标。
- `rerank_with_llm` 分批（每批 ≤20 候选），截断后按批重试而非整体放弃。
- 修 `semantic_scholar` 适配器（连续两轮 100% 失败）。

**验收**：off-topic 入库率 ≤ 3%（人工抽检）；`unused_library` 告警篇数 ≤ 纳入数的 20%；provider 成功率 ≥ 4/5。

### N4 —— 证据分级真实性（1–2 天，P1）

- `_fulltext_grade` 签名加 `anchor_strength`，`A_located_structured` **只在 `structured_cell` 时返回**（兑现 M1-9）。
- `evidence.py:601-610` 卡片分支显式传 `anchor_strength`：`quotable_points` 带结构化 locator 时给 `object_mention`，否则 `prose_only`。
- 表格/公式单元格抽取路径产出 `structured_cell`。

**验收**：A 级证据中 `anchor_strength=structured_cell` 占比 ≥ 90%（本轮 0%）。

### N5 —— 证据矩阵面板可诊断（2–3 天，P1）

面板的问题是**看不见为什么是空的**，因此优先做可观测性而非重写。

- QMATRIX 落库**拒绝原因统计**（`rejected_by_lexical` / `rejected_by_task` / `rejected_by_grade` / 各问题 top 候选分数分布），经 `GET /evidence-matrix` 返回。
- 面板空态从一句话改为**诊断卡片**：显示各阶段计数（证据 332 / 候选 0 / 链接 0）、最高 overlap 值、被拒主因，并给出「降级重建对齐」按钮。
- 新增 `GET /projects/{id}/synthesis`，面板展示各子问题 `answer_status` 与比较簇，让「重算综合」有可见结果。
- `load()` 补 `.catch()`；区分「后端错误」与「尚未生成」两种空态。
- `apps/web/lib/labels.ts` 补 grade / stance / anchor_strength / task 的人读标签（R10 前端侧）。
- 在 `/write` 页的引用面板加一条指向 `/evidence` 的入口，避免用户在错误页面找证据矩阵。

**验收**：零链接时用户能在面板内直接看到断点阶段与主因；「重算综合」有可见输出。

### N6 —— 承接上一轮未完成项（P2，N0–N3 稳定后再评估）

`paper_evidence_rollup`（M3-12）、`tabularx`（M6-18）、Overfull 双实现合并（M6-19）、R11 并入 `apply_readiness_gate`（M7-22）、evidence ledger 章节 key 不被 `_with_frame_sections` 重写为 `sN`。

**这些都不是当前瓶颈**，在证据链跑通前投入产出比很低。

---

## 5. 回归测试补强

上一轮列出的单测大多已存在（12 个测试文件），但**恰好漏掉了本轮所有故障点**。必须补：

| 用例 | 断言 |
|---|---|
| `test_qmatrix_cross_language` | 中文子问题 + 英文证据 fixture（用本项目真实数据），断言 `candidates > 0`——**这是本轮头号缺陷的直接回归** |
| `test_qmatrix_zero_candidate_fallback` | 词法全 0 时走降级路径并打 `routing_mode` 标记 |
| `test_sanitize_pg_text` | `\x00` 及非法控制符被剥离 |
| `test_ingest_document_isolation` | 一个文档抛异常，其余文档仍完成解析 |
| `test_writing_skips_llm_without_citekeys` | `allowed=set()` 时不调用 runner，输出证据缺口骨架 |
| `test_ir_has_no_empty_runs` | 降级删句后 IR 无空 run、无悬空段落 |
| `test_grade_requires_structured_cell` | `anchor_strength=prose_only` 永不产出 A 级 |
| `test_screen_uncertain_is_isolated` | `uncertain` 不进入 CARDS/EVIDENCE |
| `test_domain_ontology_is_data_driven` | 用 recsys 种子跑 evidence 抽取，`task_id` 非空；代码中无领域字面量（lint） |
| `test_search_queries_reject_mixed_cjk` | 上一轮就要求但未补：`基于序列特征的BGC识别模型` 被拒 |

**执行环境**：按既有约定，本机 `uv run pytest` / `pnpm test` 不可用，须走一次性容器（`paperforge-worker:local` + 独立 `paperforge-postgres:local`，`PYTHONPATH` 覆盖 `packages/*` 与 `services/*`，`-p no:cacheprovider`）。容器用后无法停止/删除，需向用户说明遗留。

---

## 6. 指标基线对照

| # | 指标 | 上一轮(BGC) | **本轮(recsys)** | 目标 |
|---|---|---|---|---|
| 1 | 证据层覆盖率 | 4/25 = 16% | **29/30 = 96.7%** ✅ | ≥ 90% |
| 1b | 全文解析成功率 | — | **8/21 = 38%** ❌ | 100% |
| 1c | 证据中 D 级占比 | — | **219/332 = 66%** ❌ | ≤ 30% |
| 2 | `experiment_result` 行数 | 0 | **1（全库）** ❌ | ≥ 60 |
| 5 | 主表分层正确性 | 27 行/4 篇 | 无数据 | == 1.0 |
| 6 | 内部标签泄漏 | ≥27 | **0** ✅ | 0 |
| 7 | 证据路由发散度 | 2.33 | **N/A（0 链接）** ❌ | ≤ 1.3 |
| 7b | **问题—证据链接数** | 63 | **0** ❌ | > 0 |
| 8 | 任务不符路由率 | 不可测 | 不可测（task_id 全 NULL） | 0 |
| 9 | off-topic 入库率 | ≥11% | **63%** ❌ 恶化 | ≤ 3% |
| 10 | 逐篇罗列段落比例 | s6 100% | **0%** ✅ | ≤ 15% |
| 11 | 跨文献综合段落比例 | 0% | **0%**（无引用可综合） ❌ | ≥ 60% |
| 11b | **正文引用数** | 少量 | **0** ❌ | > 0 |
| 12 | 语言一致性（中文项目英文段） | 3 | **0** ✅ | 0 |
| 13 | 质量门可用性 | 0 行 | **1 行** ✅ | 1 行/次 |
| 14 | 比较簇数 | 0 | **0** ❌ | ≥ 3 |
| 16 | A 级证据真实性 | 不可测 | **0%** ❌ | ≥ 90% |
| 17 | **降级句占比（新增）** | — | **24/116 = 21%** ❌ | ≤ 5% |

**净变化**：6 项转好（其中「证据覆盖率」「质量门」「逐篇罗列」是质变），2 项持平，**3 项恶化**（路由链接、off-topic 率、正文引用）。恶化项全部可归因到 N-RC1、N-RC6 两个根因。

---

## 7. 推荐路线

1. **只做 N0，立刻重跑，再决定后续。** 本轮两个用户可见症状挂在 `qmatrix.py` 约 40 行上。在拿到重跑数据前做 N2 之后的任何工作，大概率是在为一个已消失的问题做设计——这条教训上一轮已经验证过一次，本轮依然成立。
2. **N0-2 的「零候选熔断」比 N0-1 的桥接更重要。** 桥接总会在某个新语言/新领域组合上再次失效；「任何阶段都不许静默产出 0」是结构性防线。本轮的教训是：一个 `>=` 阈值就能让整条链路无声归零，而 `job.status` 仍是 `succeeded`。
3. **N-RC2 与上一轮 RC1 是同一个 bug 的两个实例。** 修复时不要只补 INGEST，应把 R14（per-item 隔离）作为所有循环阶段的统一约定，并加架构测试防止第三次出现。
4. **N-RC4 说明「建表 ≠ 落地」。** `task_definition` / `project_task_profile` / `evidence_unit.task_id` 全部存在却无人读写。后续验收标准应从「表/列是否存在」改为「**是否有非默认数据流经**」。
5. **N-RC3 是本轮最隐蔽的风险。** 上一轮的失败产物一眼可辨（英文原文、逐篇罗列、内部枚举印进 PDF）；本轮的失败产物是流畅可信的无源中文。**质量门只报了 `no_citations` 警告，却让 5323 字无源正文正常入库。** R15 应作为 `scholarly` profile 的硬 blocker。
6. **证据矩阵面板先做可诊断，不要重写。** 接线是对的，取数是对的。用户说「功能不可用」，实际是「没有数据所以控件不渲染」+「空态不解释原因」。

---

## 附录 A：关键源码索引（本轮）

| 主题 | 位置 |
|---|---|
| 跨语言词元交集恒 0（N-RC1） | `pipelines/qmatrix.py:258-264` `_terms`；准入判据 `:160`；阈值 `:20`；调用 `:139` |
| 零候选静默跳过 | `pipelines/qmatrix.py:91-92` |
| INGEST 无 per-document 隔离（N-RC2） | `pipelines/fulltext.py:140` |
| NUL 未清洗 | `pipelines/fulltext.py:191`（`extracted_text`）、`:273`、`:296`（`chunk.text`） |
| 空白名单仍调 LLM（N-RC3） | `pipelines/writing.py:234`；告知模型 `:304-306`；缺口声明仅在 `:1003-1016` |
| 证据上下文空态 | `pipelines/writing.py:574-575` |
| BGC 硬编码（N-RC4） | `pipelines/evidence.py:503-513`、`:516-528`、`:434`、`:446`、`:540-542`、`:567-577` |
| QDECOMP task 推断硬编码 | `pipelines/qdecomp.py:76-93` |
| task 本体种子（仅 bgc domain） | `packages/db/migrations/versions/0011_task_ontology.py:22-80` |
| `project_id=None` 写入（跨项目复用，非缺陷但需确认语义） | `pipelines/evidence.py:222-223` |
| 等级通胀未修（N-RC5） | `pipelines/evidence.py:746-756` `_fulltext_grade`；卡片分支缺 `anchor_strength` `:601-610` |
| SCREEN uncertain 无执行（N-RC6） | `pipelines/screen.py:44-47`、`:60-65` |
| 兜底填满配额 | `pipelines/ranking.py:176`；阈值 `:128` |
| 抹白句留空 run（N-RC7） | `pipelines/writing.py:895-904`；IR 生成 `:131-149` |
| 证据矩阵面板 | `apps/web/components/evidence/evidence-matrix-workbench.tsx:117-119,163-193,209-316` |
| 矩阵取数（正确，允许 project_id NULL） | `packages/db/db/repositories/evidence.py:171-178` |
| 矩阵 API | `services/api/paperforge_api/routers/writing.py:334-349,352-370,373-391,394-425` |
| 引用闭包（R9，行为正确） | `pipelines/outline.py:341-347` |

## 附录 B：取证 SQL（复现用）

```sql
\set proj '21016fe8-0b97-44f6-81d5-507750b252ab'
\set job  '73074ffd-c506-49fd-ac40-15cf7f7fa327'

-- 阶段时间线与失败点（seq 13 = ingest.failed）
SELECT seq, event_type, left(payload_json::text, 200)
FROM job_event WHERE job_id = :'job' ORDER BY seq;

-- N-RC1：证据抽到了，但一条也没对齐
SELECT
  (SELECT count(*) FROM evidence_unit eu
     JOIN library_entry le ON le.work_id = eu.work_id
    WHERE le.project_id = :'proj' AND le.status = 'selected')      AS evidence_units,   -- 332
  (SELECT count(*) FROM question_evidence_link l
     JOIN research_question q ON q.id = l.research_question_id
    WHERE q.project_id = :'proj')                                  AS links;            -- 0

-- N-RC2：下载了 21 份，只解析了 8 份
SELECT (SELECT count(*) FROM fulltext_attempt
         WHERE project_id = :'proj' AND status = 'acquired')       AS acquired,         -- 21
       (SELECT count(*) FROM document_parse dp
          JOIN document_file df ON df.id = dp.document_file_id
          JOIN library_entry le ON le.work_id = df.work_id
         WHERE le.project_id = :'proj' AND le.status = 'selected'
           AND dp.status = 'parsed')                               AS parsed;           -- 8

-- N-RC3：全部章节 LLM 生成，全部零引用
SELECT section_key, model, jsonb_array_length(coalesce(cite_keys_json, '[]')) AS n_cites
FROM paper_section
WHERE document_id = (SELECT id FROM paper_document WHERE project_id = :'proj' LIMIT 1)
ORDER BY order_no;

-- N-RC4：本体全空
SELECT count(*) FILTER (WHERE task_id IS NOT NULL)        AS with_task,        -- 0
       count(*) FILTER (WHERE topical_status <> 'uncertain') AS with_topic,    -- 0
       count(*)                                            AS total
FROM evidence_unit;
SELECT count(*) FROM project_task_profile WHERE project_id = :'proj';          -- 0
SELECT count(*) FROM experiment_result;                                        -- 1 (全库)
SELECT domain, count(*) FROM task_definition GROUP BY 1;                       -- bgc | 7

-- N-RC5：A 级证据的 anchor_strength 全是 prose_only
SELECT eu.grade, eu.anchor_strength, count(*)
FROM evidence_unit eu
  JOIN library_entry le ON le.work_id = eu.work_id
 WHERE le.project_id = :'proj' AND le.status = 'selected'
 GROUP BY 1, 2 ORDER BY 3 DESC;

-- N-RC6：SCREEN 判定 vs 实际纳入
SELECT decision, anchor_facet_hit, count(*)
FROM eligibility_decision WHERE project_id = :'proj' GROUP BY 1, 2;            -- include 8 / uncertain 22
SELECT le.bibtex_key, round(le.relevance_score::numeric, 3), left(w.canonical_title, 60)
FROM library_entry le JOIN scholarly_work w ON w.id = le.work_id
WHERE le.project_id = :'proj' AND le.status = 'selected'
ORDER BY le.relevance_score DESC;

-- N-RC7：被抹白的句子留在 IR 里
SELECT section_key,
  (SELECT count(*) FROM jsonb_array_elements(body_ir_json->'blocks') b,
                        jsonb_array_elements(b->'runs') r
    WHERE r->>'t' = 'text' AND r->>'v' = '')                       AS blanked,
  (SELECT count(*) FROM jsonb_array_elements(body_ir_json->'blocks') b,
                        jsonb_array_elements(b->'runs') r
    WHERE r->>'t' = 'text' AND btrim(r->>'v') <> '')               AS kept
FROM paper_section
WHERE document_id = (SELECT id FROM paper_document WHERE project_id = :'proj' LIMIT 1)
ORDER BY order_no;

-- 检索 provider 健康度（semantic_scholar 连续两轮全败）
SELECT provider, status, count(*) FROM search_run
WHERE project_id = :'proj' GROUP BY 1, 2 ORDER BY 1;
```

## 附录 C：跨语言 overlap 复现脚本

```python
import re

def terms(text: str) -> set[str]:
    """qmatrix.py:258-264 的等价实现。"""
    latin = re.findall(r"[a-z][a-z0-9@.-]{1,}", (text or "").casefold())
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", text or "")
    cjk_bigrams = [run[i : i + 2] for run in cjk_runs for i in range(len(run) - 1)]
    return set(latin + cjk_bigrams)

MIN_LEXICAL_SCORE = 0.02
questions = [
    "针对序列推荐系统的主要攻击方法有哪些？其技术原理和假设是什么？",
    "评估序列推荐攻击常用的数据集和评价指标是什么？不同攻击方法的效力如何比较？",
    "已有哪些防御或鲁棒训练策略能够减轻序列推荐中的攻击影响？",
    "现有研究如何建模攻击者的能力、知识和预算？对实际系统的威胁程度如何？",
    "序列推荐攻击研究存在哪些不足与未来方向？",
]
# evidence_texts 从上面的 SQL 导出（332 行英文证据）
for question in questions:
    question_terms = terms(question)
    best = max(
        len(question_terms & terms(text)) / max(1, len(question_terms))
        for text in evidence_texts
    )
    passed = sum(
        len(question_terms & terms(text)) / max(1, len(question_terms)) >= MIN_LEXICAL_SCORE
        for text in evidence_texts
    )
    print(f"best={best:.4f} passed={passed} :: {question[:24]}")

# 实测输出：5 行全部 best=0.0000 passed=0
```
