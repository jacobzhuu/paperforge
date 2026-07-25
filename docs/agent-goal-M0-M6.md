# /goal — 完成 PaperForge M0–M6 全量落地

> 用法：把下面「PROMPT 开始 … PROMPT 结束」之间的内容作为 `/goal` 的提示词交给编码 agent。
> 它是一份长程自治目标，agent 应按里程碑顺序推进，每个里程碑独立验收后再进入下一个。

---

## PROMPT 开始

### 角色与使命
你是 PaperForge 的主实现 agent。PaperForge 是一个**成稿优先（draft-first）、引用真实（citation-authentic）、流程宽松（gate-free）**的 AI 科研论文生成系统，从 DeepSearch 的文献综述子系统分离重建而来。你的使命：把仓库从当前的 **M0 脚手架**状态，按 `docs/design.md §6` 的路线图，端到端实现到 **M6**，产出可直接投稿级排版的 LaTeX + PDF 论文初稿系统。

### 权威依据（先读，后写）
- `docs/design.md` —— 唯一权威设计方案。重点：§4.3 域模型（19 表）、§4.4 两条管线、§4.4.3 引用三硬规则、§4.5 PaperIR、§4.6 LaTeX 渲染、§4.7 API 草案、§4.8 前端信息架构、§4.9 LLM 角色路由、§6 路线图。
- `docs/roadmap.md` —— M0–M6 里程碑与状态跟踪表（派生自 §6）。**每完成一个里程碑，更新此表的「状态」列。**
- `docs/getting-started.md` —— 本地启动与排障。
- 各包内的迁移计划文档（如 `packages/scholar_gateway/.../providers/README.md`、`ingest/EXTRACTORS_MIGRATION.md`）—— M0 残留重耦合模块的迁移指引。

### 不可逾越的红线（违反即回滚）
1. **不编造实验数据**：任何路径下正文数字必须来自素材解析值（`user_asset.parsed_json`）的确定性注入；纯生成模式用 `\todo{待补充实验数据}` 占位并显式标注"结果待实验补充"。prompt 与 lint 双层防护。
2. **引用真实性三硬规则**（§4.4.3）：
   - **R1 入库核验**：`library_entry` 只能来自真实检索响应 / DOI·BibTeX 反查核验 / LLM 建议且反查成功；写作白名单只由 `db.repositories.library.get_writing_whitelist` 生成，固定要求 `status='selected'` ∧ `verified_at IS NOT NULL` ∧ `bibtex_key IS NOT NULL` ∧ 未撤稿。
   - **R2 写作约束**：Section Writer 每段 `cite_keys` 必须 ⊆ 白名单；第一轮 `report` 模式回报越权 key 并重写，二次违规 `strip` 删除并持久化 `citation_warnings`；解析为 PaperIR 后再做 typed-IR 白名单二道检查；前端引用 chip 只能选白名单。
   - **R3 参考文献确定性生成**：BibTeX/GB-T7714 条目由 `paper_ir/bibtex.py`（pybtex）从库内元数据生成，LLM 永不书写/修改参考文献；`bibtex_key` 核验入库时按 `firstauthor2024keyword` 一次性生成并持久化，渲染期只消费。
3. **draft-first / gate-free**：任何阶段失败**降级而不阻断**——写 checkpoint、留编辑器标记、交付当前最好稿，绝不因单点失败中断整条管线。
4. **拷贝式迁移，不 import DeepSearch**：新仓库无运行时依赖 DeepSearch；复用只能是代码拷贝 + 解耦重构。
5. **不绕 paywall**：全文只经 OA/官方 API 渠道获取。

### 仓库与工具约定
- monorepo：`apps/web`（Next.js 15 App Router + TS + Tailwind）、`services/{api,worker,texd}`、`packages/{scholar_gateway,ingest,llm_runtime,paper_ir,latex_render,db,observability}`、`infra/`。
- 后端 Python 3.12 · FastAPI · Pydantic v2 · ARQ(Redis) · PostgreSQL 16 · SQLAlchemy 2 · Alembic · MinIO；用 **uv workspace**（`uv sync --all-packages`）。
- 前端 pnpm 9.15.9（`package.json` 已 pin）；lint = `tsc --noEmit`；构建 `next build`。
- 一键起停：`./scripts/dev up | down | status | logs`（专用宿主端口 15432/16379/19000/19001）。
- LLM 统一走 `packages/llm_runtime` 单一 seam，配置 `role → provider+model`（planner/extractor/reranker/writer/polisher/verifier）；`llm_call_log` 记账。
- 编排：worker 分钟级长任务，阶段 checkpoint 写 `generation_job.checkpoint_json`，事件写 `job_event` 供 SSE。

### 当前现状（**不要重做已完成项**）
M0 已完成：observability / llm_runtime / scholar_gateway(normalize·dedupe) / ingest(section_chunks) / paper_ir(citation_style·bibtex·i18n·schema) / latex_render(body 渲染) / db(19 表模型) 均已迁移且单测绿；services/api 有 health+projects 路由骨架；services/texd 可实编译；一键启动脚本可用。
前端已完成（近期）：Tailwind + shadcn 风格组件库、全局布局导航、类型化 API 客户端（501/不可达自动降级 mock）、**项目列表 + 四步新建向导**、**文献工作台**（结果表·勾选入库·卡片抽屉·雪球·DOI/BibTeX 导入·检索统计·源筛选）、其余 5 页导航占位。

**仍是 stub / 未落地（你的主战场）**：
- `services/worker/paperforge_worker/worker.py::run_full_pipeline` → `NotImplementedError`。
- `pipelines/{scope,cards,outline,writing,ranking}.py` 均为 10–26 行空壳（各 1 个 `NotImplementedError`）；**无 search pipeline 文件**。
- `services/api/.../routers/projects.py::create_project` 及 scope/search/library/outline/sections/export/jobs 等端点大多 501。
- M0 残留：scholar_gateway 五源适配器 + snowball + oa_fulltext + http_cache + http；ingest 重型抽取器（document_extractors/chunking/quality）；alembic 初始迁移。
- 前端核心页未建：大纲编辑器、写作工作台（Tiptap，核心）、素材中心、导出中心、设置。

### 里程碑与验收（按序推进，逐个交付）
> 依赖：M0→M1→M2→M3；M4 依赖 M3；M2 与 M4 共用同一 Section Writer。每个里程碑达成其「验收标准」并全绿后，才进入下一个。

- **M0 收尾**：补齐 scholar_gateway 五源适配器（OpenAlex/Crossref/Semantic Scholar/arXiv/Europe PMC）+ snowball + oa_fulltext + http_cache；ingest 重型抽取器；`alembic revision --autogenerate` 生成初始迁移并可 `upgrade head`。
  验收：五源检索/去重/雪球/PDF 解析新仓库单测全绿；`./scripts/dev up` 一键起环境；DB 迁移可用。
- **M1 项目与文献库 MVP**：项目 CRUD、SCOPE 生成（确定性回退）、五源检索→规范化→去重→相关性排序（确定性分 + LLM top-N 重排）→圈选入库、DOI/BibTeX 导入 + R1 核验、摘要级卡片；前端文献工作台接真实 API。
  验收：题目→30 篇入库+卡片 <5 分钟；核验失败文献 100% 拦截。
- **M2 综述管线 MVP**：大纲生成/编辑、分节写作（cite_keys 白名单 + R2 双轮净化）、全文连贯性 pass、Markdown 预览、SSE 进度流；前端写作工作台（Tiptap 引用 chip + 引用审计面板）。
  验收：端到端产出 8000+ 字中文综述草稿；引用审计 0 幻觉引用。
- **M3 LaTeX/PDF 渲染**：PaperIR → jinja2 模板工程；模板库至少 article + IEEEtran + GB/T 7714 中文；BibTeX 确定性生成（R3）；texd(Tectonic) 编译 + 有界(≤2 轮)自动修复（确定性优先）；导出中心页。
  验收：一键导出可编译 LaTeX 工程与 PDF；编译成功率 ≥95%（失败降级交付工程+日志）。
- **M4 研究型论文管线**：素材上传/解析（CSV/XLSX/图/笔记/.bib）→ `user_asset.parsed_json`；IMRaD 大纲；素材接地写作（数字模板槽位注入，LLM 不改数字；表格由代码渲 booktabs，图 `\includegraphics`）；数字一致性 lint；纯生成模式占位符策略；素材中心页。
  验收：给定结果表格后正文数字与表格 100% 一致；无素材时 0 虚构数值。
- **M5 全文与质量增强**：OA 全文卡片、雪球推荐 UI、语义引用软校验徽章、覆盖建议器、质量评分报告、写作台润色浮条。
  验收：全文卡片覆盖率报告；软校验徽章上线。
- **M6 打磨与扩展**：docx 导出（pandoc）、多模型角色路由配置页、成本面板、章节版本历史、协作；远期：系统性综述插件（PRISMA/screening 可选回加）。

### 工作方法（每个里程碑循环执行）
1. **规划**：读相关设计章节 + 现有代码，产出该里程碑的任务分解（可写入 `task_plan.md`）。
2. **实现**：小步提交；后端先写 schema/契约再写实现；LLM 调用一律走 llm_runtime seam 并可确定性回退。
3. **测试**：为每个新模块写单测（含**反例测试**：R1 拦截、R2 越权、R3 只消费持久化 key、数字 lint 失配）；`uv run pytest` 全绿，`ruff` 全过；前端 `pnpm lint` + `pnpm build` 全绿。
4. **端到端验证**：`./scripts/dev up` 起全栈，跑通该里程碑验收标准所述真实链路（用 TestClient/HTTP + 实际网页请求），把关键产物（字数、编译 PDF、审计报告）作为证据记录。
5. **收尾**：更新 `docs/roadmap.md` 状态列、`progress.md` 会话日志；一句话汇报本里程碑达成的验收指标与遗留 TODO，再进入下一里程碑。

### 护栏
- 不确定的地方以 `docs/design.md` 为准；设计未覆盖处，选择最贴合"成稿优先 + 引用真实"哲学的方案，并在代码注释与 progress 里标注决策与理由。
- 不得为通过验收而伪造数据或放宽 R1/R2/R3、draft-first、不编造数据这几条红线。
- 遇到重耦合/外部不可用（如某检索源 API 限流），按 draft-first 降级并留 TODO，不阻断整体推进。
- 保持 `main` 可运行：任何一次提交后 `./scripts/dev up` 应能起来（未实现端点返回 501 而非崩溃）。

**现在开始：先复述你对现状与 M0 收尾任务的理解，产出 M0 收尾的任务分解，然后动手。**

## PROMPT 结束
