# PaperForge 实施路线图（M0–M9）

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
| **M7 前端设计升级**（1–2 周） | 见 `docs/ui-design.md` §5：一期气质（serif + 暖色 + 去卡片化）、二期核心心智（`write.section` 增量浮现 + 导航按写作模式分叉）、三期入口（`PATCH /projects/{id}` + 首页 Prompt Canvas）、四期差异化（文献库 Research Context） | 写作期间章节逐节浮现；概览页 Card 数降至 0；首页从意图输入起步；文献可反查引用章节 | ✅ 完成 |
| **M8 图片与图表生成** | 视觉资产、确定性 visuald、ImageProvider、审核插入与全格式含图导出 | 表格→图表→批准→正文→PDF/DOCX/ZIP/Markdown Bundle；自动建议不付费 | ✅ 完成 |
| **M9 账号与多租户隔离** | opaque session、项目 owner、统一授权、用户级对象键、存量认领 | 匿名 401、跨租户 404、生产无 mock/公开 bucket/前端令牌 | ✅ 代码完成，待维护窗口迁移 |
| **M10 Cloudflare AI 生图适配** | 可注册 ImageProvider、Workers AI REST、FLUX.1-schnell、占位配置 | mock 契约通过；缺 Token/Account ID 不发请求且不影响其他功能 | ✅ 完成 |

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

### 后续修复（2026-07-26）
- **「编译成功但全文引用是 `[?]`」**：texd 容器 `read_only`，Tectonic 缓存未命中时
  连临时文件都建不了，BibTeX 打不开 `.bst`；Tectonic 把它降级为一行 warning 后照常
  产出 PDF——最坏的一种失败：看起来交付了。三处收口：
  compose 给缓存挂命名卷（镜像预热内容做种）；构建期按 `BIBSTYLE_BY_CITATION_STYLE`
  逐个跑 BibTeX 烤进全部 `.bst`；`compile_with_repair` 识别书目失败后换成确定性生成的
  内联 `thebibliography` 重编一次，并在导出中心标注降级
- **`cn_thesis` 模板别名缺失**：界面「中文学位论文/学报」发的取值不在 `TEMPLATES` 里，
  静默退回 article + unsrt——GB/T 7714 的上标顺序编号一次也没生效过。补齐别名，
  并对确实没实现的模板（acmart/elsarticle/llncs）显式回报降级而不是装作没降级
- **产物下载/预览**：`GET .../download` 增加 `?disposition=inline`。此前预览用的是
  attachment 直链，于是「打开导出中心」等于凭空下载一个文件，而预览框永远空白；
  文件名从 `paperforge-v1.pdf` 改为 `标题-v2-20260726.pdf`（RFC 5987，中文走 `filename*`）

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

### 已解决的历史限制
- M8 已将上传图和生成图以 base64 二进制文件投递给 texd，并补齐 PDF、
  LaTeX ZIP、DOCX 和 Markdown Bundle 的图文闭环；路径、扩展名、magic bytes 和容量上限均由 texd 二次校验。

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

## 全流程实跑验收（真实 DeepSeek，2026-07-25）

`POST /projects/{id}/generate` 一键跑完 scope→search→curate→cards→outline→write→render，
用时约 24 分钟（含 31 篇检索入库 + 29 张卡片抽取 + 7 章写作 + Tectonic 编译）。

| 阶段 | 结果 |
|---|---|
| scope | `llm:deepseek-v4-pro`，产出双语关键词矩阵与子主题 |
| search | 163 条原始候选 → 142 条去重 → 自动选入 31 条 |
| curate | 30 个 bibtex_key 持久化（R3） |
| cards | LLM 抽取 12 张 + 缓存复用 17 张 + 回退 1 张 |
| outline | `llm:deepseek-v4-pro`，7 章，孤儿文献 0 |
| write | 7 章 / **5 516 字** / 26 个引用 / R2 重写 0 次 / 引用告警 0 |
| render | `compile_ok=true`，模板 gbt7714，五格式齐全 |

**引用与数字双红线实测**
- 引用审计：白名单 30，已用 26，**幻觉引用 0**，被移除引用 0，29 条使用记录
- 正文 10 个实义数值（0.84 / 0.8443 / 12% / 15% / 16 / 3.3 / 40% / 70 / 83.6% / 90.0%）
  **全部可在被引文献的卡片或摘要中找到出处，0 个编造**
- 质量报告：引用密度 5.3 条/千字、文献利用率 87%、近 5 年占比 100%

**产物**：PDF 329 741 字节（GB/T 7714 中文排版、上标编号引用）、docx 22 184 字节、
LaTeX 工程 zip（main.tex + 6 个章节文件 + refs.bib）、Markdown、BibTeX。

### 本轮实跑暴露并修复的缺陷
1. **全管线提前收尾**：`run_full_pipeline` 复用 `run_library_pipeline`，后者在文献阶段
   就把任务标成 succeeded；前端看到「完成」时 outline/write/render 其实还在后台跑。
   → 加 `finalize` 开关，全管线自己收尾。
2. **推理型模型的预算陷阱**：`deepseek-v4-pro` 把思维链计入 `max_tokens`，预算耗尽时
   返回 HTTP 200 但 `content` 为空，被误判为「响应结构非法」而直接降级——
   大纲因此静默退回确定性分组（章节质量断崖式下降）。
   → provider 区分出 `output_truncated`，runner 加倍预算重试一次（上限 8192），
   大纲预算提到 6000；两条路径都有反例测试。

---

## M7 一期 / 二期完成情况（2026-07-26）

设计依据 `docs/ui-design.md`。本节记录一期与二期；三期与四期交付见后续小节。

### 一期：气质（纯前端）
- **暖色系配色**（`app/globals.css`）：冷调科技蓝 → 暖白 + 炭灰 + 铜色点缀，明暗两套。
  全部对比度按 WCAG 相对亮度公式重算并写进注释，未照搬任何外部色板。
- **接上 serif**：`--font-serif` 此前定义了但全仓库 `.tsx` 零引用。现按「≥18px 才用衬线」
  接到品牌字、页面标题、论文题目、工作台标题、项目卡题目；新增 `.pf-paper` 用于全文预览。
  字体栈把 Georgia 提到中文衬线之前（回退是逐字符的，否则拉丁字符会落到 Songti SC）。
- **概览页去卡片化**：`project-overview.tsx` 五张 Card → 0 张，改用「小写标题 + 留白 +
  分隔线」分层；只有「下一步」保留边框（全页唯一的行动召唤）。近期任务的彩色 Badge
  改为固定宽状态词 + 小圆点。写作台空态同步去卡片化。

### 二期：核心心智（纯前端）
- **章节逐节浮现**（`writing-workbench.tsx`）：订阅 `write.section` 事件增量重取章节列表。
  后端本来就写完一节存一节并逐节 emit（`pipelines/document.py::write_document`），
  此前前端只拿它拼了一句状态文案，于是 18 分钟的写作全程只有一根进度条。
  正在编辑的脏章节保留本地行，避免弹出「已恢复未保存的草稿」打断打字。
  写作中的空态从「还没有正文，先去生成大纲」改为「正在写第一节」。
- **导航按写作模式分叉**（`project-pipeline-nav.tsx`）：assisted 保留可点步骤但降到最低
  视觉权重（去连接线、完成态由绿底对勾徽章改为小实心点）；auto 收成一行
  「概览 · 第 1 / 6 步 ⌄」，点开才展开。**没有**照搬「把管线整体弱化成一行」的建议——
  协作模式的定义就是用户要介入，收起导航等于拿走方向盘（理由见 ui-design.md §4.1）。

### 验收证据（2026-07-26）
- `tsc --noEmit` 通过；`next build` 全量通过（11/11 静态页）。
- 浏览器实测浅色 / 暗色两套主题：项目列表、项目概览（assisted）、项目概览（auto，
  含展开/收起）、写作工作台空态。
- **增量浮现实测**：以一个逐节推送 `write.section` 的 mock SSE 服务驱动前端，
  观察到章节树从 0 → 3 →（含「正在写作，章节会陆续出现」脉冲提示）→ 5 节、
  字数 0 → 2,511 → 4,370 递增，任务结束后提示消失、进度条撤走。

### 本轮引入并修复的缺陷
1. **暗色 warning 文字不可读**：换色时把暗色的 `--warning-foreground` 也设成了深色，
   照「实心填充上的前景色」理解。但全仓库 11 处 `text-warning-foreground`
   **没有一处**压在实心 `bg-warning` 上，全是 `bg-warning/10~/20` 的浅色调
   （降级提示条、warning 徽章、引用 chip），实际语义是「warning 色调面上的可读文字」，
   必须跟随主题。→ 暗色改回浅色（压 /10 上 11.9:1），并在变量旁注明它与
   `--success-foreground` / `--destructive-foreground` 方向相反的原因。

---

## M7 三期完成情况（2026-07-26）

设计依据 `docs/ui-design.md` §3.2。四期（文献库 Research Context）见下一节。

### 后端：`PATCH /projects/{id}`
- `db.update_project`（`packages/db/db/repositories/projects.py`）+ `UpdateProjectRequest`
  + 路由。语义：**字段缺席 = 不改，显式 `null` = 清空**，靠 pydantic 的
  `model_fields_set` 区分，不能用 `None` 当哨兵（`topic=None` 是合法的清空）。
- topic / contribution_points 落在 `scope_json` 里，走**合并**而非整体替换——
  否则改一次主题就会把 SCOPE 生成的关键词矩阵一起抹掉。
- **不允许改 `paper_type`**：论文类型决定管线形状（REVIEW_FLOW / ORIGINAL_FLOW）、
  大纲结构与是否做数字一致性 lint。有了文献/大纲/正文之后换类型只会得到自相矛盾的
  稿子，那是「新建项目」不是「改字段」。
- 新增 6 个契约测试（局部更新、scope 保全、null 语义、枚举校验、paper_type 不可改、404）。

### 前端
- **首页 Prompt Canvas**（`components/home/prompt-canvas.tsx`）：`app/page.tsx` 从
  `redirect('/projects')` 改为意图输入。类型选择降级为输入框内的下拉；
  模板/语言/引用样式/写作模式收进「更多设置」折叠区。⌘/Ctrl+Enter 提交。
- **文件拖拽分流**：拖入文件自动切到研究型论文（ORIGINAL_FLOW 的第一步是素材，
  文本框对这条管线是结构性错配）；用户手动选过类型后不再自动改写。
  文件在浏览器内暂存，建项目后再补传（uploadAsset 需要 projectId）；
  补传失败不回滚项目，而是把用户送到素材中心重传。
- **就地改名**（`components/project/project-title.tsx`）：项目头的题目可直接编辑，
  Enter 保存 / Esc 取消。`updateProject` **刻意不给降级桩**——改名要么真落库，
  要么明确报错，不能让用户看到改好了、刷新后没存。
- 侧栏新增「新论文」入口并置顶；`/projects` 保留为完整列表页，不再是首页。

### 验收证据（2026-07-26）
- `pytest services/ packages/` 320 passed；`tsc --noEmit` 通过；`next build` 11/11。
- 对**真实 API + 真实 Postgres**（非 mock）实测 PATCH 五种行为：只改 title 时
  topic/language/citation_style/writing_mode 全部保持；空白 title → 422；
  `paper_type` 被忽略；改 topic 后 `keyword_groups` 仍在；非法枚举 → 422。
- 浏览器实测：首页输入意图 → 创建项目 → 落到概览（题目取首句、完整意图存为 topic）；
  就地改名（✓ 按钮与 Enter 两条路径都验证落库）；拖文件后类型自动切换、
  显式选择后再拖不被改写。

### 本轮引入并修复的缺陷
1. **PATCH 必炸 MissingGreenlet**：`TimestampMixin.updated_at` 带
   `onupdate=func.now()`（服务端求值），UPDATE 后 SQLAlchemy 会把该属性标记 expired
   等待回读；构造响应时 `project.updated_at` 是**同步**属性访问触发的隐式 IO，
   在 asyncpg 下直接抛异常。INSERT 路径没这问题（服务端默认值走 RETURNING）。
   → `update_project` 在 flush 后补一次 `session.refresh`。
2. **改名入口在触屏上不可达**：按钮用了 `opacity-0 group-hover:opacity-100`，
   触屏没有 hover 态；且 opacity:0 的控件会被无障碍树过滤（实测读页面时整个不出现）。
   → 改为常驻 `opacity-40`，hover/聚焦补满。
3. **类型 pin 读到过期闭包**：`typePinned` 原本是 state，`addFiles` 读到的是本次渲染
   闭包里的旧值——「选完综述立刻拖文件」仍会被改回研究型。该值从不参与渲染。
   → 改为 ref。

---

## M7 四期完成情况（2026-07-26）

设计依据 `docs/ui-design.md` §3.6，纯前端完成，无新增后端端点。

### 交付
- **章节反查**（`lib/citation-usage.ts`）：从 `listSections()` 返回的 `cite_keys` 建立
  `cite_key → 章节标题[]`，按正文顺序排列并在同章内去重；使用持久化 BibTeX key 与文献对应。
- **Research Context 主列表**（`components/library/entry-list.tsx`）：继续使用固定行高虚拟化，
  但从 ID / DOI / 分数 / 状态列改为「题名 + 作者/会议/年份 + 被引用章节」。未进入正文时，
  依次回退到排序理由和入库来源；相关性仍可排序，详细分数与核验 metadata 留在 Inspector。
- **可操作的研究语境**：新增「正文引用」筛选；搜索同时覆盖标题、作者、会议与章节名。
  工作台标题显示总文献数和已进入正文的数量，引用位置也进入文献详情抽屉。
- **独立降级**：章节与检索统计属于增强信息，任一接口失败都不会拖垮文献主列表；
  未生成正文、旧数据缺 key 时自然回退到排序理由/来源。
- **延续既有分诊能力**：虚拟化、相关性排序、状态筛选、Shift 连选、批量入库/排除/移除、
  R1/R2/R3 Inspector 均保留；主视图不再常驻分数条和状态 Badge。

### 验收证据（2026-07-26）
- `pnpm lint`（`tsc --noEmit`）通过；`pnpm build` 生产构建通过（11/11 静态页）。
- 真实 API + Postgres 数据验证：202 条文献、6 个章节形成 26 个 cite-key，26 条文献成功
  反查到章节；抽样文献能按正文顺序返回一到两个章节，未出现孤立 key。
- `git diff --check` 通过。浏览器视觉复验因本地地址访问策略被阻止，未以截图替代上述证据。

---

## M8 图片与图表生成（2026-07-26）

详细契约与安全边界见 `docs/visual-generation-optimization-plan.md`。

### M8-A：基础闭环

- 新增 `visual_asset`、`visual_source_asset`、`visual_generation_attempt` 三张表；生成图按
  内容哈希存储，重生成创建新版本，原数据被引用时禁止删除。
- PaperIR 图片块补齐 `alt_text` / `width`，新增结构化 `XRefRun`、素材引用采集和
  label 唯一性校验；章节保存时服务端重算 `asset_refs_json`。
- `LatexProject` 分离文本和二进制文件；texd 接收 base64 PNG/JPEG/PDF，并校验相对路径、
  扩展名/magic bytes、单文件 16 MiB、最多 32 个图/总计 64 MiB。编译修复轮不可改动图片。
- 上传图与生成图已接入 PDF、LaTeX ZIP、DOCX 和新增的 `markdown_bundle`；导出附带
  `visual-provenance.json`。

### M8-B：确定性图表与示意图

- `ChartSpec` 支持柱状图、折线图、散点图、箱线图和热力图，只能引用已解析 `ua_*` 表格；
  允许显式过滤、排序和 mean/median/sum/count，不接受内联数据、URL、代码、补值或静默采样。
- `DiagramSpec` 只接受节点/边/分组与 TB/LR 方向，不接受 DOT/Mermaid 源码；上限 30 节点/
  60 边。
- 新增无外网 `visuald`：Matplotlib 出 SVG/PDF/300 DPI PNG，Graphviz 负责结构图布局，
  Pillow 负责图像解码、大小校验和元数据清理；固定中文字体和色盲友好配色。
- 素材中心新增“图表与插图”页签及非代码向导；图表/示意图可生成预览、批准插入和重生成。

### M8-C：自动建议与 AI 位图

- 全文 write 后运行 `visual_plan`，最多创建 6 条建议；它不调用付费图像 API、不改动 PaperIR，
  失败也不会阻断 render/export。
- `AIImageSpec` 仅允许概念性插图，禁止实验数据、坐标轴、结果曲线和精密装置；只有用户点击
  “生成预览”才会向外部 provider 发送已确认 prompt。
- 新增独立 `ImageProvider` 契约、注册表与 factory；默认适配器为 Cloudflare Workers AI
  `@cf/black-forest-labs/flux-1-schnell`，并保留 OpenAI 适配器。配置/密钥与文本模型完全隔离；
  鉴权/审核/参数错误不重试，429/5xx/网络错误有界退避。
- AI 图在编辑器和素材中心显示来源标识；设置页只显示 provider/model/密钥是否已配置，
  成本页记录图像调用、失败、尺寸、usage 与可选成本估算，响应永不返回密钥。

### M8-D：上线与降级

- `VISUALS_ENABLED` / `AI_IMAGES_ENABLED` 分级开关；AI provider 未配置时只禁用 AI 生成，
  确定性图表、正文和无图导出仍可用。
- `scripts/dev` 与 compose 加入 visuald 启动/健康检查；visuald 使用只读根文件系统、tmpfs、
  CPU/内存/进程限额与无出站网络。
- 写作台专用 Figure NodeView 显示真实预览、caption、alt text、AI 标识和替换入口；
  Figure XRef 与引用 chip 同样结构化往返，未知 IR 块仍无损透传。

### 验收状态

- 已通过 PaperIR/visuals/latex_render/texd/visuald 的规格、渲染、溯源与二进制反例测试；
  351 个 Python 测试、前端类型检查/生产构建和 Alembic 漂移检查均通过。
- 浏览器真实运行 CSV → 柱状图 → worker → visuald → PNG/SVG/PDF → 页面预览；visuald 镜像
  构建、容器健康检查与 Graphviz 中文结构图三格式渲染也已通过。
- 真实 Cloudflare 图像冒烟需要 `IMAGE_ACCOUNT_ID` 与 `IMAGE_API_KEY`；未配置的环境只运行 mock
  provider 契约测试，不会为验收自动产生外部调用。

## M10 Cloudflare Workers AI 生图适配

- worker 已移除 OpenAI 硬编码分支，只消费统一 `ImageProvider`；Cloudflare/OpenAI 由注册表创建，
  新增 GPT Image、Gemini、ComfyUI 等后端时只需新增适配器与注册动作。
- 默认占位配置为 Cloudflare Workers AI + FLUX.1-schnell；Account ID 与 Token 留空，设置 API 仅返回
  `image_provider_configured=false`，前端只禁用 AI 生图，图表、示意图、写作与导出不受影响。
- mock 测试覆盖官方 REST 路径、Bearer 鉴权、prompt/steps、JSON/base64 与二进制响应、JPEG/PNG
  校验、401/403/400/422 不重试、429/5xx 有界重试及无配置零网络请求。

### 代码审阅修复（2026-07-26）

三个缺陷都落在同一处覆盖空洞：示意图渲染、box/heatmap 图形、多 series 横轴此前
**一个测试都没有**，而 `dot` 既不在开发机也不在 CI 里，`/render/diagram` 因此从未被执行过。

1. **带分组的示意图 100% 渲染失败**：`f"subgraph cluster_{_dot_id(id)}"` 生成
   `subgraph cluster_"enc"`——DOT 文法里这是「一个 ID 后面又跟了一个 ID」，
   graphviz 直接 syntax error。只要 `DiagramSpec.groups` 非空就必炸，而分组是
   方案 §2.2 承诺的能力。→ 整个名字包进引号：`subgraph "cluster_enc"`。
2. **箱线图返回裸 500**：`ax.boxplot(labels=...)` 在 matplotlib 3.9 弃用、3.11 移除，
   而 visuald 把 matplotlib 钉在 3.11.1；抛出的 `TypeError` 不在 `render_chart`
   捕获的 `ValueError` 里，于是连 422 都不是。→ 改用 `tick_labels=`。
3. **多 series 横轴刻度重复**：刻度取自 `records` 的长度，而每个 series 用组内局部
   下标 `range(len(xs))` 定位。2 个 series × 3 个 x 值画出 6 个刻度
   （task1,task2,task3,task1,task2,task3），后三个下面没有任何数据点。
   → 横轴类目改为跨 series 去重保序，各 series 按 x 映射到同一套位置；
   `numeric_x` 改为按全部记录判定（按组判会让一个 series 落数值轴、另一个落类目轴）；
   刻度移到循环外设置一次。
4. 顺带：`ax.legend()` 对 box/heatmap 无意义（它们不产出带 label 的 artist），
   只会画空图例并抛 UserWarning，已加类型判断跳过。

另两处校验口径不一致，同轮修复：

5. **`in` 过滤器漏行**：`eq` 有字符串兜底比较（CSV 把数值列解析成 `"1"` 时仍能匹配
   过滤值 `1`），而 `in` 是裸 `actual in expected` 的精确相等。同一份数据同一个值，
   `eq 1` 命中、`in [1, 2]` 却静默漏掉——图上少画点且不报错。
   → `in` 改为逐项复用 `eq` 的语义。
6. **轴标签绕过内容校验**：`x/y/series/error_*` 一直过 `_REMOTE_OR_CODE`，
   而同样会渲染进 SVG/PDF 的 `x_label/y_label/unit` 是唯一放行的自由文本：
   `https://…` 写进节点标签被拒、写进 y_label 却通过。并非可利用漏洞
   （matplotlib 会 XML 转义，前端用 `<img>` 而非内联 SVG），但校验口径不该按字段松紧。
   → 补 `reject_code_like_labels` 校验器，正常中英文标签与 `%` 单位仍放行。

**验收**：新增 6 个回归测试（五种图形全渲染、多 series 刻度断言、不依赖 graphviz 的
DOT 文法断言、真实调用 graphviz 的三格式渲染、`in` 与 `eq` 匹配口径一致、轴标签内容校验）。
在 visuald 镜像内挂载旧代码运行，4 个测试如实失败；换成修复后的代码 11 passed / 0 skipped。
本地 382 passed / 1 skipped、ruff 与 format 全通过。
CI 增装 graphviz——否则 `skipif(dot 不存在)` 会永远静默跳过，正是缺陷 1 躲过 CI 的原因。

---

## M9 账号体系与多租户隔离（2026-07-27）

### 账号与会话

- 新增 `app_user`、`user_session`、`auth_action_token`；邮箱规范化唯一，密码使用
  Argon2id（19 MiB / t=2 / p=1），浏览器只持有 256-bit opaque HttpOnly cookie，数据库
  只保存 SHA-256。会话空闲 7 天、绝对 30 天，密码修改/重置撤销全部旧会话。
- 完成注册、邮箱验证、登录、退出、当前账户、忘记/重置/修改密码 API。验证令牌 24 小时、
  重置令牌 30 分钟且只能消费一次；注册/找回返回统一文案。
- 登录、注册、找回按 IP 与邮箱分别使用 Redis 限流，Redis 失效时返回 503；危险请求必须
  携带允许列表 Origin。生产要求 HTTPS、`__Host-` cookie、SMTP 和非通配 CORS。

### 租户隔离与对象存储

- `paper_project.owner_id` 改为非空 UUID 外键，项目创建只能取当前用户，项目列表按 owner
  过滤。项目、写作、素材、视觉、设置、下载和 SSE 六组 router 共用唯一授权入口；匿名 401，
  外租户与不存在资源统一 404，各 router 的旧 UUID 直查函数已移除。
- 素材、视觉和导出写入 `users/{user_id}/projects/{project_id}/...`；OA 全文写入
  `shared/oa/...`。MinIO bucket 保持无公开 policy，所有二进制只通过认证 API 返回。
- worker 在任务开始时重新读取项目 owner；外部只能经已授权 API 入队，公共学术目录不提供
  反查其他账户项目关系的接口。

### 前端与迁移

- Web 默认同源 `/api/v1`，上传/SSE 均携带 cookie；401 统一回登录，403/404 不再 mock。
  示例数据仅可在开发环境显式开启，生产构建强制关闭。认证区、应用守卫、用户菜单和退出已完成。
- 0004 先用禁用 placeholder 无损承接旧匿名项目；`paperforge-admin bootstrap-admin` 可安全
  认领，CSV 可逐项目拆分。对象迁移按复制、读回 SHA-256 校验、更新 key 的顺序执行，旧路径
  至少保留七天。

### 验收状态

- 全量 Python：380 passed、1 skipped；认证专项 25 个，覆盖会话、令牌、密码策略、限流、
  Origin、设置最小披露和跨租户参数化反例。Ruff、锁文件和差异检查通过。
- 前端 TypeScript 与 Next 生产构建通过（16/16 页面）；隔离数据库完成 0003 → 0004 旧数据
  迁移与 Alembic 无漂移检查。
- 当前开发数据库仍在 0003；上线需按 `docs/getting-started.md §5.1` 先备份，再由指定管理员
  显式执行升级、认领和对象迁移。
