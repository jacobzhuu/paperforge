# PaperForge 实施路线图（M0–M6）

源自 docs/design.md §6。关键依赖：M0 → M1 → M2 → M3；M4 依赖 M3（渲染）；
M2 与 M4 的写作器共用同一 Section Writer。

| 里程碑 | 内容 | 验收标准 | 状态 |
|---|---|---|---|
| **M0 脚手架 + 资产迁移**（1–2 周） | 新仓库 monorepo、CI、compose(pg/redis/minio/texd)；迁移 scholar_gateway/ingest/llm_runtime/observability 四包及测试 | 五源检索、去重、雪球、PDF 解析新仓库单测全绿；`docker compose up` 一键起环境 | ✅ 完成 |
| **M1 项目与文献库 MVP**（2 周） | 项目 CRUD、SCOPE 生成、五源检索→排序→圈选入库、DOI/BibTeX 导入 + R1 核验、摘要级卡片；文献工作台 | 题目→30 篇入库+卡片 <5 分钟；核验失败 100% 拦截 | ✅ 完成 |
| **M2 综述管线 MVP**（2–3 周） | 大纲生成/编辑、分节写作（cite_keys 白名单 + R2）、连贯性 pass、Markdown 预览、SSE | 端到端 8000+ 字中文综述草稿；引用审计 0 幻觉引用 | ✅ 完成 |
| **M3 LaTeX/PDF 渲染**（2 周） | PaperIR、模板库(article+IEEEtran+GB/T 7714)、BibTeX 确定性生成(R3)、texd 编译、自动修复、导出中心 | 一键导出可编译工程与 PDF；编译成功率 ≥95% | ✅ 完成 |
| **M4 研究型论文管线**（2–3 周） | 素材上传/解析、IMRaD 大纲、素材接地写作、数字一致性 lint、纯生成占位符策略 | 正文数字与表格 100% 一致；无素材时 0 虚构数值 | ✅ 完成 |
| **M5 全文与质量增强**（2 周） | OA 全文卡片、雪球推荐 UI、语义引用软校验、覆盖建议器、质量评分、润色浮条 | 全文卡片覆盖率报告；软校验徽章上线 | ✅ 完成 |
| **M6 打磨与扩展**（持续） | docx 导出、多模型路由配置、成本面板、章节版本历史、协作；远期系统性综述插件 | — | ✅ 首轮完成 |

## M0 当前进度（本次脚手架）
已完成：
- monorepo 结构 + uv workspace + pnpm 前端 + CI + compose + .env.example + 设计文档落盘
- **observability** 迁移（logging 原样 + metrics 改 PaperForge 维度）
- **llm_runtime** 迁移：types/providers/client（Settings→LLMConfig 解耦）+ 角色路由 + json_utils（R2 cite-key 过滤）+ 测试
- **scholar_gateway** 迁移：normalize（纯函数）+ dedupe（去仲裁流）+ 候选模型解耦 + 测试；providers 迁移计划文档
- **ingest** 迁移：section_chunks（纯函数）+ 测试；重型抽取器迁移计划文档
- **paper_ir**：citation_style（简化 card 解耦）+ bibtex（R3 持久化 key 确定性生成）+ PaperForge 精简 i18n + PaperIR schema + 测试
- **latex_render**：PaperIR→LaTeX body 渲染器 + 转义 + 测试；模板体系 TODO 文档
- **db**：§4.3 全部 19 张表 SQLAlchemy 模型 + base/session/repository + 测试
- **services/api**：FastAPI app + config(pydantic-settings) + storage seam + health/projects 路由骨架
- **services/worker**：ARQ WorkerSettings 骨架 + 6 个 pipeline 阶段 stub（附改造来源映射）
- **services/texd**：Tectonic 编译微服务 + Dockerfile（构建期预热包缓存）
- **apps/web**：Next.js 15 App Router 骨架 + 移植 sourceCapabilities.ts

## M0 收尾（已完成）
- **scholar_gateway/providers**：五源适配器按 provider 拆分迁移完成
  （crossref / openalex / semantic_scholar / arxiv / europepmc + base + mapping + query_syntax）
- **scholar_gateway/runtime**：跨实例 pacer、连续 429 熔断器、日配额耗尽诊断
- **scholar_gateway/cache**：`HttpCacheBackend` 协议 + Null/InMemory/SQLAlchemy 三实现
  （保留 `scholarly_http_cache` 表结构，改存原始响应体以覆盖 Atom/JSON 两类源）
- **scholar_gateway/http**：合规抓取客户端（SSRF 防护 + 逐跳重定向校验 + 大小上限 + 礼貌 UA）
- **scholar_gateway/snowball**：前向 cited-by + 后向 references 一跳扩展 + 引文影响力排序（解耦 ORM）
- **scholar_gateway/fulltext**：OA URL 规划 + 合规抓取（解耦 ledger，产物交 worker 落 document_file）
- **ingest**：document_extractors / chunking 零改动迁入；quality 改造为文献取向的切块评分；
  新增 text_extract（HTML/纯文本）与 pdf（pypdf 优先 + 标准库操作符扫描兜底）
- **db + alembic**：`0001_initial` 迁移产出 §4.3 全部 19 张表；`upgrade head` 与 `check` 均通过；
  ScholarlyWork 增补 citation_count / influential_citation_count（雪球与 OA 预算依赖）

### M0 验收证据（2026-07-25）
- `uv run pytest`：112 passed（含五源检索/去重/雪球/PDF 解析/缓存/SSRF 契约测试）
- `uv run ruff check .`：全绿
- `alembic upgrade head` → 19 张业务表 + alembic_version；`alembic check` 无漂移
- `./scripts/dev status`：postgres/redis/minio/texd/api/worker/web 七服务全部运行
- 真实联网端到端：OpenAlex/Crossref/arXiv/EuropePMC 四源各返回真实结果
  （Semantic Scholar 匿名池 429，按 draft-first 降级并给出配置提示，未阻断）；
  32 条原始候选 → 31 条去重后候选；雪球扩展 6 条邻居；OA 全文 3/3 抓取成功；
  arXiv PDF 解析出 109 977 字符 / 103 块（其中 81 块判定可用于卡片抽取）

### M0 遗留（归入后续里程碑）
- 前端页面（大纲编辑器/写作台/素材中心/导出中心/设置）→ M2–M6
- pipelines 与 worker 编排、API 真实实现 → M1 起逐里程碑落地

## M1 完成情况（2026-07-25）

### 交付
- **db 仓储**：projects / works(候选→实体归一) / library(R1 白名单 + 卡片 + R3 key 分配) /
  search_runs / jobs(checkpoint + 事件 + LLM 记账)
- **llm_runtime.LLMRunner**：角色路由 + 成本记账回调 + JSON 净化 + R2 白名单审计的唯一调用入口
- **scholar_gateway.verify**：R1 反查（DOI→Crossref/OpenAlex、arXiv DataCite DOI→arXiv 官方 API、
  标题模糊匹配 ≥0.90 且作者/年份吻合）
- **paper_ir**：`parse_bibtex_entries` 把 .bib 解析为**待核验线索**；cite key 姓氏抽取修正
- **worker 管线**：scope（LLM + 确定性回退）/ search（五源并行→去重→确定性排序→LLM 重排→入库）/
  importing（R1 逐条核验）/ cards（摘要级卡片 + 确定性回退）；`_run_stage` 保证 gate-free
- **API**：项目 CRUD、SCOPE 生成/编辑、检索与导入任务、文献库圈选、写作白名单、任务查询、
  成本、SSE 进度流
- **前端**：库工作台接真实 API（检索/导入/卡片触发 + SSE 进度条 + 白名单计数），保留 mock 降级

### 验收证据（真实联网，2026-07-25）
- 题目 → 入库：**25.9 秒**（≪ 5 分钟目标）；四源成功（S2 匿名 429 走降级），
  200 条原始候选 → 158 条去重 → 自动选入 29 条，**29/29 有卡片、有 bibtex_key、已核验**
- R1 拦截：DOI `10.9999/this-doi-was-invented-by-an-llm` 与 .bib 条目
  「A Completely Fabricated Paper About Nothing」**均被拒绝**并记 `verification_failed`；
  真实 arXiv DOI `10.48550/arXiv.2005.11401` 经 arXiv 官方 API 核验通过（`matched_by=arxiv_id`）
- 白名单：导入后 30 个 cite key，全部满足 selected + verified + keyed + 未撤稿
- `uv run pytest`：180 passed；`uv run ruff check .`：全绿；`pnpm lint` + `pnpm build`：通过
- `./scripts/dev up`：七服务健康（修复了本机 HTTP 代理导致的 localhost 健康检查 502）

### 已知限制
- 无 LLM 配置时 scope/卡片/重排走确定性回退（卡片=摘要切句），配置 provider 后自动升级
- 浏览器 UI 未做自动化截图验收：本环境 Chrome 扩展未连接；前端已通过 tsc + 生产构建验证

## M2 完成情况（2026-07-25）

### 交付
- **outline 管线**：卡片主题聚类 → 章节树（LLM + 确定性回退 + IMRaD 模板）；
  孤儿文献回收保证入库文献都有归属；章节 cite_keys 在大纲阶段就收敛到白名单
- **Section Writer**：段落携带 cite_keys；分层上下文（全局大纲 + 本章卡片 +
  相邻章节滚动摘要 + 术语表）；prompt 明令禁止编造数字
- **R2 三道防线**：prompt 只给白名单 → 首轮 `report` 越权触发**带错误信息重写一次**
  → 二次违规 `strip` + `citation_warnings` 持久化；PaperIR typed-IR 再检查一次
- **连贯性 pass**：改写引入新数字即整体放弃改写（数字红线优先于文采）
- **document 装配**：PaperIR → paper_document / paper_section / citation_usage
- **paper_ir.markdown**：Markdown 预览（引用编号 + GB/T 7714 参考文献 + R2 告警可见）
- **API**：大纲生成/编辑、分节生成、章节编辑（服务端白名单兜底）、引用审计、Markdown 预览、
  一键全管线 `POST /projects/{id}/generate`
- **前端**：大纲编辑器（章节增删改、论证要点、文献分配）+ 写作工作台
  （Tiptap 正文编辑 + 白名单引用 chip + Markdown 预览 + 引用审计面板）

### 验收证据（2026-07-25）
- 端到端产出：7 章 / **9 968 字**中文综述（Markdown 预览 10 534 字），
  基于 M1 真实入库的 30 篇文献
- **引用审计 0 幻觉引用**：替身模型在每章故意注入白名单外的 cite key，
  `rewrite_count=7` 证明首轮全部被识别并触发重写，最终 `hallucinated_cite_keys=[]`；
  48 条 citation_usage 落库，参考文献按 GB/T 7714 从库内元数据确定性生成（R3）
- `uv run pytest`：207 passed（新增 42 条 M2 测试，含 R2 三道防线与数字红线反例）
- `uv run ruff check .` / `pnpm lint` / `pnpm build`：全绿
- `/outline`、`/write`、`/library` 三页在真实服务上返回 200

### 已知限制
- 本机未配置 LLM provider：真实文风质量取决于用户配置的模型；
  验收用受控替身证明的是**管线与 R2 契约**，不是模型文采
- 写作台的「AI 重写/扩写/润色浮条」属 M5 润色能力，尚未接入

## M3 完成情况（2026-07-25）

### 交付
- **模板体系**：`latex_render/templates/{article,ieeetran,gbt7714}.tex.j2`；
  导言区是仓库资产，渲染只往固定槽位填内容，**LLM 不可修改导言区**
- **工程装配** `build_latex_project`：main.tex + sections/*.tex（按章节拆文件，
  报错行号可对应到章节）+ refs.bib（R3 确定性生成，缺持久化 key 即拒绝生成书目）
- **texd 客户端 + 有界修复**：确定性修复优先（缺失宏包降级、未定义环境/命令剥离），
  仍失败才允许 LLM 对**章节文件**做最小修补（导言区与 refs.bib 不容触碰），修复轮次 ≤2
- **导出管线**：pdf / latex_zip / markdown / bibtex 四种产物入对象存储与 `export_artifact`；
  编译失败也交付工程 zip + 编译日志（draft-first）
- **API**：`POST/GET /projects/{id}/exports`、`GET .../exports/{aid}/download`
- **前端**：导出中心（触发、SSE 进度、编译状态、产物下载）
- **texd 镜像**：把实际交付的导言区、字号×字形组合、Latin Modern 全家族（144 个字体文件）
  在**构建期**烤进 Tectonic 缓存——运行时无外网且只读，漏一个文件就编不出来

### 验收证据（2026-07-25）
- **编译成功率 3/3（100%）**：article / IEEEtran / GB-T 7714 中文模板均编出真实 PDF
  （89 KB / 140 KB / 109 KB），修复轮次 0
- HTTP 全链路：`POST /exports` → 任务 succeeded → `GET .../download` 返回
  `application/pdf` 140 401 字节，附件名 `paperforge-v2.pdf`
- PDF 实际内容核对：中文标题/摘要/章节/编号引用 `[1] [2]` 均正确排版
- `uv run pytest`：229 passed（新增 22 条 M3 测试，含 R3 拒绝、修复降级、修复轮次上界反例）
- `uv run ruff check .` / `pnpm lint` / `pnpm build`：全绿

### 已知限制
- 图片资产（`\includegraphics`）随 M4 素材中心接入，当前只登记引用路径
- docx 导出（pandoc）属 M6

## M4 完成情况（2026-07-25）

### 交付
- **`ingest.assets`**：CSV/TSV/XLSX → 表头 + 行 + `numeric_cells`（保留原始精度，不做四舍五入）；
  图/笔记/代码/.bib 各自的确定性解析；`.bib` 标记 `verification_required`（仍走 R1 反查）
- **`ingest.numlint`**：正文数值 vs 素材解析值比对；年份与「图 1/表 2」类序数不误报；
  产出 `sourced / unsourced` 明细，**不阻断**但必须显式呈现
- **素材接地写作**：Section Writer 的 prompt 中，可用数值以「唯一允许写出的数字」形式给出；
  无素材时明确要求写「结果待补充」而不是编数字
- **渲染层红线**：`TableBlock` 由代码从 `parsed_json` 渲染 booktabs（LLM 无法改动单元格），
  `FigureBlock` 走 `\includegraphics`；素材缺失渲染 `\todo{}` 占位而不是编一张表
- **IMRaD 模板**：研究型论文大纲实例化（Related Work 接文献库，Method/Experiments 接素材）
- **API**：`POST/GET/DELETE /projects/{id}/assets`、`/assets/{id}/download`、`GET /numlint`
- **前端**：素材中心（上传、表格解析预览、NUMLINT 报告）

### 验收证据（真实 HTTP + 端到端，2026-07-25）
| 场景 | 结果 |
|---|---|
| A 有素材、只写素材里的数值 | `consistent=true`，24/24 处数字**全部**溯源到 results.csv |
| B 模型编造 `0.999`（素材中不存在） | `consistent=false`，24 处全部标记 `unsourced` 并写入任务告警 |
| C 无素材（纯生成模式） | 正文写「实验结果待补充」，全文 **0 个实验数值**，`consistent=true` |

- 素材上传经 HTTP 验证：`results.csv` → 3 行 × 4 列 / 9 个可引用数值 / ref `ua_0d7f859e`
- `uv run pytest`：258 passed（新增 29 条 M4 测试，含编造数值、无素材、精度保持等反例）
- `uv run ruff check .` / `pnpm lint` / `pnpm build`：全绿

### 已知限制
- 图片二进制尚未随 LaTeX 工程投递给 texd（工程里已生成正确的 `\includegraphics` 路径），
  完整图文编译随 M5 一并接入

## M5 完成情况（2026-07-25）

### 交付
- **OA 全文管线**：`plan_oa_fulltext` → 合规抓取 → pypdf 解析 → 质量筛块 → `document_file` 落库；
  卡片升级为全文级（`fulltext_used=true`，`source_hash` 含全文以便缓存失效）
- **雪球扩展**：核心种子按引文影响力挑选 → 前向/后向一跳 → 去重 → 入库为 **candidate**
  （R1：邻居来自真实响应故 verified，但仍需用户圈选才进白名单）
- **语义引用软校验**：verifier 角色比对引用上下文与摘要，低分出**黄色徽章**，
  绝不删除引用、绝不阻断渲染（设计 §4.4.3：执行点从「渲染前拒绝」移到「编辑器内提示」）
- **覆盖建议器**：无引用章节 / 引用密度低 / 入库未被引用 / 文献偏旧 / 子主题未覆盖
- **质量评分报告**：字数、引用密度、文献利用率、近 5 年占比、全文覆盖率——只呈现不设门槛
- **润色浮条**：润色/扩写/缩写/学术语气；服务端二次把关，**改动数字即放弃本次润色**
- **API**：`/snowball`、`/ingest`、`/quality`、`/quality/generate`、`/sections/{key}/refine`
- **前端**：库工作台的雪球/OA 全文按钮、写作台质量报告页签、软校验徽章、段落级润色浮条

### 验收证据（真实联网，2026-07-25）
- **OA 全文**：`planned=3 acquired=3 parsed=3`，生成 3 张全文级卡片，覆盖率报告上线
- **雪球扩展**：4 个核心种子 → 66 条邻居 → 63 条入库为 candidate（库从 152 → 216 条）
- **质量报告**：7 章 / 9 968 字 / 24 条引用 / 密度 2.4 条每千字 /
  文献利用率 0.80 / 近 5 年占比 0.97 / 全文覆盖率 0.10
- `uv run pytest`：272 passed（新增 13 条 M5 测试，含软校验越界下标、覆盖提示反例）
- `uv run ruff check .` / `pnpm lint` / `pnpm build`：全绿

### 本轮修复的缺陷
- `_run_stage` 只认带 `to_payload` 的对象，元组/字典返回值会把 checkpoint 写成 `true`
  （ingest 与 snowball 的统计全部丢失）→ 支持三种形态
- 覆盖建议对「确定性回退的主题切词」逐词刷提示 → 只对与主题不同的实义子主题提示

## M6 首轮完成情况（2026-07-25）

### 交付
- **docx 导出**：Markdown → pandoc → docx（国内投稿场景）；pandoc 缺失时只降级这一种格式，
  其余产物照常交付（draft-first）
- **模型角色路由设置页**：6 个角色（planner/extractor/reranker/writer/polisher/verifier）
  的当前模型与用途；**密钥永不回传**，接口只报告「是否已配置」
- **成本面板**：`llm_call_log` 按角色×模型聚合（调用数、输入/输出 tokens、成本估算、平均延迟）
- **版本历史**：大纲与文稿每次生成都建新版本，设置页可回溯（当前版本高亮）
- **API**：`GET /settings`、`GET /projects/{id}/versions`、`GET /projects/{id}/cost/detail`

### 验收证据（2026-07-25）
- 导出五格式全绿：`['bibtex','docx','latex_zip','markdown','pdf']`，`compile_ok=true`；
  docx 下载返回 14 186 字节、`Microsoft Word 2007+`、正确的 MIME
- 设置页读到 6 个角色路由；`llm_api_key_configured=false` 且响应体不含任何密钥
- 版本历史：文稿 v1/v2（v2 为当前、7 节）、大纲 v1/v2

### 明确未做（设计 §6 标注为「持续」/远期）
- **协作**（多人实时编辑、评论、权限）：需要用户体系与冲突合并，本轮只打了 `owner_id` 地基
- **系统性综述插件**（把 §3.3 摈弃的 screening/PRISMA 作为可选插件回加）：远期项
- 设置页目前是**只读**视图：改配置走 `.env` + 重启，避免把密钥写进数据库
