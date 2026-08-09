# 问题驱动的文献综述学术深度优化方案

> 状态：**规划稿，未实施任何代码改动**
> 审计基准：仓库工作树 `/data/zhuzy/projects/paper-forge`，2026-07-30
> 审计范围：`README.md`、`docs/design.md`、`packages/{scholar_gateway,ingest,db,llm_runtime,paper_ir}`、
> `services/worker/paperforge_worker/**`、`services/api/paperforge_api/routers/**`、`apps/web/lib/**`

**能力标注约定**（全文统一）：

| 标注 | 含义 |
|---|---|
| 【已有】 | 代码存在、已接入主链路、默认路径会执行 |
| 【部分实现】 | 代码存在且可运行，但未接入默认路径 / 被预算或参数严重削弱 / 只覆盖部分场景 |
| 【死代码】 | 代码存在且有测试，但生产链路零调用点 |
| 【完全缺失】 | 仓库中不存在对应实现 |
| 【需验证】 | 静态阅读无法判定，需真实运行取证 |

---

## 1. 当前架构与真实数据流审计

### 1.1 声明的管线 vs 实际执行的管线

`docs/design.md` §4.4.1 与 `services/worker/paperforge_worker/worker.py:999` 的 docstring 都声明综述管线为：

```
scope → search → curate → ingest → cards → outline → write → quality → visual → render
```

**实际代码不是这样。** 一键入口 `run_full_pipeline`（`worker.py:989`）的真实调用序列：

| 步骤 | 调用点 | 说明 |
|---|---|---|
| 1 | `worker.py:1004` → `run_library_pipeline(..., finalize=False)` | 内部为 scope → search → curate → **cards** |
| 2 | `worker.py:1023` `_outline(...)` | OUTLINE |
| 3 | `worker.py:1028` `write_document(...)` | WRITE（`coherence=False`） |
| 4 | `worker.py:1044` `_quality(...)` | QUALITY，`quality_profile` 默认 `"draft"`（`worker.py:994`） |
| 5 | `worker.py:1059` `suggest_visuals(...)` / `worker.py:1071` `export_document(...)` | VISUAL / RENDER |

**`ingest` 阶段不在其中。** 全仓库 `acquire_fulltexts` 只有一个调用点：`worker.py:554`，位于 `run_ingest_pipeline`——一个**独立的 job kind**，通过独立 API `POST /projects/{id}/ingest`（`services/api/paperforge_api/routers/writing.py:704-721`）触发，前端是一个独立按钮「获取 OA 全文」（`apps/web/lib/labels.ts:123`、`apps/web/lib/api.ts:1010`）。

`run_library_pipeline` 调用卡片抽取时**不传 `fulltexts`**：

```python
# worker.py:252（run_library_pipeline 内）
lambda: generate_cards(context, language=language),
```

而 `generate_cards` 的签名默认 `fulltexts: dict[str, str] | None = None`，随即 `fulltexts = fulltexts or {}`（`pipelines/cards.py:105`）。

> **审计结论 1（最重要）**：**默认一键路径产出的 100% 是摘要级卡片**，`literature_card.fulltext_used` 全为 `False`。全文管线不是"没做"，而是**做了但没接进主链路**，且用户必须知道要先手动点一次「获取 OA 全文」才有全文证据。

### 1.2 真正送进写作模型的上下文（逐字段实测）

写作上下文由 `pipelines/document.py:_card_context`（`document.py:774-795`）构造，**这是唯一的文献信息来源**：

```python
context[entry.bibtex_key] = {
    "title":         work.canonical_title,
    "year":          work.publication_year,
    "venue":         work.venue_name,
    "summary":       (card.summary if card else None) or work.abstract,
    "contributions": card.contributions_json or [],
    "methods":       card.methods_json or [],
    "results":       card.results_json or [],
    "limitations":   card.limitations_json or [],
    "quotable_points": card.quotable_points_json or [],
    "fulltext_used": bool(card and card.fulltext_used),
}
```

再由 `pipelines/writing.py:_build_prompt`（`writing.py:326-391`）压缩成 prompt 文本：

```python
parts = [f"[{key}] {card.get('title','')} ({card.get('year') or 'n.d.'})"]
if card.get("summary"):
    parts.append(f"  summary: {str(card['summary'])[:300]}")        # writing.py:344
for field_name in ("contributions", "methods", "results", "limitations"):
    parts.append(f"  {field_name}: {'; '.join(... values[:3])}")     # writing.py:345-348
if card.get("fulltext_used"):
    for point in located[:3]:                                        # writing.py:352
        parts.append(f"  fulltext evidence ({locator}): {point['text']}")
```

**逐条回答用户的第 2 项核查要求：**

| 问题 | 结论 |
|---|---|
| 是否只有标题、年份、摘要和元数据 | **默认路径：是。** 且**连作者都没有**——`_card_context` 不含 authors/DOI。venue 进了字典但 `_build_prompt` 未使用。 |
| 是否读取论文全文 | 【部分实现】仅当用户单独跑过 ingest job；且全文**不进写作 prompt**，只进卡片抽取（`cards.py:203`），写作端只看到 ≤3 条 quotable_points |
| 方法 / 实验设置 | 【部分实现】只有 `methods` 列表前 3 条自由文本短句，无结构化字段 |
| 数据集 / 样本量 / 参数 / 评价指标 | 【完全缺失】数据模型中不存在任何对应字段（见 §5.1） |
| 定量结果 / 误差 | 【完全缺失】`results_json` 是自由文本 list，无数值槽位；摘要级路径下数值大多丢失 |
| 局限 | 【已有】`limitations_json`，但前 3 条、自由文本 |
| 表格抽取 | 【死代码】见 §1.4 |
| 图注 / 公式 / 算法 / 补充材料 | 【完全缺失】PDF 解析层不产生任何对应产物（见 §1.3） |
| 结论可追溯到论文 + 页码 + 原文 | 【部分实现】数据结构支持（`quotable_points` 带 `page/section/paragraph`，质量层建 claim-evidence 锚点），但默认路径下 `page` 恒为 `None`（见 §1.5） |

### 1.3 PDF 解析层：只出纯文本

`packages/ingest/ingest/pdf.py:_extract_with_pypdf`（`pdf.py:50-108`）逐页 `page.extract_text()`，产出的 `structure_segments` 每项只有：

```python
{"format": "pdf", "page_number": ..., "page_range": [...],
 "page_locator_reliable": True, "char_start": ..., "char_end": ...}   # pdf.py:79-88
```

**没有 `section_title`，也没有 `heading`。** 而下游 `pipelines/fulltext.py:171` 恰恰在找这两个键：

```python
section = located.get("section_title") or located.get("heading")   # fulltext.py:171
```

> **审计结论 2**：PDF 路径**永远只能产出 `[[PAGE=N]]` 标记，`[[SECTION=...]]` 从不出现**。`cards.py:38` 的系统提示词要求模型"回填页码/章节"，但章节信息在解析层就已丢失。表格、公式、图注、算法框、补充材料在 `pypdf.extract_text()` 一步全部退化为无结构文本行或直接丢失。加密 PDF 直接放弃（`pdf.py:58-60`，合规红线，正确）。

### 1.4 结构化抽取能力是死代码

`packages/ingest/ingest/section_chunks.py` 提供了相当完整的能力：

- `classify_content_role`（`section_chunks.py:103`）：11 类内容角色 + `claim_eligible` 标志
- `parse_markdown_table`（`section_chunks.py:146`）→ `StructuredTableEvidence`，**带 cell 级 `start_offset/end_offset` 定位**（`section_chunks.py:77-90`）
- `select_section_aware_chunks`（`section_chunks.py:221`）：按 methods → results → discussion → intro 线索组择优选块

四者均在 `packages/ingest/ingest/__init__.py:39-45` 导出。**但全仓库检索（排除 tests）显示零调用点。** `pipelines/fulltext.py:126-129` 走的是另一条更弱的路：

```python
usable = [_located_chunk_text(chunk, parsed.metadata or {})
          for chunk in chunks
          if assess_chunk_quality(text=chunk.text).usable_for_cards]   # fulltext.py:126-129
```

> **审计结论 3**：**"section 感知切块"在设计文档中被列为 INGEST 的组成部分（design.md §4.4.1），在代码中存在且有测试，但从未被生产链路调用。** 表格 cell 级证据定位能力同样闲置。

### 1.5 信息漏斗：全文 → 写作 prompt 的逐级衰减

即使用户手动跑了 ingest，信息量也在五道关卡上塌缩：

| 关卡 | 位置 | 限制 |
|---|---|---|
| 1. 抓取篇数 | `pipelines/fulltext.py:33` | `DEFAULT_MAX_WORKS = 12` |
| 2. 每篇 URL 尝试 | `packages/scholar_gateway/scholar_gateway/fulltext.py:19` | `MAX_URLS_PER_WORK = 3`；来源仅 work_url(pdf) / arXiv / Europe PMC（`fulltext.py:117-158`） |
| 3. 全文存量 | `pipelines/fulltext.py:34` | `MAX_FULLTEXT_CHARS = 120_000` |
| 4. 送抽取器 | `pipelines/cards.py:203` | `fulltext[:60000]`，输出上限 `max_output_tokens=1600`（`cards.py:208`） |
| 5. 卡片条目 | `pipelines/cards.py:23` | `MAX_LIST_ITEMS = 6`——methods/results/limitations/quotable_points **各 ≤6 条** |
| 6. 进写作 prompt | `pipelines/writing.py:344-352` | summary `[:300]` 字符、各 list `[:3]` 条、fulltext evidence `[:3]` 条 |

> **审计结论 4**：一篇 120k 字符的论文，最终以 **≤3 条原文片段 + ≤3 条方法短句 + 300 字符摘要**的形态进入写作模型。这是"综述深度不足"的**主要物理原因**——不是模型不会综合，是它拿不到可综合的东西。

### 1.6 已经存在、且做得不错的部分（不要重复建设）

| 能力 | 位置 | 评价 |
|---|---|---|
| 句级 claim–evidence 锚点 | `pipelines/quality.py:159-231` `build_claim_evidence` | 【已有】按句绑定 CiteRun，产出 `claim_kind/source_kind/source_page/evidence_excerpt/support_status/support_score` |
| 论断类型分类 | `quality.py:143-156` `classify_claim` | 【已有】numeric / causal / comparison / effect / conclusion / background，中英双语正则，且排除化学命名与版本号误判（`quality.py:120-124`） |
| 核心论断全文覆盖率 | `quality.py:296-319` | 【已有】`core_claim_fulltext_coverage` 已是一等指标，前端已展示（`apps/web/components/export/export-center.tsx:393`） |
| 引用真实性 R1/R2/R3 | `db.get_writing_whitelist` / `writing.py:220-266` / `paper_ir/bibtex.py` | 【已有】三道防线完整，幻觉引用问题已解决 |
| 数值 lint | `packages/ingest/ingest/numlint.py` + `document.py:363-386` | 【已有】正文数值 vs 素材/文献证据比对 |
| 反罗列指令 | `writing.py:53`、`writing.py:71`、`outline.py:38`、`outline.py:57` | 【已有】prompt **已明确禁止**逐篇复述 |
| 确定性跨研究比较章节 | `outline.py:322-395` `review_synthesis_section` | 【部分实现】强制插入一节，带文献矩阵表，但表只有 Study/Year/Method/Evidence basis 四列 |

---

## 2. 现有问题及根因

### P1 —— 全文管线未接入主链路（根因级）

- **现象**：默认一键生成的综述，所有论断只能追溯到摘要。
- **根因**：`run_full_pipeline`（`worker.py:989-1004`）不调用 `acquire_fulltexts`；`run_library_pipeline` 调 `generate_cards` 时不传 `fulltexts`（`worker.py:252`）。
- **严重度**：P0。这是单点、低成本、高收益的修复。

### P2 —— 写作 prompt 在无全文时把综述降级为"背景写作"

`writing.py:_asset_block`（`writing.py:394-476`）对 `paper_type == "review"` 的分支：

```python
if located_fulltext:
    return "...numeric, causal, comparative, effect, and conclusion claims may only use
            located fulltext evidence shown above..."     # writing.py:426-432
return "...no located fulltext evidence is available; write background only, with no
        numeric, causal, comparative, effect, or conclusion claims."   # writing.py:437-442
```

- **现象**：默认路径 `located_fulltext` 恒为 `False` → **每一节都被指示"只写背景，不得写数字、因果、比较、效果或结论"**。
- **后果二选一**：模型服从 → 产出无结论的空洞综述；模型不服从 → 论断被 `build_claim_evidence` 标为 `abstract_only`，`core_claim_fulltext_coverage` 归零。
- **根因**：证据规则本身是对的（防过度推断），但它建立在一个默认不满足的前提上。规则与管线接线不匹配。

### P3 —— 上下文是"每篇一段摘要"，模型无从综合

**直接回答用户的核查点"写作 Prompt 是否诱导罗列"：不是。** `writing.py:53` 明写"按主题论证展开，不要逐篇复述文献"，`outline.py:38` 明写"不要按文献逐篇罗列"。指令层面是正确的。

真正的机制是：`_build_prompt`（`writing.py:339-363`）把上下文组织成 **N 个以 `[key] title (year)` 开头的独立卡片块**，块与块之间没有任何跨文献结构——没有共同的问题、没有可比维度、没有对齐的指标、没有一致/冲突标注。**上下文的形状就是一份文献清单**，模型只能在清单上做表面改写。指令说"按主题"，材料说"按论文"，材料赢。

- **根因**：缺少介于「卡片」与「写作」之间的**综合层**（问题—证据矩阵）。

### P4 —— OUTLINE 是文献驱动而非问题驱动

- `generate_outline`（`outline.py:102`）输入是 `topic + research_question + list[CardBrief]`；`CardBrief`（`outline.py:87-99`）只有 cite_key/title/year/summary/contributions/methods/results/limitations/fulltext_used。
- 章节由**卡片聚类**产生（`_body_sections`，`outline.py:189-239`），确定性回退甚至直接**按年代分组**（`deterministic_body_sections`，`outline.py:275-317`：「研究背景与早期工作」/「近期研究进展」）。
- `scope.py` 只产出**一句话** `research_question` + 扁平 `subtopics` 列表（`scope.py:26-31`），**没有子问题、没有比较维度、没有 PICO/任务-数据集-指标框架**。
- **根因**：产品链路里不存在"研究问题分解"这一步。综述的骨架来自"我检索到了什么"，而不是"我要回答什么"。

### P5 —— 摘要证据被扩写为强结论，只警告不阻断

`apply_readiness_gate`（`quality.py:274`）确实会为每条无全文支撑的核心论断产生 blocker：

```python
missing_evidence = len(core_hashes - supported_hashes)
if missing_evidence:
    blockers.append(_issue("core_claim_fulltext_missing", f"{missing_evidence} 条核心论断没有可定位且相符的全文证据"))  # quality.py:324-332
```

但收尾处：

```python
report.blockers = blockers if report.quality_profile == "submission" else []      # quality.py:449
report.warnings = warnings + (blockers if report.quality_profile == "draft" else [])
```

而 `run_full_pipeline` 的默认值是 `quality_profile: str = "draft"`（`worker.py:994`）。

- **现象**：**默认路径下，"全部核心论断均无全文证据"会被降级为一条警告，正常渲染出 PDF。**
- **根因**：draft-first 哲学（README 第 3 行「成稿优先、流程宽松」）与学术深度目标在此处直接冲突。这是**设计取舍**，不是 bug——但它意味着系统当前不阻止摘要→强结论的扩写。

### P6 —— 无可比性模型，无法阻止错误横向比较

- 数据层没有 population / dataset / task / metric / protocol / 样本量 任何字段（`packages/db/db/models/library.py:138-158` 的 `LiteratureCard` 全部是自由文本 JSONB）。
- `review_synthesis_section`（`outline.py:322`）的比较表列为 `[研究, 年份, 方法/对象, 证据基础]`，其中"方法"取 `card.methods[0]`（`outline.py:337`）——一句自由文本。
- `_support_score`（`quality.py:503-509`）是**词袋 token 交集比**，阈值 0.12（`quality.py:192`）。它无法判断两项研究是否在同一数据集、同一指标下可比，也无法发现数值与原文不符。
- **根因**：【完全缺失】可比性维度既没有抽取，也没有建模，也没有校验。

### P7 —— 结构化抽取能力闲置

见 §1.4。`select_section_aware_chunks` / `parse_markdown_table` / `classify_content_role` 三项能力已迁移、已测试、未接线。

### P8 —— 章节级证据分配缺失

`write_section`（`writing.py:181-193`）的 `allowed` 集合来自 `section["cite_keys"]`，即**整篇文献**被分配给章节。没有"这一节需要这篇文献的哪一条证据"的粒度。因此同一篇文献在多节中被反复以相同的 300 字符摘要呈现，加剧了重复与罗列感。

---

## 3. 目标综述生成范式

### 3.1 范式对照

| 维度 | 当前（文献驱动） | 目标（问题驱动） |
|---|---|---|
| 骨架来源 | 卡片聚类 / 年代分组 | 研究问题 → 子问题 → 比较维度 |
| 上下文单位 | 一篇文献一个块 | 一个子问题一个证据簇（跨文献对齐） |
| 段落组织 | 主题名下顺次提及各文献 | 论断先行 → 一致证据 → 条件性差异 → 冲突 → 缺口 |
| 证据单位 | 卡片字段（自由文本） | EvidenceUnit（带定位、类型、可比维度、数值） |
| 比较 | 无结构，模型自由发挥 | 仅在 comparability_key 相同的证据间允许 |
| 结论 | 复述文献要点 | 回答子问题 + 适用条件 + 证据强度 + 不确定性 |
| 引用 | 句级 cite_key | 句级 cite_key + evidence_unit_id（页/表/式级定位） |

### 3.2 目标写作单元的形状（每个正文段落）

```
[论断句]                       ← 回答某个子问题的一个侧面
  ├─ 一致证据：E1(A,p.5,Tab2), E3(C,p.7)   ← 同一 comparability_key
  ├─ 条件性差异：E2(B,p.4) 在 X 条件下相反
  ├─ 证据强度：strong | moderate | weak | abstract_only
  └─ 适用边界：数据集/规模/领域限定
```

### 3.3 不变量（新增红线，与既有 R1/R2/R3 并列，建议记为 R4/R5/R6）

- **R4 证据分级红线**：`claim_kind != background` 的句子，其支撑证据的 `evidence_grade` 必须 ≥ `moderate`；`abstract_only` 证据只能支撑 `background` 与 `attribution`（"某文献报告了……"）两类句子，不得支撑因果/比较/效果/结论句。
- **R5 可比性红线**：任何跨文献比较句，其引用的 ≥2 个 evidence_unit 必须共享同一 `comparability_key`；否则系统降级为"分别报告"表述并出告警。
- **R6 定位红线**：`numeric` 类论断必须携带 `evidence_unit_id`，且该 unit 必须有 `page` 或 `table_id`/`equation_id` 之一。

---

## 4. 全文与多模态证据获取方案

### 4.1 获取层（scholar_gateway）

| 项 | 当前 | 目标 |
|---|---|---|
| 来源 | work_url(pdf) / arXiv PDF / Europe PMC render（`scholar_gateway/fulltext.py:117-158`） | 新增：**arXiv LaTeX 源码（`/e-print/`）**、**PMC JATS XML**（`efetch?db=pmc&rettype=xml`）、Unpaywall best_oa_location、OpenAlex `open_access.oa_url`、DOAJ |
| 篇数 | `DEFAULT_MAX_WORKS = 12` | 提升至可配置 `FULLTEXT_MAX_WORKS`（默认 40），按 `relevance_score` + `influential_citation_count` 排序取前 N，而非当前的 `targets[:max_works]` 原序截断（`scholar_gateway/fulltext.py:94`） |
| 合规 | 只走 OA 渠道，不绕 paywall（`fulltext.py:7-8` 注释；`pdf.py:58-60` 拒绝加密 PDF） | **完全保留**。新增源同样只用官方 OA 端点，并在 `document_file` 记录 `license` |

> **JATS XML 与 LaTeX 源是本方案性价比最高的一步**：两者天然带章节树、表格结构、公式（MathML / `$..$`）、图注，无需 OCR 或版面分析即可拿到 §1.3 中丢失的全部结构。PMC 与 arXiv 覆盖了 CS/物理/生医的绝大多数 OA 文献。

### 4.2 解析层（ingest）

新增三个解析器，与现有 `extract_pdf_content` 并列注册到 `ingest.extract`：

1. `ingest/jats.py`：JATS XML → 章节树、`<table-wrap>`、`<fig><caption>`、`<disp-formula>`、`<sec>` 层级、参考文献剥离。
2. `ingest/latex_source.py`：arXiv e-print tarball → `\section`/`\subsection` 树、`tabular`/`booktabs` 表、`equation`/`align` 环境、`\caption`、`algorithm` 环境。
3. `ingest/pdf.py` 增强：接入 PyMuPDF（`fitz`）或 pdfplumber 做 (a) 基于字号/字重的标题识别 → 补齐 `structure_segments` 的 **`section_title`**（修复 §1.3 断链）；(b) `find_tables()` 表格抽取；(c) 图注正则（`^(Figure|Fig\.|Table|表|图)\s*\d+`）。**保留 pypdf 为兜底路径**，维持 `pdf.py:32-47` 的"两条路径取更长者"策略。

### 4.3 结构化切块层

把 §1.4 的死代码接线，并扩展：

- `pipelines/fulltext.py:126-129` 的 `usable` 过滤替换为 `classify_content_role` + `select_section_aware_chunks` 组合，保留 `assess_chunk_quality` 作为第二道。
- `_located_chunk_text`（`fulltext.py:154-175`）在 `[[PAGE=..|SECTION=..]]` 基础上增加 `TABLE=` / `FIG=` / `EQ=` 标记。
- 表格走 `parse_markdown_table` → `StructuredTableEvidence`，cell 级 offset 直接映射为 `evidence_unit.locator`。

### 4.4 降级策略（禁止摘要冒充全文）

严格四级，落到 `evidence_unit.grade`：

| 级别 | 条件 | 可支撑的 claim_kind |
|---|---|---|
| `A_located_structured` | 全文 + 页码/表号/式号 + 结构化数值 | 全部 |
| `B_located_prose` | 全文 + 页码或章节，纯文字 | causal / comparison / effect / conclusion |
| `C_fulltext_unlocated` | 有全文但无可靠定位 | effect / conclusion（且必须标注不确定性） |
| `D_abstract_only` | 仅摘要/元数据 | **仅** background / attribution |

`D` 级证据在写作 prompt 中必须以显式标签呈现（如 `[ABSTRACT-ONLY — 只可用于背景或"某文献报告"式转述]`），而不是像现在这样与全文证据混排后仅靠一句全局规则约束。

---

## 5. 结构化证据数据模型

### 5.1 当前模型的缺口

`LiteratureCard`（`packages/db/db/models/library.py:138-158`）：

```python
summary / contributions_json / methods_json / results_json /
limitations_json / quotable_points_json / fulltext_used /
extraction_model / source_hash
```

全部是自由文本或自由文本 list。**没有任何可比维度、数值、定位对象、证据类型字段。**

### 5.2 新增表（Alembic 迁移，`packages/db/migrations/versions/`）

**`evidence_unit`** —— 证据原子单位（替代 `quotable_points_json` 的扁平结构）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | PK |
| `work_id` | FK scholarly_work | |
| `project_id` | FK paper_project, nullable | 跨项目复用时为 NULL |
| `kind` | String(24) | `experimental_fact` / `theoretical_derivation` / `author_conclusion` / `review_restatement` / `model_inference` ← **直接实现用户要求的五类区分** |
| `grade` | String(24) | `A_located_structured` … `D_abstract_only`（见 §4.4） |
| `text` | Text | 原文片段（verbatim） |
| `text_hash` | String(64) | 去重 + 篡改检测 |
| `page` / `section_path` / `paragraph_index` | Integer / Text / Integer | 定位 |
| `object_ref` | String(64) | `table:3` / `fig:2` / `eq:7` / `algo:1` |
| `char_start` / `char_end` | Integer | 回原文的精确 offset |
| `source_document_file_id` | FK document_file | 可回放到原始字节 |

**`evidence_measurement`** —— 从证据中抽出的可比数值

| 列 | 说明 |
|---|---|
| `evidence_unit_id` | FK |
| `metric_name` | 归一化指标名（`accuracy` / `F1` / `nDCG@10` / `HR@20`） |
| `value` / `unit` / `ci_low` / `ci_high` / `std` | 数值与误差 |
| `dataset` / `task` / `model_family` / `sample_size` / `split` | 可比维度 |
| `comparability_key` | **确定性生成**：`sha1(task|dataset|metric|split)` ← R5 红线的执行依据 |

**`research_question`** —— 问题分解树

| 列 | 说明 |
|---|---|
| `project_id` / `parent_id` | 支持核心问题 → 子问题两层 |
| `text` / `kind`（`core`/`sub`） / `order_index` | |
| `comparison_dimensions_json` | 该问题下需要横向比较的维度列表 |
| `answer_status` | `answered` / `partial` / `contested` / `insufficient_evidence` |

**`question_evidence_link`** —— 问题—证据矩阵（本方案的核心新结构）

| 列 | 说明 |
|---|---|
| `research_question_id` / `evidence_unit_id` | 多对多 |
| `stance` | `supports` / `contradicts` / `conditional` / `not_comparable` / `gap` |
| `condition_note` | 该证据成立的条件（数据集/规模/领域） |
| `confidence` | 抽取置信度 |

**`claim_evidence_anchor`** —— 把 `quality.py:build_claim_evidence`（现在是内存 list + `repositories/quality.py` 落库）扩展为一等表，增列 `evidence_unit_id`、`comparability_ok`、`grade_ok`，供 R4/R5/R6 校验与前端定位。

> 注：`packages/db/db/repositories/quality.py`（190 行）已存在质量报告仓储，claim_evidence 已有落库路径【需验证】其表结构是否足以承载新增列，迁移时应优先扩列而非新建。

---

## 6. 问题分解与跨文献综合流程

### 6.1 新增阶段（插入现有管线）

```
scope → [QDECOMP] → search → curate → ingest(结构化) → cards
      → [EVIDENCE] → [QEMATRIX] → [SYNTH] → outline → write → quality → render
                       ▲ 新增三阶段
```

| 新阶段 | 职责 | 落点 |
|---|---|---|
| `QDECOMP` | 研究问题 → 3–6 个子问题 + 每个子问题的比较维度 | 扩展 `pipelines/scope.py`，写入 `research_question` 表 |
| `EVIDENCE` | 全文块 → `evidence_unit` + `evidence_measurement` | 新建 `pipelines/evidence.py` |
| `QEMATRIX` | 证据 × 子问题 匹配 → `question_evidence_link` | 新建 `pipelines/qmatrix.py` |
| `SYNTH` | 每个子问题内做一致/差异/冲突/不可比/缺口判定 | 新建 `pipelines/synthesis.py` |

### 6.2 QDECOMP：问题分解

扩展 `scope.py:_SYSTEM_PROMPT_ZH`（`scope.py:23-42`）的输出 schema：

```json
{
  "research_question": "...",
  "sub_questions": [
    {"text": "子问题",
     "comparison_dimensions": ["数据集", "模型规模", "评价指标", "攻击预算"],
     "expected_evidence_kinds": ["experimental_fact", "author_conclusion"]}
  ],
  "keyword_groups": [...], "subtopics": [...], ...
}
```

保留现有确定性回退（`scope.py` 已有 fallback 机制），无 LLM 时按 `subtopics` 逐条升格为子问题。

### 6.3 QEMATRIX：问题—证据矩阵

对每个 `(sub_question, evidence_unit)` 对做匹配。**规模控制**：先用确定性检索（BM25 / 向量）取 top-K 候选，再用 `extractor` 角色批量判定 stance，避免 N×M 次 LLM 调用。

矩阵是一个可持久化、可在前端展示、可人工修正的对象——**它取代"卡片列表"成为写作的输入**。

### 6.4 SYNTH：五类判定（直接回应用户要求）

在每个子问题内，对证据分组后判定：

| 判定 | 规则（确定性优先） |
|---|---|
| 一致证据 | ≥2 个 unit，同 `comparability_key`，同向 stance |
| 条件性差异 | 同 key，反向 stance，但 `condition_note` 可区分 |
| 真正冲突 | 同 key，反向 stance，条件不可区分 → 正文必须显式写出冲突 |
| 不可比较 | `comparability_key` 不同 → **禁止生成比较句**，降级为分别陈述（R5） |
| 证据缺口 | 子问题下无 `grade ≤ C` 的证据 → 正文写"现有证据不足以回答"，且计入 `answer_status = insufficient_evidence` |

**用户问题"如何自动阻止错误横向比较"的答案**：比较不由模型自由决定，而由 `comparability_key` 门控——SYNTH 阶段只把同 key 的证据组织进"比较簇"，不同 key 的证据在写作 prompt 中被物理隔离到不同的块，并附禁止比较的显式标注；写完后 `quality` 阶段再做一次 R5 校验（比较句引用的 unit 是否同 key），违规则改写或降级。

### 6.5 OUTLINE 改造

`generate_outline`（`outline.py:102`）的输入从 `list[CardBrief]` 改为 `list[SubQuestionBundle]`。章节 ≈ 子问题（或子问题聚类），`argument_points` 由 SYNTH 的判定结果确定性生成，而非模型自由发挥。

`deterministic_body_sections` 的年代分组回退（`outline.py:275-317`）改为按子问题回退——**任何情况下都不再退化为"早期工作/近期进展"这种文献驱动骨架**。

`review_synthesis_section`（`outline.py:322`）的矩阵表升级为真正的证据矩阵：列改为 `[研究, 任务/数据集, 方法, 关键指标与数值, 证据等级, 定位]`。

---

## 7. 写作 Prompt 和上下文组装策略

### 7.1 上下文形状的根本改变

**当前**（`writing.py:339-363`，按论文组织）：

```
Literature cards:
[smith2020] Title A (2020)
  summary: ...
  methods: ...
[jones2021] Title B (2021)
  ...
```

**目标**（按问题与证据簇组织）：

```
本节回答的子问题：Q2 —— 在稀疏交互场景下，投毒攻击的有效性是否随模型规模下降？

【比较簇 C1】comparability_key=(sequential-rec | MovieLens-1M | HR@20 | leave-one-out)
  一致证据（2 项）：
    E17 [smith2020] p.6 Tab.3 — "HR@20 drops from 0.182 to 0.131 under 1% poisoning"
        measurement: HR@20 = 0.131 (base 0.182), dataset=ML-1M, n=6040
    E23 [jones2021] p.8 Tab.2 — "...0.174 → 0.128 (1% injection)"
        measurement: HR@20 = 0.128 (base 0.174), dataset=ML-1M, n=6040
  → SYNTH 判定：一致；效应方向一致，量级相近

【比较簇 C2】comparability_key=(sequential-rec | Amazon-Beauty | nDCG@10 | temporal-split)
  单一证据：E31 [lee2022] p.5 — ...
  → 与 C1 不可比（数据集与指标均不同）；**禁止与 C1 做量级比较**

【证据缺口】Q2 在 >1B 参数模型上无任何 grade≤C 证据
```

> 这是本方案与"改 prompt 措辞"的本质区别：**不是让模型不要罗列，而是让它拿到的材料本身不是清单。**

### 7.2 Prompt 改造要点

对 `writing.py:_SYSTEM_PROMPT_ZH/EN`（`writing.py:42-76`）：

1. 保留现有的 cite_keys 句级绑定与禁止内联标记规则（已正确）。
2. 新增：每个 `sentences[]` 项增加 `evidence_ids: []` 字段——句子必须声明所依据的 evidence_unit，这是 R6 的执行入口，也直接回答用户"每个关键论断都能定位到具体证据片段、页面、表格或公式"。
3. 新增：每段必须声明 `stance_summary`（consistent / conditional / conflicting / insufficient），强制模型输出综合判定而非陈述。
4. 段落模板从"观点先行、证据跟随"细化为"论断 → 一致证据 → 条件性差异 → 冲突/缺口 → 适用边界"。
5. 明确禁止句式：「文献 A 提出了……文献 B 提出了……」的连续 attribution 句超过 2 句即视为罗列。

对 `_asset_block`（`writing.py:394-476`）的 review 分支：不再二元判断 `located_fulltext`，改为按 §4.4 的四级证据分别给出可写作范围；`D_abstract_only` 证据在块内单独成组并显式标注。

### 7.3 上下文预算重分配

现有 `max_output_tokens=4000`（`writing.py:225`）保持；输入侧从"每篇 ≤3 条"改为"每子问题 ≤N 个比较簇、每簇 ≤M 条证据"，按 token 预算动态裁剪，**优先保留 grade A/B 的证据**——这是与当前 `[:3]` 无差别截断（`writing.py:348-352`）的关键差异。

### 7.4 摘要/引言/结论

结论章必须由 `research_question.answer_status` 确定性驱动：逐个子问题给出"已回答 / 部分回答 / 存在争议 / 证据不足"，禁止在结论中出现正文未出现的论断。

---

## 8. 引用、证据等级与防过度推断机制

| 机制 | 现状 | 改造 |
|---|---|---|
| R1 入库核验 | 【已有】`get_writing_whitelist` | 不变 |
| R2 cite_key 白名单三道防线 | 【已有】`writing.py:220-266` + `enforce_cite_key_whitelist` | 不变，增加 evidence_id 白名单同构校验 |
| R3 参考文献确定性生成 | 【已有】`paper_ir/bibtex.py` | 不变 |
| **R4 证据分级** | 【完全缺失】 | `claim_kind` × `evidence_grade` 相容性矩阵，在 `quality.py` 与写作后处理各校验一次 |
| **R5 可比性** | 【完全缺失】 | 比较句的 evidence units 必须同 `comparability_key` |
| **R6 定位** | 【部分实现】`source_page` 已有但常为 None | numeric 论断必须有 page 或 object_ref |
| 数值一致性 | 【部分实现】`numlint` 比对正文数值 vs 证据文本 | 升级为与 `evidence_measurement.value` 精确比对（当前是文本包含匹配） |
| 语义软校验 | 【部分实现】`soft_check_citations`（`quality.py:512`）比对引用与**摘要** | 改为比对引用与 evidence_unit 原文 |
| 支撑度打分 | 【已有但弱】`_support_score` 词袋交集，阈值 0.12（`quality.py:503`） | 保留为快筛，新增 LLM 判定二段（entailment: supports/partial/contradicts/unrelated） |

### 8.1 阻断策略（回应 P5）

新增 `quality_profile = "scholarly"`（介于 draft 与 submission 之间），在 `quality.py:449` 的分支中：R4/R5/R6 违规为 **blocker**，其余仍为 warning。默认综述项目使用该 profile；`draft` 保留给需要快速出稿的场景。这样既不推翻 README 的 draft-first 哲学，又让"学术深度模式"有真正的护栏。

**同时**：正文中被判定为 `abstract_only` 支撑的强论断，不是简单删除，而是**自动降级改写**为 attribution 句式（"Smith 等报告了……"）并加 `\todo{需全文核实}` 标记——保持 draft-first 的"降级不阻断"。

---

## 9. 后端、数据库、任务队列和前端改造范围

### 9.1 后端模块

| 文件 | 改动 | 规模 |
|---|---|---|
| `services/worker/paperforge_worker/worker.py` | `run_full_pipeline` 接入 ingest（`:1004` 后）；新增 QDECOMP/EVIDENCE/QEMATRIX/SYNTH 阶段编排；修正 `:999` docstring | 中 |
| `pipelines/fulltext.py` | 接线 `select_section_aware_chunks`；扩展定位标记；提高 `DEFAULT_MAX_WORKS`；按分值排序选篇 | 中 |
| `pipelines/cards.py` | 卡片抽取拆分为「卡片」+「证据单元」两个产物；解除 `MAX_LIST_ITEMS=6` 对证据的限制 | 大 |
| `pipelines/evidence.py` | **新建**：evidence_unit / measurement 抽取 | 大 |
| `pipelines/qmatrix.py` | **新建**：问题—证据矩阵 | 大 |
| `pipelines/synthesis.py` | **新建**：五类判定 | 大 |
| `pipelines/scope.py` | 扩展 schema 至 sub_questions + comparison_dimensions | 小 |
| `pipelines/outline.py` | 输入换成子问题包；回退逻辑改造；矩阵表升级 | 中 |
| `pipelines/writing.py` | `_build_prompt` 重写为问题驱动组装；`_asset_block` 四级证据；schema 加 evidence_ids | 大 |
| `pipelines/quality.py` | R4/R5/R6 校验；新 profile；support 二段判定 | 中 |
| `packages/ingest/{jats,latex_source}.py` | **新建** | 大 |
| `packages/ingest/pdf.py` | PyMuPDF 增强 + section_title 补齐 | 中 |
| `packages/scholar_gateway/fulltext.py` | 新增 OA 源 | 中 |

### 9.2 数据库

5 张新表（§5.2）+ `literature_card` 增列 + `claim_evidence` 扩列。全部走 Alembic（`packages/db/migrations/versions/`）。**兼容性**：`quotable_points_json` 保留并在过渡期双写，旧项目不迁移也能渲染。

### 9.3 任务队列（ARQ）

新增 4 个 job function 并注册到 worker 的 function 列表；`GenerationJob.kind` 增加对应枚举值；`checkpoint_json` 需支持新阶段续跑（现有 `_run_stage` 的 checkpoint 机制可直接复用）。

**成本控制**：EVIDENCE 与 QEMATRIX 是新增的大宗 LLM 消耗，必须复用 `cards.py:81` 的 `source_hash` 内容指纹缓存模式（跨项目复用）。

### 9.4 前端

| 位置 | 改动 |
|---|---|
| `apps/web/lib/types.ts` | 新增 evidence/question/matrix 类型 |
| `apps/web/lib/pipeline.ts:142-152` | 阶段枚举扩充 |
| 新建「研究问题」页 | 子问题编辑、比较维度设置 |
| 新建「证据矩阵」页 | 问题 × 证据表格，可人工改 stance、标不可比 |
| 写作编辑器 | 句子 hover 显示 evidence_unit 原文 + 页码 + 表号，一键跳转 PDF 原位 |
| `export-center.tsx:393` | 从单一 `core_claim_fulltext_coverage` 扩展为 §10 的指标面板 |

### 9.5 明确不改动

引用真实性 R1/R2/R3 全链路、PaperIR schema 核心、LaTeX 渲染、visuald、texd、导出与账号体系。

---

## 10. 测试集、质量指标与验收标准

### 10.1 测试集构建

- **黄金集 G1（20 篇）**：人工标注的 OA 论文，标注每篇的 section 结构、表格、公式、关键数值 → 用于解析层与证据抽取层的精度评估。
- **黄金集 G2（5 个主题）**：每主题人工撰写"标准子问题分解 + 证据矩阵 + 一致/冲突判定" → 用于综合层评估。
- **对照集 G3**：同 5 主题分别用**现系统**与**新系统**各生成一篇综述，双盲专家评分。
- 测试环境：按 `MEMORY.md` 记录，本机 `uv run pytest` / `pnpm test` 不可用，须走一次性容器。

### 10.2 指标定义与门槛

| # | 指标 | 定义 | 基线（当前，预估） | 目标 |
|---|---|---|---|---|
| 1 | 全文获取率 | `parsed / selected` | 默认路径 **0%**（未接线）；手动 ingest 后【需验证】 | ≥ 70% |
| 2 | 方法/结果章节覆盖率 | 成功定位到 methods 与 results 章节的论文比例 | **0%**（PDF 无 section_title，§1.3） | ≥ 80% |
| 3 | 表格/公式/图注抽取成功率 | 对 G1 标注对象的召回 | 表格 0% / 公式 0% / 图注 0% | 表 ≥ 70%，式 ≥ 60%，图注 ≥ 80% |
| 4 | 关键结论证据覆盖率 | `core_claim_fulltext_coverage`（`quality.py:317` 已实现） | 默认路径 **0** | ≥ 0.85 |
| 5 | 页码级引用准确率 | 抽样论断，人工核对页码是否指向真实出处 | 不可测（页码恒 None） | ≥ 90% |
| 6 | 数值与原文一致率 | 正文数值 vs `evidence_measurement.value` 精确比对 | numlint 仅文本匹配【需验证】 | ≥ 98% |
| 7 | 无依据强结论率 | `claim_kind∈{numeric,causal,comparison,effect,conclusion}` 且 grade=D 的句子占比 | 预估高（默认全 D） | ≤ 2% |
| 8 | 不可比研究错误比较率 | 比较句中 evidence units 不同 `comparability_key` 的比例 | 不可测（无 key） | ≤ 3% |
| 9 | 跨论文综合段落比例 | 段落引用 ≥2 篇且含 stance 判定 | 【需验证】预估 < 25% | ≥ 60% |
| 10 | 按论文罗列段落比例 | 连续 attribution 句 ≥3 或段内单一 cite_key 且为 attribution | 【需验证】预估 > 40% | ≤ 15% |
| 11 | 问题回答完整度 | `answer_status != insufficient_evidence` 的子问题占比 | 不可测（无子问题） | ≥ 80% |
| 12 | 专家评分 | 5 名领域专家，6 维（深度/组织/证据/批判性/可信度/可读性）1–5 分，双盲对照 G3 | 待测 | 均分 ≥ 3.8 且显著优于对照 |

> 指标 1–8 应实现为**自动化指标**，由 `quality` 阶段落库并在前端展示；9–11 需要一个轻量的判别器（正则 + cheap 模型）；12 为人工。

### 10.3 "问题驱动是否真的更好"的衡量方法（回应用户核查点）

单看新系统的绝对值不足以证明优越性。采用**双盲对照 + 配对检验**：

1. 同 5 主题、同文献库、同模型、同 token 预算，分别跑旧/新流程（G3）。
2. 专家不知道稿件来源，对 6 维打分 → 配对 Wilcoxon 符号秩检验。
3. 同时报告自动指标 4/7/9/10 的差异，以及**成本差异**（LLM token 与墙钟时间）——如果深度提升以 5 倍成本换来，需要产品决策而非工程决策。
4. 消融：单独接入全文（仅修 P1）vs 全量问题驱动，区分"接线修复"与"范式改造"各自的贡献。**这一步很关键**——有可能仅 P1 修复就拿到大部分收益。

---

## 11. 分阶段 Milestone、优先级和风险

### M-A：接线修复（1–2 天，P0，无新依赖）

- 修复 P1：`run_full_pipeline` 在 cards 之前插入 ingest；`run_library_pipeline` 传递 `fulltexts`。
- 修正 `worker.py:999` 与 design.md 的文档—实现偏差。
- 修复 §1.3 断链：`pdf.py` 的 `structure_segments` 补 `section_title`（先用启发式，见 M-C 做正式版）。
- 放宽 §1.5 关卡 1/5/6：`DEFAULT_MAX_WORKS`、`MAX_LIST_ITEMS`、writing 的 `[:3]` 改为按 token 预算裁剪。
- 接线 §1.4 死代码：`select_section_aware_chunks` + `classify_content_role`。

**依赖**：无。**风险**：LLM 成本与耗时上升（全文卡片 vs 摘要卡片）——需先测单项目成本。
**验收**：指标 1 ≥ 50%、指标 4 > 0，端到端一键跑通。**这是投入产出比最高的一步，且是后续所有工作的前提。**

### M-B：证据数据模型（3–5 天，P0）

- 5 张新表 + 迁移 + 仓储层。
- `pipelines/evidence.py`：从现有全文块抽 `evidence_unit`（先不做 measurement）。
- `quality.py` 接 `evidence_unit_id`，R6 落地。

**依赖**：M-A。**风险**：迁移影响存量项目 → 双写 + 兼容读。

### M-C：多模态解析（5–8 天，P1）

- JATS XML + arXiv LaTeX 源解析器（收益最大，实现最确定）。
- PyMuPDF 增强 PDF（表格 + 标题 + 图注）。
- `evidence_measurement` 抽取。

**依赖**：M-B。**风险**：新增二进制依赖（PyMuPDF AGPL 授权需确认；`fitz` 体积影响 worker 镜像）；arXiv e-print 抓取频率限制。**这是全计划中最不确定的一环，建议先做 JATS/LaTeX，PDF 增强单独评估。**

### M-D：问题驱动综合（5–8 天，P1）

- QDECOMP / QEMATRIX / SYNTH 三阶段。
- OUTLINE 输入切换为子问题包。

**依赖**：M-B（M-C 非强依赖，可先用 B/C 级证据跑通）。**风险**：QEMATRIX 的 N×M 成本；必须先做候选检索裁剪。

### M-E：写作与防护（3–5 天，P1）

- `_build_prompt` 重写；`_asset_block` 四级证据；schema 加 evidence_ids。
- R4/R5 校验 + `scholarly` profile + 自动降级改写。

**依赖**：M-D。**风险**：模型可能无法稳定输出 evidence_ids → 需要确定性回退（用 `_support_score` 反查最佳 unit）。

### M-F：前端与度量（4–6 天，P2）

- 研究问题页、证据矩阵页、编辑器证据溯源。
- 指标 1–11 自动化 + 面板。

**依赖**：M-B/D/E。

### M-G：评测（3–5 天，P1，与 M-F 并行）

- G1/G2/G3 构建 + 双盲对照 + 消融。

### 11.1 全局风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| **成本爆炸** | 全文抽取 + 证据矩阵可能使单篇综述 LLM 成本上升 3–10× | 强制 `source_hash` 跨项目缓存；EVIDENCE 用便宜长上下文模型；QEMATRIX 先确定性裁剪 |
| **与 draft-first 哲学冲突** | 新红线可能让"永远能拿到稿子"失效 | 用新 profile 隔离；违规**降级改写**而非阻断 |
| **OA 覆盖不足** | 部分领域（尤其人文社科、部分期刊）OA 率低 → 指标 1 天花板 | D 级降级路径必须完善；在 UI 明示"本综述 X% 论断仅有摘要级证据" |
| **过度工程** | 5 张新表 + 4 个新阶段可能超出实际收益 | **M-A 后先做消融**（§10.3 第 4 点），用数据决定是否继续 M-D 及之后 |
| PyMuPDF AGPL | 授权风险 | 评估 pdfplumber（MIT）替代 |

---

## 12. 推荐的最终技术路线

**核心判断：当前系统的综述深度问题，60% 是接线与预算问题，40% 是范式问题。不要一上来就做范式改造。**

推荐路线：

1. **先做 M-A（接线修复），然后立刻做一次消融评测。** 系统里已经存在的东西——全文抓取、页码定位、句级 claim-evidence 锚点、论断类型分类、核心论断覆盖率、反罗列 prompt、跨研究比较章节——组合起来的能力，远高于默认路径实际发挥出来的水平。有相当可能，仅 M-A 就能把指标 4 从 0 拉到 0.5+，把专家评分显著拉高。**在拿到这个数据之前，不要投入 M-C/M-D。**

2. **M-B（证据数据模型）是无论如何都要做的地基。** 因为"每个论断可定位到页码/表格/公式"这个目标，在当前 `quotable_points_json` 扁平结构下不可能实现。

3. **M-C 优先做 JATS + LaTeX 源，PDF 版面分析放最后。** 前两者是确定性解析，投入产出比远高于 PDF 版面分析，且覆盖了 arXiv/PMC 这两个最主要的 OA 池。

4. **M-D/M-E 的价值取决于 M-A 消融结果。** 如果 M-A 后指标 9/10（跨论文综合 vs 罗列）仍然不达标，说明范式问题是真的，再做完整的问题驱动改造。

5. **防过度推断优先于深度。** R4/R5/R6 的价值不只在提升深度，更在**防止系统用摘要级证据写出看起来很深、实际无依据的综述**——那比浅显的综述危害更大。因此 M-E 的红线部分应当在 M-D 之前或同期落地。

6. **保持 draft-first 不动摇。** 所有新红线都以"降级改写 + 显式标注"实现，而不是"拒绝出稿"。`scholarly` profile 让用户自己选择严格度。

---

## 附录 A：关键源码索引

| 主题 | 路径:行 |
|---|---|
| 一键全流程编排 | `services/worker/paperforge_worker/worker.py:989-1109` |
| 文献阶段（无 ingest） | `worker.py:200-270`，卡片调用 `worker.py:252` |
| 独立 ingest 任务 | `worker.py:520-575`，`acquire_fulltexts` 唯一调用点 `worker.py:554` |
| 全文抓取与解析 | `services/worker/paperforge_worker/pipelines/fulltext.py:55-141`，定位标记 `:154-175` |
| OA URL 规划 | `packages/scholar_gateway/scholar_gateway/fulltext.py:88-158` |
| PDF 解析（纯文本） | `packages/ingest/ingest/pdf.py:50-108` |
| section 感知切块（死代码） | `packages/ingest/ingest/section_chunks.py:103,146,221` |
| 卡片抽取 | `pipelines/cards.py:87-216`，预算 `:23,203,208` |
| 卡片落库模型 | `packages/db/db/models/library.py:138-158` |
| 写作上下文构造 | `pipelines/document.py:774-795` |
| 写作 prompt 组装 | `pipelines/writing.py:326-391`，证据规则 `:394-476` |
| 写作系统提示词 | `pipelines/writing.py:42-76` |
| 大纲生成 | `pipelines/outline.py:102-152`，提示词 `:25-61`，回退 `:275-317` |
| 跨研究比较章节 | `pipelines/outline.py:322-395` |
| 论断分类 | `pipelines/quality.py:143-156` |
| claim–evidence 锚点 | `pipelines/quality.py:159-231` |
| 就绪门与 profile 分支 | `pipelines/quality.py:274-455`，关键分支 `:449` |
| 支撑度打分 | `pipelines/quality.py:503-509` |
| 数值 lint | `pipelines/document.py:363-386`，`packages/ingest/ingest/numlint.py` |
| 范围/研究问题生成 | `pipelines/scope.py:23-70` |
| ingest API 端点 | `services/api/paperforge_api/routers/writing.py:704-721` |
| 前端阶段枚举 | `apps/web/lib/pipeline.ts:142-152`，`apps/web/lib/types.ts:192-232` |
