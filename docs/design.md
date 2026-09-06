# 独立科研论文生成系统设计方案(工作名:PaperForge)

- 日期:2026-07-24
- 状态:设计提案(待评审)
- 来源:从 DeepSearch 中分离文献综述能力,重建为独立的"科研论文生成系统"
- 决策输入:全新技术栈自由设计;产出 LaTeX + PDF;同时支持"用户提供研究成果写作"与"纯 AI 生成初稿"两种模式;引用必须真实,但放弃全链路溯源的流程性强制门槛

---

## 0. 摘要

DeepSearch 现有文献综述子系统(`literature_review/` 约 2.4 万行 + 29 张专属表)是一个**以可审计溯源为最高目标**的 PRISMA 级系统:形式化完成(formal completion)要求锁定检索源全部穷尽、筛选裁决全部落库、每个 finding 通过语义支持验证、每个实质段落可回溯到 claim→finding→chunk→snapshot 五级链路。这套契约对"系统性综述的可复现性"是正确的,但对"高质量论文成稿"是巨大的负担:大量算力和流程花在证据审计上,而真正决定论文质量的写作、结构、行文连贯性反而被 id-grounded 逐段绑定约束得难以展开。

新系统的定位反转为:**成稿优先(draft-first)、引用真实(citation-authentic)、默认流程宽松**。
M11 在不破坏这一默认值的前提下增加可选的严格投稿质量门；任务执行成功与论文投稿就绪
是两个独立状态。

- 保留并迁移的核心资产:五大学术检索适配器(OpenAlex/Crossref/Semantic Scholar/arXiv/EuropePMC,含限速/熔断/缓存)、标识符规范化、确定性去重、引文雪球扩展、OA 全文获取、PDF/文档解析、section 感知切块、引用格式化、LLM Provider 抽象、主题聚类与结构化写作的 JSON-schema 校验模式、PostgreSQL/Alembic/对象存储基建约定。
- 明确摈弃:PRISMA 流程守恒、多阶段筛选裁决、finding 语义验证、synthesis claim 类型规则、研究质量评估(study appraisal)、效应量/分类学机器、检索 lane 穷尽账目、语料冻结、formal/diagnostic/insufficient-evidence 状态机、通用 OSINT 管线(claims/research_quality/slides 等约 9 万行)。
- 引用真实性由"三条硬规则"保障(检索入库核验、cite-key 白名单校验、参考文献确定性生成),取代全链路溯源。
- 新栈:FastAPI(Python,最大化复用)+ ARQ Worker + PostgreSQL + Redis + MinIO 后端;Next.js 15 + TypeScript + Tailwind/shadcn + Tiptap 前端;Tectonic 沙箱编译 LaTeX→PDF。
- 两条产品管线:综述论文管线(主题→检索→文献库→大纲→分节写作→引用校验→LaTeX/PDF)与研究型论文管线(用户素材摄取→相关工作检索→IMRaD 写作→数字一致性 lint→LaTeX/PDF)。
- 学术诚信红线:系统不伪造实验数据;纯 AI 模式下实验结果以显式占位符呈现;导出附 AI 辅助声明选项。

---

## 1. 背景与目标

### 1.1 现状

DeepSearch 是一个 evidence-first 的深度研究/OSINT 平台,文献综述作为 `research_task.task_type = "literature_review"` 的协议驱动管线嵌在其中:

```text
review_protocol -> review_search_strategy -> scholarly_result_occurrence -> scholarly_work
-> dedupe_cluster/dedupe_conflict -> 多阶段 screening_decision -> review_corpus_snapshot(冻结)
-> extracted_finding/study_characteristic -> evidence matrix
-> synthesis_claim/claim_evidence_link -> review_report_ir -> review_markdown
```

其质量契约(`docs/literature-review-quality-contract.md`)将产出分为 formal review / diagnostic artifact / insufficient evidence / needs adjudication 四类,formal 完成需通过 12 项硬门槛(引用精度 ≥0.95、PRISMA 守恒、必检源终态覆盖 =1.00、严重语义失配 =0 等)。

### 1.2 问题:溯源目标与成稿目标的冲突

| 维度 | 现系统(溯源优先) | 论文生成需要(成稿优先) |
|---|---|---|
| 首要产出 | 可审计的证据账本 | 一篇结构完整、行文专业的论文 |
| LLM 角色 | 被限制在 schema 校验的语义判断,逐段绑定 claim_id | 承担主要写作职责,自由组织论证与行文 |
| 失败语义 | 门槛不过 → INSUFFICIENT_EVIDENCE,拒绝正式产出 | 永远产出草稿,质量问题以提示呈现 |
| 检索语义 | 锁定协议、lane 穷尽账目、occurrence 守恒 | 覆盖面够用即可,支持随时补充检索 |
| 筛选语义 | 多阶段裁决 + 排除理由枚举 + 人工仲裁 | 相关性排序 + 用户勾选 |
| 引用语义 | 段落→claim→finding→chunk→snapshot 五级链 | 引用真实、格式正确、位置合理 |

结论:不是"放松旧系统参数",而是**换一个第一性目标重建系统**,同时把旧系统中与目标无关的确定性资产(检索、规范化、去重、解析、格式化)平移过来。

### 1.3 新系统定位

**PaperForge:AI 科研论文写作平台**——面向研究生/科研人员,输入研究主题或自有研究成果,产出可直接投稿级排版(LaTeX/PDF)的论文初稿,全部参考文献真实可溯。

支持的论文类型:

1. **综述论文(review)**:narrative review / survey / state-of-the-art review。系统自动完成文献调研与全文写作。
2. **其他论文(original)**:方法/实验/系统/立场类论文。两种模式:
   - **成果写作模式**:用户上传实验数据、方法描述、图表、贡献点,系统完成相关工作调研并组织成完整论文(Introduction / Related Work / Method / Experiments / Conclusion)。
   - **纯生成模式**:仅给定题目与方向,系统检索文献并生成完整初稿;涉及实验结果处生成实验设计与显式 `TODO` 占位符,**不编造数据**。

### 1.4 设计原则

1. **Draft-first(分级门禁)**:任何阶段失败都降级而不阻断;永远能拿到当前最好的稿子。
   唯一的例外是**语料根本不成立**——没有任何研究子问题,或全项目可用全文证据凑不出
   两个独立来源——这时出稿没有意义,任务停在 `needs_input`(实现见
   `pipelines/readiness.py` 的 `BLOCKING_CODES`)。覆盖率没达标属于**降级项**:照常出稿,
   缺证的子问题写成显式的「现有证据不足以回答」小节。
   `review_style="systematic"` 与 `quality_profile="submission"` 是用户显式要求的严格产出,
   覆盖率缺口在那里意味着选择偏差,不参与降级(`STRICT_BLOCKING_CODES`)。
2. **引用必须真实**:每条参考文献必须对应真实存在、经元数据核验的文献;LLM 永不手写参考文献条目。
3. **确定性代码管格式,LLM 管内容**:BibTeX、编号、图表渲染、模板、数字一致性由代码保证;论证与行文由 LLM 生成(这一分工原则继承自旧系统,是其最有价值的架构遗产)。
4. **人机协作节点显式化**:检索圈选、大纲确认、章节改写是产品交互点,不是流程裁决点;全部可跳过(全自动模式)。
5. **合规获取**:全文仅经 OA/官方 API 渠道获取,不绕 paywall/robots(继承旧系统合规策略)。
6. **不伪造数据**:原创研究的实验数值只能来自用户素材的确定性解析或显式占位；综述中的
   文献数值必须来自可定位全文证据，摘要只能支持背景陈述。
7. **双模式就绪语义**:`draft` 永远交付当前最好草稿；`submission` 要求核心论断全文定位、
   元数据确认、章节审批和最终 PDF 版面检查全部通过。

---

## 2. 现有系统能力盘点(代码审计结论)

### 2.1 后端模块地图(`services/orchestrator/app/`,行数为实测)

| 模块 | 行数 | 职责 | 与新系统关系 |
|---|---:|---|---|
| `literature_review/` | 23 869 | 综述管线全部逻辑 | **主要迁移来源**(详见 §3) |
| `services/` | 30 010 | 任务状态机、worker、各阶段编排 | 摈弃,借鉴 checkpoint 模式 |
| `reporting/` | 15 866 | Markdown/ReportIR/grounded 校验 | 摈弃,ReviewReportIR 思想改造为 PaperIR |
| `research_quality/` | 14 868 | answer-slot/agentic loop/gap critic | 摈弃 |
| `api/` | 9 493 | REST 路由 | 摈弃,新写(参考 schema 风格) |
| `slides/` `figures/` `presentation_ir/` | 10 144 | PPT/图表产物 | 摈弃(远期可回迁) |
| `acquisition/` | 4 530 | 采集路由/HTTP 策略/浏览器回退 | 少量复用(HTTP 客户端与策略) |
| `planning/` | 4 326 | 通用研究计划器 | 摈弃 |
| `claims/` | 3 329 | 逐句 claim 起草/验证 | 摈弃 |
| `parsing/` | 3 065 | HTML/PDF/DOCX/PPTX/XLSX 抽取与切块 | **复用** |
| `search/` | 2 881 | SearXNG/通用 web 检索 | 摈弃(学术检索走 adapters) |
| `indexing/` | 1 187 | OpenSearch 块索引 | 摈弃(V1 不需要;远期库内全文检索再评估) |
| `llm/` | 667 | OpenAI-compatible provider seam | **复用** |
| `entity_resolution/` `artifacts/` `storage/` | 2 564 | 实体消歧/导出清单/对象存储 | storage 复用,其余摈弃 |

### 2.2 数据模型

- 通用账本 14 张表(`packages/db/models/ledger.py`):research_task/run/task_event/search_query/candidate_url/fetch_job/fetch_attempt/content_snapshot/source_document/source_chunk/citation_span/claim/claim_evidence/report_artifact。→ 除借鉴 task/event/checkpoint 模式外整体摈弃。
- 综述 29 张表(`packages/db/models/literature_review.py`,1 450 行)。→ 保留 scholarly_work 系四张 + http 缓存表;其余摈弃(见 §3.3)。

### 2.3 前端(`apps/web`,React 18 + Vite)

Protocol 编辑器/Builder 卡片、检索策略视图、证据矩阵、ReportIR Inspector、任务工作台等。新前端整体重写(Next.js),旧组件作为交互参考;`sourceCapabilities.ts`(各学术源能力元数据)逻辑可直接移植。

### 2.4 基建

PostgreSQL 16 + Alembic、MinIO/文件系统对象存储、OpenSearch(新系统不用)、SearXNG(不用)、JSON 日志与指标(`packages/observability`,复用)、host-local worker 模式(借鉴)。

---

## 3. 复用 / 改造 / 摈弃清单(核心)

判定标准:(a) 与"引用真实 + 成稿质量"直接相关 → 复用;(b) 思想正确但耦合旧账本或过严 → 改造;(c) 仅服务于溯源审计/流程裁决 → 摈弃。

### 3.1 直接复用(搬迁级,改动小)

| 旧路径(`services/orchestrator/app/` 或 `packages/`) | 能力 | 迁移目标 | 必要改动 |
|---|---|---|---|
| `literature_review/adapters.py`(1 961 行) | Crossref/OpenAlex/Semantic Scholar/arXiv/EuropePMC 五源检索;请求 pacing、熔断器、失败诊断 | `packages/scholar_gateway/providers/` | 按 provider 拆文件;去掉 occurrence 落账;配置改 pydantic-settings |
| `literature_review/normalization.py` | DOI/PMID/PMCID/arXiv/OpenAlex/S2/CorpusId 规范化、标题哈希、Jaccard | `packages/scholar_gateway/normalize.py` | 纯函数,零改动 |
| `literature_review/dedupe.py`(563 行) | 确定性去重:标识符精确匹配 → 标题哈希 → 标题相似+作者+年份 | `packages/scholar_gateway/dedupe.py` | 删除 dedupe_conflict 人工仲裁流,低置信冲突直接保留双记录并打标 |
| `literature_review/snowball.py`(674 行) | OpenAlex/S2 引文雪球(references + citations)、引文影响力排序、核心文献选择 | `packages/scholar_gateway/snowball.py` | 换缓存句柄;Related Work 发现的核心武器 |
| `literature_review/oa_fulltext.py`(443 行) | OA 全文 URL 规划(arXiv PDF / EuropePMC PDF / doi.org / work_url)与获取 | `packages/scholar_gateway/fulltext.py` | 解耦 ledger 写入(search_query→…→source_chunk 链改为直接写 document_file 对象存储) |
| `literature_review/http_cache.py` + `scholarly_http_cache` 表 | 学术 API 响应缓存 | `packages/scholar_gateway/cache.py` | 保留表结构 |
| `literature_review/citation_format.py` | APA / GB/T 7714-2015 / author-year 引用格式化,中文感知 | `packages/paper_ir/citation_style.py` | 新增 `bibtex.py`(pybtex 确定性生成)与 CSL-JSON 导出 |
| `literature_review/section_chunks.py`(329 行) | section 线索组(methods/results/discussion/limitations)、内容角色分类(作者署名/纯引用/样板文本识别)、Markdown 表格解析、字符预算选块 | `packages/ingest/section_chunks.py` | 直接复用,服务于文献卡片抽取 |
| `literature_review/json_utils.py` | LLM JSON 输出净化 | `packages/llm_runtime/json_utils.py` | 零改动 |
| `literature_review/report_i18n.py` | 中/英报告语言辅助 | `packages/paper_ir/i18n.py` | 直接复用 |
| `llm/`(client/providers/types) | OpenAI-compatible provider seam(智谱 GLM / DeepSeek 等即插即用)、noop provider | `packages/llm_runtime/` | 扩展:多模型角色路由、流式输出、并发限额、成本记账钩子 |
| `parsing/document_extractors.py`、`chunking.py`、`quality.py` | PDF/DOCX/HTML/纯文本抽取(标准库实现,不执行宏)、稳定切块、质量评分 | `packages/ingest/` | 直接复用;V2 评估 GROBID/marker 升级 PDF 结构化质量 |
| `packages/db`(base/session/repositories 模式 + Alembic 约定) | ORM 基类、会话、仓储模式、迁移纪律 | 新仓库 `packages/db/` | 沿用约定,模型全新 |
| `packages/db/models/literature_review.py` 中 `scholarly_work`/`work_identifier`/`work_url`/`work_author` 四表 | 文献实体与标识符/作者/URL 归一 | `packages/db/models/library.py` | 保留字段(含 is_retracted/oa_status/license);去掉 source_priority 等 lane 相关字段 |
| `packages/observability` | JSON 日志、请求指标 | 原样 | 零改动 |
| `infra/`(postgres/minio compose 片段) | 基建编排 | 新仓库 `infra/` | 裁剪(去 OpenSearch/SearXNG/caddy/grafana,按需回加) |
| `apps/web/.../sourceCapabilities.ts` | 各学术源能力/筛选项元数据 | 新前端 `lib/sourceCapabilities.ts` | 移植为 TS 模块 |

### 3.2 改造复用(借架构思想,重实现)

| 旧模块 | 保留的思想 | 改造方向 |
|---|---|---|
| `protocol_generation.py`(1 159 行) | LLM 起草 + 确定性回退 + 严格 JSON schema 校验的"计划生成"模式 | 改为 **Scope 生成器**:主题 → 研究范围、检索计划、关键词矩阵、时间窗。去掉 PICO/PECO 强制框架、协议锁定(locked)语义、版本裁决;产物可编辑、可随时重生成 |
| `topic_relevance.py`(624 行) | 实体锚定的主题相关性评分、facet 覆盖 | 改为**检索结果排序器**:确定性打分 + LLM 对 top-N 重排,替代多阶段筛选;分数只用于排序与推荐,不做硬性纳入/排除 |
| `llm_synthesis.py` 中 ThemeBundle/主题聚类 | 主题提议→合并→孤儿分配→重分区的聚类框架 | 改为**大纲生成器**:文献卡片聚类 → 章节树(章节标题 + 分配到各章的文献集合 + 论证要点);去掉 synthesis_claim 类型与证据数量规则 |
| `llm_extraction.py` + `evidence_matrix.py` 的 ReviewEvidenceCard | 结构化"文献卡片"(方法/数据集/结果/局限/引文位置) | 改为 **literature_card**:每篇文献一张卡(贡献/方法/结果/局限/可引用要点),作为写作上下文;去掉 verifier_status、五级证据指针,仅保留 work_id + 可选页码/章节定位 |
| `llm_review_writer.py` | 分节生成 + Pydantic schema 校验 + 段落携带引用 id 列表的**结构化写作模式** | 改为 **Section Writer**:段落携带 `cite_keys`(而非 claim_ids);校验规则从"每段绑定已验证 claim"放宽为"cite_keys ⊆ 文献库白名单";新增连贯性重写 pass |
| `reporting/review_report_ir_schema.py` / builder / `review_markdown.py` | IR → 渲染分离;渲染器消费 IR 而非 LLM 原始输出 | 重设计为 **PaperIR**(§4.5):支持 LaTeX 语义(section 树/公式/图表/algorithm/cite 节点),渲染目标从 Markdown 换为 LaTeX 为主、Markdown 预览为辅 |
| worker + `task_event` + `research_run.checkpoint_json` | 可恢复长任务:阶段 checkpoint、事件流、断点续跑 | 简化重写为 `generation_job`(§4.3):ARQ 任务 + 阶段 checkpoint + SSE 进度;去掉 QUEUED/PAUSED 完整状态机中与裁决相关的态 |
| `acquisition/http_client.py` + 采集策略 | SSRF 防护、超时/重定向/响应大小上限、礼貌 UA | 收敛为 `scholar_gateway/http.py` 的安全 HTTP 客户端;去掉浏览器回退、域熔断账本 |
| `literature_review/gap_analysis.py` | 覆盖缺口发现 | 降级为**建议器**:「该章节引用密度低/缺少 2023 年后文献/某主题未覆盖」类提示,绝不阻断 |

### 3.3 明确摈弃(及理由)

| 摈弃项 | 旧位置 | 摈弃理由 |
|---|---|---|
| PRISMA 流程守恒计算 | `prisma.py`、occurrence 账本 | 仅系统性综述的方法学要求;narrative/survey 不需要。检索统计降级为展示性面板 |
| 多阶段筛选裁决 | `screening.py`、`llm_screening.py`、`screening_decision`/`exclusion_reason` 表 | title-abstract→retrieval→full-text→final 四阶段 + 理由枚举 + 人工仲裁,成本高且不改善成稿质量。替代:相关性排序 + 用户勾选 |
| finding 语义支持验证 | `finding_verification.py`、`finding_extraction_attempt` 表 | 卡片是写作上下文而非受审计事实,不需要逐条 support/contradict 验证 |
| synthesis claim 类型规则 | `synthesis_validation.py`、`synthesis_claim`/`claim_evidence_link`/`finding_synthesis_disposition` 表 | consensus 需 N 篇独立支持等规则改为写作 prompt 中的软性指引 |
| 研究质量评估 | `study_quality.py`、`study_quality_assessment`/`quality_domain_judgment` 表 | 正式 appraisal 属系统性综述功能,V1 不做;远期可作为可选模块回加 |
| 效应量/可比性/分类学机器 | `taxonomy_effects.py`、`structured_effect_estimate`/`effect_comparability_*`/`controlled_vocabulary_term`/`vocabulary_alias`/`evidence_taxonomy_assignment` 表 | 元分析级机器,超出论文写作平台范围 |
| 检索 lane 计划与穷尽账目 | `search_lane_plan.py`、`review_search_strategy` 的 lane 家族/版本/穷尽语义 | "必检源终态覆盖 =1.00"是形式化完成的要求;新系统只记录轻量 `search_run` 供复现参考 |
| 语料冻结 | `review_corpus_snapshot`、corpus 版本不可变语义 | 文献库是活的工作集,支持随时增删 |
| occurrence 守恒 | `scholarly_result_occurrence` 表 | 每条 provider 命中的守恒账本仅服务 PRISMA |
| formal/diagnostic/insufficient-evidence 状态机 | `terminal_status.py`、`quality_decision.py`、`quality_metrics.py`、`quality_benchmark.py` | 四态裁决机不迁移;质量以评分报告呈现。**注意**:这不等于"永不阻断"——正文写作前保留一个分级门禁(§1.4 原则 1),只有语料根本不成立才停在 `needs_input`,覆盖率缺口一律降级出稿 |
| 通用 OSINT 管线 | `search/`(SearXNG)、`planning/`、`claims/`、`research_quality/`、`reporting/` grounded 校验、`slides/`、`figures/`、`presentation_ir/`、`artifacts/`、`indexing/`、`entity_resolution/`、crawler/reporter/openclaw 服务 | 属于深度研究平台,与论文生成无关;实测约 9.4 万行不迁移 |
| 逐段证据绑定渲染门槛 | review writer 的 claim_id 强制 + 段落级校验失败拒绝渲染 | 被引用三硬规则替代(§4.4.3) |

### 3.4 旧机制 → 新机制对照

| 旧机制 | 新机制 |
|---|---|
| 全链路溯源(段落→claim→finding→chunk→snapshot) | 引用三硬规则:入库核验 + cite-key 白名单 + 参考文献确定性生成 |
| formal completion 12 项硬门槛 | 质量评分报告(引用密度/覆盖度/新旧文献比/连贯性),仅提示 |
| 多阶段 screening + 仲裁 | 相关性排序 + 前端一键圈选(全自动模式取 top-K) |
| PRISMA 流程图 | 检索统计面板(每源命中/去重/入库数),可选导出为附录 |
| evidence matrix 硬校验 | 句级论断—全文证据矩阵 + 自动生成的跨研究比较/局限/冲突章节与文献对比表 |
| INSUFFICIENT_EVIDENCE 终态 | 低覆盖警告 + 照常产出草稿 |
| 语料冻结版本 | 文献库工作集 + 导出时快照 bibliography 版本 |

---

## 4. 新系统架构设计

### 4.1 技术选型与理由

| 层 | 选型 | 理由 |
|---|---|---|
| 后端框架 | **Python 3.12 + FastAPI + Pydantic v2** | §3.1 全部可迁移资产为 Python;学术生态(pybtex、pylatexenc、pandas)最全。"全新栈"体现在服务划分与前端,后端语言不换是理性选择 |
| 任务执行 | **ARQ(Redis)worker** | 论文生成是分钟级长任务,需异步、进度流、断点续跑;ARQ 原生 asyncio、轻量,替代旧系统的 host-local 轮询 worker |
| 数据库 | **PostgreSQL 16 + SQLAlchemy 2 + Alembic** | 沿用旧系统约定与迁移纪律,仓储模式直接照搬 |
| 对象存储 | **MinIO / S3 兼容 / 本地文件系统后端** | 存 OA 全文 PDF、用户素材、LaTeX 工程与 PDF 产物;复用旧 storage seam |
| 前端 | **Next.js 15(App Router)+ TypeScript + Tailwind + shadcn/ui** | 论文写作台需要现代编辑体验与流式渲染;RSC 减少样板;组件生态成熟 |
| 编辑器 | **Tiptap(章节富文本,自定义 citation 节点)+ Monaco(LaTeX 源码视图)+ KaTeX(公式预览)** | 引用作为原子 chip 节点插入正文,是引用白名单校验的前端保障 |
| 实时进度 | **SSE**(Server-Sent Events) | 单向进度流足够,免 WebSocket 复杂度 |
| LaTeX 编译 | **Tectonic**(独立容器,无网络、只读模板、资源限额) | 自包含、可沙箱、可缓存包;比完整 TeXLive 轻一个量级 |
| 视觉渲染 | **Matplotlib + Graphviz + Pillow**（独立 `visuald`） | 只接收结构化规格和服务端解析数据；固定字体/配色，无外网，不执行 LLM 代码 |
| LLM | **OpenAI-compatible 多 provider**(迁移自旧 `llm/`) | 智谱 GLM/DeepSeek/GPT/Claude/本地 vLLM 即插即用;按角色路由(§4.9) |
| 图像生成 | **独立 `ImageProvider` seam + registry/factory** | 与文本模型密钥/端点隔离；默认 Cloudflare Workers AI `FLUX.1-schnell`，保留 OpenAI 适配器；只生成概念性位图 |
| 仓库形态 | **独立新仓库 `paper-forge/`,monorepo(uv workspace + pnpm)** | 与 DeepSearch 完全解耦,拷贝式迁移代码与测试,不产生运行时依赖 |

### 4.2 系统拓扑

```text
paper-forge/
├── apps/web                  # Next.js 15 前端
├── services/api              # FastAPI:项目/文献库/大纲/章节/导出 REST + SSE
├── services/worker           # ARQ:检索、摄取、卡片、大纲、写作、编译各管线任务
├── services/texd             # Tectonic 编译沙箱(HTTP 微服务,无外网)
├── services/visuald          # 图表/示意图渲染与 AI 位图规范化(无外网)
├── packages/scholar_gateway  # 检索适配器/规范化/去重/雪球/OA 全文/缓存(迁移)
├── packages/ingest           # PDF/文档解析、section 切块(迁移)
├── packages/llm_runtime      # provider seam、角色路由、JSON 校验、成本记账(迁移+扩展)
├── packages/paper_ir         # PaperIR schema、引用样式、BibTeX、i18n(迁移+新写)
├── packages/latex_render     # PaperIR -> LaTeX 工程渲染、模板库、编译修复
├── packages/visuals          # Chart/Diagram/AIImage 规格、visuald 客户端与 ImageProvider
├── packages/db               # 模型/仓储/迁移(约定迁移,模型新写)
├── packages/observability    # JSON 日志/指标(迁移)
└── infra/                    # postgres + redis + minio + texd + visuald compose
```

运行时数据流:`web → api → (db/redis) → worker → [scholar_gateway | ingest | llm_runtime | visuals → visuald | latex_render → texd] → db/minio → SSE → web`。

### 4.3 核心域模型(Schema 草案,首个迁移)

```text
-- 项目与任务
paper_project(id, owner_id, title, paper_type{review|original}, writing_mode{auto|assisted},
              language{zh|en}, venue_template, citation_style, status, scope_json, created_at, updated_at)
generation_job(id, project_id,
               kind{search|ingest|cards|qdecomp|evidence|qmatrix|synth|outline|write|compile|visual|full},
               status{queued|running|succeeded|failed|cancelled}, progress, stage,
               checkpoint_json, error_json, created_at, finished_at)
job_event(id, job_id, seq, event_type, payload_json, created_at)          -- SSE 源,借鉴 task_event

-- 文献域(scholarly_work 系四表自旧系统迁移,字段基本不变)
scholarly_work(id, canonical_title, normalized_title_hash, abstract, publication_year, publication_date,
               work_type, venue_name, publisher, language, doi, pmid, pmcid, arxiv_id, openalex_id,
               semantic_scholar_id, corpus_id, is_retracted, oa_status, license, created_at, updated_at)
work_identifier / work_url / work_author                                   -- 原样迁移
scholarly_http_cache                                                       -- 原样迁移

library_entry(id, project_id, work_id, status{candidate|selected|excluded},
              relevance_score, rank_reason_json, user_pinned,
              added_via{search|snowball|doi_import|bibtex_import|llm_suggested_verified},
              bibtex_key UNIQUE(project_id, bibtex_key), verified_at, created_at)
literature_card(id, project_id, work_id, summary, contributions_json, methods_json,
                results_json, limitations_json, quotable_points_json, fulltext_used,
                extraction_model, source_hash, created_at)                 -- source_hash 支持跨项目缓存复用
document_file(id, work_id, kind{oa_pdf|html|xml}, object_key, mime, bytes, fetched_from_url, created_at)

-- 用户素材(研究型论文)
user_asset(id, project_id, kind{dataset|result_table|figure|method_note|code|bib},
           title, description, object_key, parsed_json, created_at)
visual_asset(id, project_id, kind{chart|diagram|ai_image}, generation_status, review_status,
             title, caption, alt_text, target_section_key, insertion_hint_json, figure_label,
             spec_json, provider, model, error_json, renditions_json, content_hash, input_hash,
             document_version, version, supersedes_id, created_at, updated_at)
visual_source_asset(visual_asset_id, user_asset_id, source_hash)            -- RESTRICT 删除保护
visual_generation_attempt(id, visual_asset_id, provider, model, request_id, latency_ms,
                          output_width, output_height, usage_json, cost_estimate, error_code, created_at)

-- 论文结构
outline(id, project_id, version, tree_json, status{draft|confirmed}, created_at)
paper_document(id, project_id, version, outline_id, status, created_at)
paper_section(id, document_id, section_key, parent_key, order_no, title,
              body_ir_json, cite_keys_json, asset_refs_json,
              status{generated|edited|approved}, model, updated_at)
citation_usage(id, project_id, section_id, work_id, cite_key, context_snippet, created_at)

-- 检索留痕(轻量复现,非账本)
search_run(id, project_id, provider, query_text, filters_json, hit_count,
           retrieved_count, status, error, executed_at)

-- 产物与成本
export_artifact(id, project_id, document_version, format{latex_zip|pdf|docx|bibtex|markdown|markdown_bundle},
                object_key, compile_log_key, content_hash, created_at)
llm_call_log(id, project_id, job_id, role, model, input_tokens, output_tokens,
             cost_estimate, latency_ms, created_at)                        -- 简化自旧 token_ledger 设计
```

对比旧系统:29 张综述表 → M0 保留 5 张(scholarly_work 系 + 缓存)、新增 14 张，共 19 张；M8 再新增 3 张视觉表，共 22 张。删除的 24 张全部属于筛选裁决/守恒账本/质量评估/效应量域。

### 4.4 论文生成管线

#### 4.4.1 综述论文管线

```text
INIT     题目/方向、语言、模板、篇幅偏好、模式(全自动/协作)
SCOPE    LLM 生成研究范围+关键词矩阵+时间窗+子主题(可编辑;确定性回退)     [改造自 protocol_generation]
QDECOMP  核心问题 → 可编辑子问题树 + 比较维度 + 预期证据类型
SEARCH   五源并行检索 → 规范化 → 去重 → 相关性排序(确定性分 + LLM top-N 重排) [adapters/normalization/dedupe/topic_relevance]
CURATE   协作模式:用户圈选入库;全自动:top-K 入库。对种子文献做雪球扩展一轮  [snowball]
INGEST   OA 全文获取 → 解析 → section 感知切块;无全文则摘要级降级          [oa_fulltext/ingest/section_chunks]
CARDS    每篇入库文献抽取 literature_card(贡献/方法/结果/局限/可引要点)      [llm_extraction 模式]
EVIDENCE 全文/摘要 → A/B/C/D 级 evidence_unit + 可比较 measurement
QEMATRIX 子问题 × 候选证据 → stance/condition/confidence；人工覆盖优先
SYNTH    同 comparability_key 内判定一致/条件差异/冲突；异 key 禁止横比
OUTLINE  子问题综合包 → 章节树；按问题回退，禁止退化成年代/逐篇结构
WRITE    逐章节结构化生成(§4.4.3 引用约束)→ 摘要/引言/结论后写;每写完一节立刻落库
POLISH   全文连贯性 pass(独立阶段,逐节报进度)。默认开启,用户可中途跳过:
         已润色的章节保留,剩余章节按初稿交付
CITECHK  确定性引用审计 + 可选语义相关性软检查(仅出提示)
VISUAL_PLAN 全文完成后最多生成 6 条视觉建议；不调用付费生图、不改 PaperIR、不阻断管线
RENDER   PaperIR → LaTeX 工程 + BibTeX → Tectonic 编译 → PDF(有界自动修复)
REVIEW   编辑器内人工修改/局部重生成/润色 → 再导出
```

WRITE 阶段上下文构造(长文一致性关键):全局大纲 + 本章问题对齐证据簇
(优先 A/B 级并携带 evidence_id/定位/measurement/comparability_key)
+ 相邻章节滚动摘要 + 术语表。写后与质量阶段分别执行 R4 证据等级、R5 可比性、
R6 数字定位校验。

质量档位与「一键跑通全流程」的关系：一键入口默认 `draft`——同样跑完整质检、
发现项一条不少，但不把它们升级成阻断项，因此一定产出导出件。要不要为这些发现项
再花一轮「重写未达标章节 + 全文重新评估」（最坏两轮改写、四次重评），由用户在
交付之后通过 `quality_repair.available` 决策点显式选择，与可选润色同形。
`scholarly` / `submission` 是用户主动选择的严格档，那两档保留质量门与管线内
自动收敛：未达标就不产出导出件。

#### 4.4.2 研究型论文管线

```text
INIT     论文类型(方法/实验/系统/立场)、模板、贡献点表述
INPUT    素材上传:结果表格(CSV/XLSX)、图(PNG/PDF/SVG)、方法笔记(MD/DOCX)、代码片段、
         已有 .bib —— 确定性解析入 user_asset.parsed_json                    [ingest 复用]
SEARCH+  相关工作定向检索(基线方法、同题工作、背景文献),同综述管线但规模小
OUTLINE  IMRaD 模板实例化:Abstract/Intro/Related Work/Method/Experiments/Results/Discussion/Conclusion
WRITE    Related Work ← 文献库;Method/Experiments ← user_asset;
         数值硬规则:正文数字必须来自 parsed_json 的确定性注入(模板槽位),LLM 不得改写数字;
         表格由代码从 parsed_json 渲染为 booktabs；上传图直接 \includegraphics；
         数据图仅由 ChartSpec + parsed_json 确定性生成，LLM 只能建议规格、caption 与分析
NUMLINT  数字一致性 lint:扫描正文数值 vs 素材解析值,失配即标记
CITECHK / RENDER / REVIEW  同综述管线
```

纯生成模式差异:跳过 INPUT;Experiments/Results 生成实验设计与 `\todo{待补充实验数据}` 占位符,正文明确标注"结果待实验补充"。**系统在任何路径下都不生成虚构实验数值**——这是产品红线,也写入 prompt 与 lint 双层防护。

#### 4.4.3 引用真实性三条硬规则(取代全链路溯源)

- **R1 入库核验**:library_entry 只能来自 (a) provider 真实检索响应(带 provider record id);(b) DOI/BibTeX 导入且经 Crossref/OpenAlex 反查核验;(c) LLM 建议的文献,必须先反查(DOI 存在或标题模糊匹配 ≥0.90 + 作者/年份吻合)并成功解析为 scholarly_work 才准入,失败即丢弃并记录 `verification_failed`。写作白名单只由 `db.repositories.library.get_writing_whitelist` 生成,固定要求 `status='selected'`、`verified_at IS NOT NULL`、`bibtex_key IS NOT NULL` 且文献未撤稿。
- **R2 写作约束**:Section Writer 输出 schema 中每段 `cite_keys` 必须 ⊆ 项目白名单;JSON 净化第一轮以 `report` 模式返回路径与越权 key,触发该段落带错误信息重写一次;二次违规以 `strip` 模式删除并把 `citation_warnings` 持久化到 Section IR,供编辑器显示「引用已移除」。解析为 PaperIR 后必须再执行 typed-IR 白名单检查,覆盖 CiteRun 的 `keys` 字段。前端 Tiptap 引用 chip 只能从白名单选择,人工编辑同样无法引入幻觉引用。
- **R3 参考文献确定性生成**:BibTeX/GB/T 7714 参考文献表由 `paper_ir/bibtex.py` 从库内元数据生成(pybtex),LLM 在任何环节都不书写、不修改参考文献条目;bibtex_key 在核验入库时按固定顺序生成一次(`firstauthor2024keyword`,冲突加后缀)并写入 `library_entry.bibtex_key`,渲染期只消费持久化 key,不再重算。

可选软校验(不阻断):对每个引用位置,用 cheap 模型比对上下文与该文献摘要的语义相关性,低分处在编辑器中显示黄色提示徽章。这继承了旧系统"确定性代码管事实边界"的精神,但把执行点从"渲染前拒绝"移到"编辑器内提示"。

### 4.5 PaperIR(结构化论文中间表示)

继承旧系统"渲染器只消费 IR、不消费 LLM 原始输出"的原则,面向 LaTeX 重新设计:

```jsonc
{
  "meta": { "title", "authors": [], "abstract", "keywords": [], "language", "venue_template" },
  "sections": [
    { "key": "s1", "level": 1, "title": "引言",
      "blocks": [
        { "type": "paragraph", "runs": [ {"t":"text","v":"..."}, {"t":"cite","keys":["wang2023survey"]},
                                          {"t":"math_inline","v":"O(n\\log n)"} ] },
        { "type": "equation", "latex": "...", "label": "eq:loss" },
        { "type": "figure", "asset_ref": "va_a1b2c3d4", "caption": "...", "alt_text": "...",
          "label": "fig:va_a1b2c3d4", "width": "column" },
        { "type": "paragraph", "runs": [ {"t":"text","v":"如"},
          {"t":"xref","target":"fig:va_a1b2c3d4","kind":"figure"}, {"t":"text","v":"所示"} ] },
        { "type": "table", "source": {"kind":"user_asset","ref":"ua_7"}, "caption": "...", "label": "tab:results" },
        { "type": "algorithm", "latex": "..." },
        { "type": "todo", "text": "待补充实验数据" }
      ] }
  ],
  "bibliography": { "style": "gbt7714|ieee|apa", "entries_from": "library" }   // 条目渲染期注入,IR 不内嵌
}
```

要点:cite/xref 是原子节点而非正文字符串；figure 同时支持旧 `ua_*` 上传图和新 `va_*` 生成图。
LaTeX 使用不变 label + `\\ref`，Markdown/DOCX 按 PaperIR 遍历顺序确定性编号。服务端保存章节时
重新计算 `asset_refs_json`并校验 label 全文唯一，不信任客户端派生字段。LLM 生成的自由 LaTeX
仅允许出现在 equation/algorithm 块且过白名单环境校验。

### 4.6 LaTeX 渲染与模板体系

- 模板库 V1:IEEEtran、acmart、Elsevier(elsarticle)、Springer(llncs)、通用中文学位论文/学报模板、无格式 article。模板 = 主 `.tex` 骨架 + 环境白名单 + 宏包锁定清单,LLM 不可修改导言区。
- 视觉渲染：`ChartSpec/DiagramSpec → visuald → SVG/PDF/PNG`；AI 图先经独立 `ImageProvider`，再由 visuald
  校验、去元数据并统一为 PNG。文件按内容哈希存储，每次重生成建新版本，不原地覆盖。
- AI 图业务管线只调用 `create_image_provider(ImageProviderConfig)` 与统一 `generate/close` 协议；
  Cloudflare、OpenAI 以及未来 Gemini/ComfyUI 仅在 provider 层实现请求、响应和就绪判定，worker、
  VisualAsset、审核插入、来源记录与导出层不含厂商分支。缺少厂商必填配置时只关闭 AI 生成入口。
- 渲染:`paper_ir → jinja2 模板 → LatexProject(text_files + binary_files) → texd(Tectonic) → PDF + 编译日志`。
  texd 对 base64 二进制文件校验相对路径、扩展名、magic bytes、单文件 16 MiB、最多 32 个图/总计 64 MiB。
- 编译失败自动修复(有界 ≤2 轮):修复轮只可改文本，二进制图始终原样携带；仍失败则交付 LaTeX 工程 +
  Markdown 预览并展示日志(draft-first)。
- 导出：PDF/LaTeX ZIP 确定性图优先 PDF 矢量版、AI 图用 PNG；DOCX 在 Pandoc 临时目录写入图片并真正嵌入，上传 PDF 图先将首页确定性转换为 300 DPI PNG；
  `markdown_bundle` 打包 Markdown、`figures/` 和 `visual-provenance.json`。

### 4.7 API 草案(REST + SSE,`/api/v1`)

```text
POST   /projects                                  创建项目(type/mode/topic/template/language)
GET    /projects /projects/{id}                   列表/详情
POST   /projects/{id}/scope/generate              生成研究范围      PUT /projects/{id}/scope
POST   /projects/{id}/search/runs                 触发检索(可指定 providers/filters)
GET    /projects/{id}/library?status=candidate    候选/入库文献列表(含排序理由)
POST   /projects/{id}/library/entries             圈选入库 | DOI/BibTeX 导入(触发 R1 核验)
DELETE /projects/{id}/library/entries/{eid}
POST   /projects/{id}/snowball                    雪球扩展
POST   /projects/{id}/assets                      上传素材(multipart)   GET /assets
POST   /projects/{id}/visuals/suggest             全文视觉建议（异步，不调用 AI 生图）
GET    /projects/{id}/visuals                     列出视觉资产/版本/rendition
POST   /projects/{id}/visuals                     手动创建 ChartSpec/DiagramSpec/AIImageSpec
PATCH  /projects/{id}/visuals/{vid}               修改待批准规格与文案
POST   /projects/{id}/visuals/{vid}/generate      异步渲染；AI 图到此才调 provider
POST   /projects/{id}/visuals/{vid}/approve       事务性插入/替换 FigureBlock
POST   /projects/{id}/visuals/{vid}/reject        拒绝建议
POST   /projects/{id}/visuals/{vid}/regenerate    创建下一版本，旧图保持可用
GET    /projects/{id}/visuals/{vid}/renditions/{format}  受权限保护的 SVG/PDF/PNG
POST   /projects/{id}/outline/generate            生成大纲          PUT /projects/{id}/outline
POST   /projects/{id}/generate                    全管线一键生成(kind=full)
POST   /projects/{id}/sections/{key}/generate     单章节(重)生成    PUT /sections/{key}(人工编辑)
POST   /projects/{id}/sections/{key}/refine       润色/扩写/缩写/改语气
GET    /projects/{id}/citations/audit             引用审计报告(R2 结果+软校验徽章)
POST   /projects/{id}/exports {format}            触发导出          GET /exports
POST   /projects/{id}/quality/generate            生成快照质量报告
GET    /projects/{id}/quality/evidence            句级论断—证据明细
PATCH  /projects/{id}/quality/evidence/{anchor}   人工确认/驳回证据
GET    /projects/{id}/jobs/{job_id}/events        SSE 进度流
POST   /projects/{id}/jobs/{job_id}/polish/skip   跳过剩余连贯性润色(幂等)
```

### 4.8 前端信息架构

> 本节是页面级的模块清单。前端的**设计原则、视觉语言与改造计划**见
> `docs/ui-design.md`——其中 §2「已成事实」记录了若干已落实且不应回退的决策，
> §4「明确不做」记录了被驳回的改动及理由。动前端前先读那份。

| 页面 | 内容 | 参考旧组件 |
|---|---|---|
| 项目列表 + 新建向导 | 类型(综述/研究型)→ 主题/贡献点 → 模板/语言 → 模式 | `LiteratureReviewCreateForm`、`ProtocolBuilderCards` 交互 |
| 文献工作台 | 检索结果表(相关性排序、勾选入库)、卡片详情抽屉、雪球推荐区、DOI/BibTeX 导入、检索统计面板 | `EvidenceMatrixView` 表格模式、`sourceCapabilities` |
| 大纲编辑器 | 树形拖拽、每章文献分配、论证要点编辑 | 新写 |
| 写作工作台 | 左:章节树+进度;中:Tiptap 编辑器(引用/figure xref chip、专用 Figure NodeView、公式、AI 润色浮条);右:视觉建议、文献卡片、引用审计 | 新写(核心页面) |
| 素材中心(研究型) | “原始素材 / 图表与插图”双页签；表格解析、图表/示意图/AI 插图非代码向导、预览与版本 | 新写 |
| 导出中心 | 模板切换、编译日志、PDF 预览、LaTeX 工程/docx/bib/Markdown Bundle 下载 | `ArtifactCenter` 思想 |
| 设置 | 文本/图像 provider 状态、引用样式、LLM 与图像调用成本面板；密钥永不回传 | 新写 |

### 4.9 LLM 层设计(角色路由)

| 角色 | 任务 | 模型档位 | 说明 |
|---|---|---|---|
| planner | scope/大纲/主题聚类 | 中档 | JSON schema 严格校验 + 确定性回退(继承旧模式) |
| extractor | 文献卡片抽取 | 便宜 + 长上下文 | 卡片按 work+source_hash 跨项目缓存 |
| reranker | 检索结果 top-N 重排 | 便宜 | 输出仅排序与理由 |
| writer | 章节写作、连贯性 pass | 最强档 | 结构化输出(段落+cite_keys),流式 |
| polisher | 润色/改写/学术语气 | 强档 | 编辑器内交互式 |
| verifier | 引用语义软校验、数字 lint 辅助 | 便宜 | 仅产出提示 |

全部经 `llm_runtime` 单一 seam(迁移自旧 `llm/providers.py`),配置为 `role → provider+model` 映射;`llm_call_log` 记账,项目页展示成本。

### 4.10 账号与租户边界

- 采用共享 PostgreSQL + 项目级行隔离。`app_user` 是租户主体，`paper_project.owner_id`
  为非空 UUID 外键；文献选择、卡片、素材、任务/事件、大纲、正文、引用、视觉、成本和导出
  都沿项目继承私有归属。`scholarly_work`、作者/标识符、HTTP cache 与合法 OA 全文保持共享。
- FastAPI 是唯一认证权威。密码用 Argon2id；随机 opaque session 只以 SHA-256 入库并通过
  Secure/HttpOnly/SameSite=Lax cookie 传递。业务路由只能读取统一授权依赖已按
  `(project_id, owner_id)` 命中的项目；匿名返回 401，外租户与不存在统一返回 404。
- 私有对象键为 `users/{user_id}/projects/{project_id}/{assets|visuals|exports}/...`，公共 OA
  为 `shared/oa/...`。bucket 不公开，预览、下载与 SSE 都经过认证 API。
- 浏览器只走同源 `/api/v1`；危险方法校验 Origin。注册、登录、找回按 IP 与邮箱 Redis
  限流并失败关闭。生产禁用示例数据，设置接口只报告能力/模型/密钥是否配置，不返回内部地址。

---

## 5. 代码迁移策略

原则:**拷贝式迁移,不做运行时依赖**。新仓库不 import DeepSearch;每个迁移模块连同其单测一起搬,搬完即在新仓库内独立演进。DeepSearch 保持原样继续服务溯源场景,两系统仅共享"人"不共享代码。

迁移三步法(每个模块):

1. 拷贝源文件 + 对应测试(旧测试在 `services/orchestrator/tests/` 与 `tests/unit/` 下,按模块名检索);
2. 剥离耦合:删除对 ledger 仓储、task_event、settings 全局对象的引用,改为构造注入(adapters 与 snowball 的主要工作量在此);
3. 在新包内跑通测试并补充新契约测试(如 R1 核验、cite-key 白名单)。

预估迁移量:直接复用约 7 000–8 000 行(含测试),改造复用约 4 000 行思想重写;两者合计约占新后端首版代码的一半,是"分离重建"仍然划算的根本原因。

---

## 6. 实施路线图

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| **M0 脚手架 + 资产迁移**(1–2 周) | 新仓库 monorepo、CI、compose(pg/redis/minio/texd);迁移 scholar_gateway/ingest/llm_runtime/observability 四包及测试 | 五源检索、去重、雪球、PDF 解析在新仓库单测全绿;`docker compose up` 一键起环境 |
| **M1 项目与文献库 MVP**(2 周) | 项目 CRUD、SCOPE 生成、五源检索→排序→圈选入库、DOI/BibTeX 导入 + R1 核验、摘要级卡片;文献工作台页面 | 从题目到 30 篇入库文献 + 卡片 <5 分钟;核验失败文献 100% 拦截 |
| **M2 综述管线 MVP**(2–3 周) | 大纲生成/编辑、分节写作(cite_keys 白名单 + R2)、连贯性 pass、Markdown 预览、SSE 进度 | 端到端产出 8 000+ 字中文综述草稿;引用审计 0 幻觉引用 |
| **M3 LaTeX/PDF 渲染**(2 周) | PaperIR、模板库(article + IEEEtran + GB/T 7714 中文模板)、BibTeX 确定性生成(R3)、texd 编译、自动修复、导出中心 | 一键导出可编译 LaTeX 工程与 PDF;编译成功率 ≥95%(失败降级交付工程+日志) |
| **M4 研究型论文管线**(2–3 周) | 素材上传/解析、IMRaD 大纲、素材接地写作、数字一致性 lint、纯生成模式占位符策略 | 用户给定结果表格后,正文数字与表格 100% 一致;无素材时 0 虚构数值 |
| **M5 全文与质量增强**(2 周) | OA 全文卡片、雪球推荐 UI、语义引用软校验、覆盖建议器、质量评分报告、写作台润色浮条 | 全文卡片覆盖率报告;软校验徽章上线 |
| **M6 打磨与扩展**(持续) | docx 导出、多模型路由配置页、成本面板、章节版本历史、协作;远期:系统性综述模式(把 §3.3 摈弃的 screening/PRISMA 作为可选插件回加) | — |
| **M8 图片与图表生成** | A 基础闭环：视觉资产/PaperIR/texd 二进制；B 确定性图表/示意图；C 自动建议/ImageProvider；D 成本、进度、降级与视觉 QA | CSV/XLSX → 图表 → 批准 → 引用 → PDF/DOCX/LaTeX ZIP/Markdown Bundle 四格式含图；建议不付费、不改正文、不阻塞无图导出 |
| **M9 账号与多租户隔离** | 邮箱密码、opaque session、项目 owner、统一授权、用户级对象键、存量认领 | 匿名 401；跨租户项目/素材/视觉/导出/SSE 404；生产无 mock、公开 bucket 或前端可读令牌 |
| **M10 Cloudflare AI 生图适配** | provider registry/factory、Workers AI REST、FLUX.1-schnell、占位配置与无密钥降级 | 不使用真实 Token 的 mock 契约通过；新增厂商无需改 worker/视觉资产/导出链路 |
| **M11 投稿质量闭环** | draft/submission 双模式、论断—全文证据矩阵、快照报告、系统综述真实检索日志、活动视觉槽位与 PDF 后验 QA | 投稿失败结构化阻断且不产出“可提交”文件；最终 PDF 通过后标记 `submission_ready` |

关键依赖顺序:M0 → M1 → M2 → M3;M4 依赖 M3(渲染);M2 与 M4 的写作器共用同一 Section Writer。

---

## 7. 风险与对策

| 风险 | 等级 | 对策 |
|---|---|---|
| 幻觉引用 | 高 | 三硬规则(§4.4.3);前端 chip 选择器物理隔绝手写引用;引用审计页 |
| 长文一致性差(术语漂移、章节重复、风格断裂) | 高 | 分层上下文(全局大纲+滚动摘要+术语表)、连贯性 pass、章节间去重扫描(借鉴旧 summary/body duplicate guard) |
| LaTeX 编译失败循环 | 中 | 环境白名单约束生成、确定性修复优先、有界修复轮次、降级交付工程+Markdown |
| 编造实验数据 | 高(声誉/伦理) | 数字确定性注入 + NUMLINT + 占位符策略;prompt 与 lint 双层;导出前强制展示 TODO 清单 |
| 全文获取版权 | 中 | 仅 OA/官方 API 渠道(继承旧合规策略);document_file 记录来源;不提供绕 paywall 能力 |
| provider 限流/封禁 | 中 | 迁移旧 pacing+熔断+缓存;polite UA;EuropePMC/OpenAlex 优先(配额宽) |
| LLM 成本失控 | 中 | 卡片跨项目缓存、角色路由(便宜模型做抽取/排序)、llm_call_log 成本面板、项目级预算上限 |
| 范围回潮(重新长出溯源机器) | 中 | 本文档 §3.3 为负面清单;任何回加须以"可选插件"形式且不得阻断 draft-first 主路径 |
| 检索覆盖不足导致综述片面 | 中 | 雪球扩展 + 覆盖建议器(仅提示);检索统计面板让用户可见每源命中情况 |

---

## 8. 学术伦理与合规说明

本系统定位为**写作辅助工具**,产出物为"初稿/草稿"。落地时应内置:

1. 导出物可选附带 AI 辅助声明模板(各出版社政策不同,Elsevier/IEEE/Springer 均要求披露生成式 AI 使用;声明文案按 venue 模板给默认值);
2. 不伪造数据红线(§4.4.2)与引用真实三硬规则(§4.4.3)是伦理承诺的技术实现;
3. 提示用户:AI 生成内容须经本人核实与实质性修改后方可投稿,系统提供的引用审计/数字 lint/TODO 清单即为核实入口;
4. is_retracted 字段保留:入库时若命中撤稿标记,前端红色警示且默认不入写作白名单。

---

## 9. 附录:旧→新文件映射速查

```text
services/orchestrator/app/literature_review/adapters.py        -> packages/scholar_gateway/providers/{crossref,openalex,semantic_scholar,arxiv,europepmc}.py + runtime.py(pacer/breaker)
services/orchestrator/app/literature_review/normalization.py   -> packages/scholar_gateway/normalize.py
services/orchestrator/app/literature_review/dedupe.py          -> packages/scholar_gateway/dedupe.py(删仲裁流)
services/orchestrator/app/literature_review/snowball.py        -> packages/scholar_gateway/snowball.py
services/orchestrator/app/literature_review/oa_fulltext.py     -> packages/scholar_gateway/fulltext.py(解耦 ledger)
services/orchestrator/app/literature_review/http_cache.py      -> packages/scholar_gateway/cache.py
services/orchestrator/app/literature_review/citation_format.py -> packages/paper_ir/citation_style.py(+新 bibtex.py)
services/orchestrator/app/literature_review/section_chunks.py  -> packages/ingest/section_chunks.py
services/orchestrator/app/literature_review/json_utils.py      -> packages/llm_runtime/json_utils.py
services/orchestrator/app/literature_review/report_i18n.py     -> packages/paper_ir/i18n.py
services/orchestrator/app/llm/{client,providers,types}.py      -> packages/llm_runtime/
services/orchestrator/app/parsing/{document_extractors,chunking,quality}.py -> packages/ingest/
services/orchestrator/app/literature_review/protocol_generation.py -> services/worker/pipelines/scope.py(改造)
services/orchestrator/app/literature_review/topic_relevance.py -> services/worker/pipelines/ranking.py(改造)
services/orchestrator/app/literature_review/llm_synthesis.py   -> services/worker/pipelines/outline.py(改造:ThemeBundle 部分)
services/orchestrator/app/literature_review/llm_extraction.py  -> services/worker/pipelines/cards.py(改造)
services/orchestrator/app/literature_review/llm_review_writer.py -> services/worker/pipelines/writing.py(改造:claim_ids→cite_keys)
services/orchestrator/app/literature_review/evidence_matrix.py -> packages/paper_ir/cards.py(简化 ReviewEvidenceCard)
services/orchestrator/app/literature_review/gap_analysis.py    -> services/worker/pipelines/coverage_hints.py(降级为建议器)
services/orchestrator/app/acquisition/(HTTP 客户端与安全策略)  -> packages/scholar_gateway/http.py(收敛)
services/orchestrator/app/storage/                             -> services/api/storage/(对象存储 seam)
packages/db/models/literature_review.py(scholarly_work 系)     -> packages/db/models/library.py
packages/db/{base,session,repositories 模式} + alembic 约定     -> packages/db/(约定沿用)
packages/observability/                                        -> packages/observability/(原样)
apps/web/src/components/literatureReview/sourceCapabilities.ts -> apps/web/lib/sourceCapabilities.ts
不迁移:search/ planning/ claims/ research_quality/ reporting/ slides/ figures/ presentation_ir/
        artifacts/ indexing/ entity_resolution/ 及 literature_review 内 prisma/screening/llm_screening/
        finding_verification/synthesis_validation/study_quality/quality_*/taxonomy_effects/
        search_lane_plan/terminal_status/temporal_evidence_tables/quality_benchmark
```
