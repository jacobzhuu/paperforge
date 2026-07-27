# PaperForge 前端设计纲领（UI Design）

> 本文是 `docs/design.md` §4.8「前端信息架构」的展开，定位是**前端的宪法**：
> 设计原则、已成事实、待办改造、以及**明确不做**的事情各占一节。
>
> 写作背景：2026-07-26 对照 Claude 的设计哲学做了一轮前端评估。评估的输入是一份
> 外部建议（十七条，见 §6「外部建议对照表」），本文是结合仓库实际代码审计后的结论——
> **建议里有相当一部分是在劝我们别做我们从未做过的事**，这类条目在 §2 逐条标注为「已成事实」，
> 目的是防止后来者按建议原文再"改"一遍已经对的东西。

---

## 1. 设计哲学

PaperForge 不是一个通用 AI Chat，也不是一条论文生成流水线的前台。它是：

> **一个以论文成稿为中心、以文献证据为支撑、过程尽量不打断用户的科研写作工作空间。**

一句话口号：**Paper First, Evidence Always, Complexity on Demand.**

后端三条核心理念到 UI 的翻译：

| 后端理念（design.md §1.4） | UI 必须表达成 |
|---|---|
| Draft-first | 用户始终看到"正在形成的论文"，而不是流水线的状态机 |
| 引用必须真实 | 引用与证据随手可查，但不抢占正文的视觉带宽 |
| 人机协作节点显式化 / gate-free | 警告提醒用户，但不阻断工作 |

### 1.1 八条设计原则

**01 — Paper is the interface**
论文本身始终是产品的中心对象，pipeline 是达成它的手段，不是展示对象。

**02 — Start from intent**
入口从研究意图开始（"你想写什么"），而不是从功能导航开始（"选择一个模块"）。

**03 — Draft first**
尽早、尽可能增量地让用户看到可读、可编辑的 Draft。等待期不是空白期。

**04 — Evidence on demand**
每一处引用都可溯源，但默认不干扰阅读。证据距离正文永远只有一次点击。

**05 — Warnings, not walls**
非致命问题一律 warning，不阻塞论文形成；致命错误极少出现且必须给出下一步。

**06 — Progressive disclosure**
高级参数、检索设置、模型配置、阶段详情按需暴露，默认收起。

**07 — Typography over containers**
优先用排版、间距、层级分层，而不是给每个语义段落套一张 Card。

**08 — Calm by default**
即使后台正在跑一个 20 分钟、8 阶段、上百次 LLM 调用的 workflow，前台仍然安静。

### 1.2 三种容器（其余一律不是容器）

1. **Prompt Canvas** —— 意图输入。首页、新建论文。
2. **Paper Canvas** —— 论文本体。写作台中栏、全文预览。
3. **Inspector** —— 证据 / 引用 / 校验 / 设置。右栏或抽屉。

不属于这三类的界面元素，默认用标题 + 留白 + 分隔线组织，**不套 Card**。

---

## 2. 已成事实（不要"改进"回去）

以下决策已在代码中落实且有明确理由。任何提议改动这几项的建议，必须先读对应文件的注释。

| 项 | 现状 | 位置 |
|---|---|---|
| 左侧栏只放用户对象 | 已砍至「项目 / 设置」两项 + 项目切换器；七个工作台入口早已移出（每个都要手工带 `?project=`，漏一处就掉回空态） | `apps/web/components/layout/sidebar.tsx:20` |
| 写作台三栏编辑空间 | `章节树 \| 正文(72ch 限宽) \| 校验面板`，含专注模式；<2xl 时右栏降级为抽屉而非硬塞三栏 | `apps/web/components/writing/writing-workbench.tsx:362` |
| 进度默认一行、按需展开 | 单行「阶段 + 对象 + 用时 + 百分比」，ChevronDown 展开阶段序列与降级详情 | `apps/web/components/jobs/job-progress-card.tsx:81` |
| 引用不堆 badge | 正文只出现 cite chip，点击进抽屉看核验状态 / DOI / 摘要 / 文献卡片 | `apps/web/components/library/card-drawer.tsx` |
| gate-free 的可视化 | 「降级完成」badge、逐阶段重跑按钮、每步可点不设前置校验 | `job-progress-card.tsx`、`project-pipeline-nav.tsx` |
| 正文限宽与阅读排印 | `--reading-font-size: 17px` / `line-height: 1.75`，正文 72–78ch | `apps/web/app/globals.css:53`、`.pf-prose` |
| 文献列表虚拟化 | 已从一次性渲染全部条目的 `<table>` 改掉（实测单项目 330 条） | `apps/web/components/library/entry-list.tsx:18` |
| 对比度阈值 | `--muted-foreground` 压到 44%、`-strong` 系列专供"彩色文字压同色浅底" | `apps/web/app/globals.css:23`、`:39` |

---

## 3. 待办改造

按 ROI 排序。每条给出现状、目标、约束与工作量。

### 3.1 ⭐ 论文边写边出现（后端零改动）

**现状**：`services/worker/paperforge_worker/pipelines/document.py:139` 已经**写完一节存一节**，
并逐节 `emit("write.section", {section, title, words, generator})`；框架章节（摘要/引言/结论）
同样发事件。前端 `apps/web/lib/useJobTracker.ts:64` 收到这个事件后**只拿它拼了一句状态文案**。

**目标**：写作台监听 `write.section`，增量刷新 `listSections()`。一次 18 分钟的写作从
"盯着进度条"变成"章节一节一节浮现"。这是原则 03 最直接的兑现。

**约束**：
- 增量刷新不能覆盖用户正在编辑的脏草稿（`writing-workbench` 有 `dirty` 态与 beforeunload 保护）。
  新章节到达时若当前节 dirty，只更新章节树，不动编辑器。
- 概览页与大纲页也可以顺带订阅，但优先做写作台。

**工作量**：约 40 行前端。全部方案里 ROI 最高。

### 3.2 首页从 Dashboard 转向 Prompt Canvas

**现状**：`apps/web/app/page.tsx` 只是 `redirect('/projects')`；`/projects` 是标准 SaaS
卡片网格 + 搜索框 + 两个下拉筛选。

**目标**：

```
PaperForge                                             ○

                   今天想研究什么？

      ╭─────────────────────────────────────╮
      │ 描述你的研究主题、问题或论文目标…    │
      │                                     │
      │ + 材料                  综述论文 ▾  │
      ╰─────────────────────────────────────╯

                   最近的论文
      Three-Stage Pre-Training for …        2 小时前
      序列推荐的对抗攻击 …                   昨天
```

类型选择降级为输入框内的下拉（Progressive Disclosure），不再是两张对等大卡片。
`/projects` 保留为完整列表页，但不再是首页。

**前置阻塞（已解除，2026-07-26）**：
后端此前**没有 `PATCH /projects/{id}``**，而 `createProject` 必须带 `title`——
一个从「描述你的主题」推导出的标题若不可改，用户就被永久锁在一个烂标题上。
现已补齐：`PATCH /projects/{id}` 支持 title / topic / venue_template / language /
citation_style / writing_mode / contribution_points，语义是**字段缺席 = 不改、
显式 null = 清空**；`paper_type` 刻意不可改（它决定管线形状，换类型 = 新建项目）。
题目在项目头就地可改（`components/project/project-title.tsx`）。

**其他约束**：
- `+ 材料` 的文件必须先在浏览器内暂存——`uploadAsset(projectId, …)` 需要项目已存在，
  所以顺序是：选文件 → 建项目 → 补传。
- 拖文件进输入框应自动把类型切到「研究型论文」（见 §4.2）。
- 高级参数（模板 / 语言 / 引用样式 / 写作模式）从四步向导收进"更多设置"折叠区；
  向导本身保留，作为 `/projects` 页的入口。

### 3.3 接上 serif 排版

**现状**：`--font-serif` 在 `apps/web/app/globals.css:51` 定义了，
`tailwind.config.ts` 也映射了 `font-serif`——但**全仓库 `.tsx` 里零引用**。
这是一条已经铺好却没接的线。

**目标**：

| 用 Serif | 用 Sans |
|---|---|
| PaperForge 品牌字 | 导航、按钮 |
| 论文标题、章节标题 | 元数据、状态、计数 |
| 正文预览、空状态 headline | 设置项、文献条目信息 |

产生「学术出版物 × 现代 AI」的气质，比通用 SaaS 风更贴产品。

**约束（重要）**：中文界面**不要全面 serif**。`Songti SC` 在小字号、非 Retina 屏上
笔画会糊。规则定为：**仅 ≥18px 的标题与正文预览用 serif，其余一律 sans。**

### 3.4 暖色系配色

**现状**：`--background: 210 40% 98%`（冷调蓝白），整套是科技蓝。

**目标**：

```
Page        暖白（warm off-white）
Surface     white
Primary     charcoal
Secondary   warm gray
Accent      muted orange / copper
```

**约束（重要）**：`globals.css` 第 23–24 行与 39–43 行的注释是**实测出来的对比度约束**——
`--muted-foreground` 必须压到 44% 才能在 `--muted` 底上过 AA 4.5:1；`-strong` 系列
存在的唯一理由是"彩色文字压在同色浅底上"时原色只有约 2:1。
换暖色底色后这些比值**必须重算**，不能照搬任何外部产品的色值。改完要重新验一遍
AA，尤其是：未选中 Tab、muted 徽章、warning 文字压 warning/10 底。

### 3.5 减少 Card

**现状**：全仓库 135 处 `<Card`，分布在 14 个文件。

**重灾区**：`apps/web/components/project/project-overview.tsx` ——
「稿件快照 / 近期任务 / 成本 / 版本」四张卡拼成的经典 SaaS 网格，是原则 07 的反面教材。

**目标**：按 §1.2 的三容器规则重写概览页——

```
下一步
生成大纲                                    [跑通全管线] [去大纲 →]
当前 46 篇文献已入库，可以开始组织章节结构。

稿件                                                     32,481 字
46 篇文献 · 7 章 · 3 个导出产物
0 幻觉引用，全文 128 个引用键均在白名单内

近期任务
完成      写作            2 小时前
降级完成   检索            3 小时前

成本
调用 214 次 · 输入 1,204,553 tokens · 输出 186,220 tokens
```

靠 whitespace / typography / subtle divider 分层。同一规则适用于导出中心与设置页。

### 3.6 文献库改成 Research Context

**现状**：文献工作台仍偏"数据库视图"（ID / 标题 / 年份 / DOI / 分数 / 状态）。

**目标**：主视图回答的不是"数据库里有什么"，而是"**这些论文为什么出现在我的论文里**"：

```
文献                                                        38

Attention Is All You Need
Vaswani et al. · NeurIPS 2017
被引用于 引言、相关工作

Retrieval-Augmented Generation for Knowledge-Intensive NLP
Lewis et al. · NeurIPS 2020
被引用于 相关工作
```

详细 metadata 展开后再看（抽屉已有）。

**实现**：「被引用于哪些章节」目前**没有现成后端数据**，但可以在前端从 `listSections()`
返回的 IR 里提取每节的 cite-key，反查出 `cite_key → sections[]` 映射。
不需要后端改动。中等工作量，产品差异化收益高。

### 3.7 Pipeline 导航按写作模式分叉

见 §4.1——这一条是对外部建议的**修正**，不是照搬。

**目标**：
- `writing_mode === 'auto'`：导航降级为一行环境状态（`● 正在写作 · 相关工作`），
  点开才看到完整阶段列表。此时用户本来就不介入。
- `writing_mode === 'assisted'`：**保留可导航的步骤**，但降低视觉重量——
  去掉步骤间的连接线与圆点描边，改为纯文字 + 极淡的当前态底色，
  完成态用一个小圆点而不是带底色的对勾徽章。

---

## 4. 明确不做（及理由）

### 4.1 不把 Pipeline 导航整体替换成一行状态

外部建议主张把 stepper 完全弱化成 `Writing your paper…`。**对 PaperForge 是错的。**

Claude 的 chat 只有一个不透明步骤；PaperForge 有 8 个**带人工决策点**的步骤，
而 assisted 模式的定义就是"用户圈选入库、逐章确认"（design.md §1.4 原则 4）。
把导航换成一行状态，等于在协作模式下拿走用户的方向盘——他无法回到文献工作台调整入库集合，
也无法在写作前修大纲。

正确解法是按 `writing_mode` 分叉（§3.7），而不是一刀切。

### 4.2 首页不做成纯文本单输入框

`apps/web/lib/pipeline.ts:46` 的 `ORIGINAL_FLOW` 明确把 `assets` 排在第一位，
理由写在注释里：素材是写作的**输入**，排在写作之后等于让作者写完正文才被邀请上传
那些本该为正文数字接地的数据。

也就是说，研究型论文的起点是**一批实验数据文件**，不是一句话。
一个只能接受文本的输入框对这条管线是结构性错配。

解法：文本框为主，但**支持拖拽文件进来，并在检测到文件时自动切换到研究型论文**。

### 4.3 不做全面 serif

见 §3.3 的约束。中文 serif 在小字号下的可读性代价是真实的。

### 4.4 不照搬任何外部产品的色值

见 §3.4 的约束。本仓库的对比度参数是实测得到的，不是抄来的。

---

## 5. 分期执行计划

| 期 | 内容 | 预估 | 涉及层 | 状态 |
|---|---|---|---|---|
| **一期：气质** | §3.3 接上 serif；§3.4 暖色变量 + 重算 AA；§3.5 概览页去卡片化 | 1–2 天 | 纯前端 | ✅ 2026-07-26 |
| **二期：核心心智** | §3.1 `write.section` 增量浮现；§3.7 导航按 writing_mode 分叉 | 2–3 天 | 纯前端 | ✅ 2026-07-26 |
| **三期：入口** | `PATCH /projects/{id}`；§3.2 首页 Prompt Canvas + 文件拖拽分流；`/projects` 降级为列表页 | 3–4 天 | 前端 + 后端 | ✅ 2026-07-26 |
| **四期：差异化** | §3.6 文献库改 Research Context + 章节反查 | 3–5 天 | 纯前端 | ✅ 2026-07-26 |

一期与二期互不依赖，可并行。三期依赖后端 PATCH 端点先落地。
各期交付明细与验收证据见 `docs/roadmap.md` §M7。

---

## 6. 外部建议对照表

2026-07-26 收到的十七条建议逐条裁决，供追溯：

| # | 建议 | 裁决 | 去向 |
|---|---|---|---|
| 一 | 确立设计哲学 Paper First / Evidence Always | 采纳 | §1 |
| 二 | 首页从 Dashboard 转向论文启动空间 | 采纳（需先补 PATCH 端点） | §3.2 |
| 三 | Review/Original 不做两张大卡，收进下拉 | 采纳（但保留文件拖拽分流） | §3.2 / §4.2 |
| 四 | Paper Workspace 三栏结构 | **已成事实** | §2 |
| 五 | 彻底弱化 Pipeline | **部分驳回**，改为按 writing_mode 分叉 | §4.1 / §3.7 |
| 六 | SSE 进度做成环境状态而非 Dashboard | **已成事实** | §2 |
| 七 | Gate-free 的柔和 warning | **已成事实** | §2 |
| 八 | 引用不堆 badge，点击看证据 | **已成事实** | §2 |
| 九 | 文献库从数据库变 Research Context | 采纳 | §3.6 |
| 十 | 暖色视觉语言 | 采纳（须重算 AA） | §3.4 |
| 十一 | Serif / Sans 双字体系统 | 采纳（限 ≥18px） | §3.3 / §4.3 |
| 十二 | 减少 70% 的 Card | 采纳 | §3.5 |
| 十三 | 只保留三种容器 | 采纳 | §1.2 |
| 十四 | 左栏只放用户对象 | **已成事实** | §2 |
| 十五 | 心智模型从 Pipeline 转向 Workspace | 采纳，落点是"边写边出现" | §3.1 |
| 十六 | 八条 Design Principles | 采纳 | §1.1 |
| 十七 | 首页最终形态 | 采纳 | §3.2 |
