# PaperForge UI/UX 优化计划（第二轮）

> 审计与计划日期：2026-07-29
> 范围：`apps/web` 全部页面与组件（18,713 行 TS/TSX）
> 定位：`docs/ui-design.md` 是**前端宪法**（设计哲学 + 已成事实 + 明确不做）；
> 本文是在那份宪法**已经执行完四期之后**（2026-07-26 全部标 ✅）对当前代码做的
> 第二轮审计与执行计划。
>
> **本文不重新讨论 `ui-design.md` §2「已成事实」和 §4「明确不做」的任何一条。**
> 那八项（左栏只放用户对象、写作台三栏、进度默认一行、引用不堆 badge、gate-free
> 可视化、正文限宽、文献列表虚拟化、对比度阈值）与四项（不把 Pipeline 换成一行状态、
> 首页不做纯文本单输入框、不做全面 serif、不照搬外部色值）在本轮复核中**仍然成立且已落实**，
> 下文出现的所有改动都必须与它们兼容。本文只处理宪法执行完之后**新暴露出来的问题**，
> 以及宪法覆盖不到的层面（设计 token、组件尺度、失败态、移动端）。

---

## 0. 一页摘要

第一轮改造解决的是**气质与心智模型**（暖色、serif、去卡片化的概览页、边写边出现、
Prompt Canvas 首页、文献库转 Research Context）。这些都落地了，而且质量很高。

这一轮暴露的问题在**另外三个层面**：

| 层面 | 一句话诊断 |
|---|---|
| **设计 token 层** | 尺度系统已塌陷：`Button` 四个 size 全是 `h-11`，于是 12 处调用方手工写 `h-6/h-7/h-8` 压回去；`text-xs` 用了 225 次而 `text-sm` 只有 149 次，12px 事实上成了正文字号 |
| **信息架构层** | 存在**两条互相冲突的建项目路径**（首页 Prompt Canvas 与 `/projects` 四步向导），默认值、必填项、交互范式三者都不同 |
| **状态与产物层** | 「失败 → 重试」入口只存在于运行中的任务条里，任务一结束入口就消失；「文献 / 引用 / 图表 / 导出产物」四者之间没有统一的关系视图 |

优先级：**P0 设计 token 收敛 → P1 建项目路径合并 → P2 失败/重试与产物地图 → P3 页面级精修与移动端**。

全部改动**纯前端**，不触碰任何 API、数据结构或后端逻辑。现有 13 个测试文件必须保持通过。

---

## 1. 审计：主要问题与影响

每条给出：**现状（含 `file:line`）→ 影响 → 判定**。

### A. 设计系统层

#### A1 ⚠️ Button 尺寸标度已塌陷，调用方在手工绕过它

**现状** —— `components/ui/button.tsx:17-22`：

```ts
size: {
  default: 'h-11 px-4 py-2',
  sm:      'h-11 rounded-md px-3 text-xs',   // 和 default 一样高
  lg:      'h-11 rounded-md px-6',           // 和 default 一样高
  icon:    'h-11 w-11',
}
```

四个 size **高度完全相同**。`size="sm"` 只改了横向 padding 和字号，不改高度。
全仓库有 50 处 `size="sm"`，它们期望的"更小的按钮"并不存在。

于是调用方开始手工压高度，绕过组件：

| 位置 | 覆写 | 实际触控高度 |
|---|---|---|
| `jobs/job-progress-card.tsx:127` 跳过润色 | `h-6 px-2 text-xs` | **24px** |
| `jobs/job-progress-card.tsx:141` 暂停 | `h-6 px-2 text-xs` | **24px** |
| `jobs/job-progress-card.tsx:153` 取消 | `h-6 px-2 text-xs` | **24px** |
| `project/project-shell.tsx:186` 继续 | `h-7 px-2 text-xs` | 28px |
| `project/project-shell.tsx:193` 放弃 | `h-7 px-2 text-xs` | 28px |
| `scope/scope-editor.tsx:320,399` | `h-8` | 32px |
| `writing/cite-key-picker.tsx:57` | `h-7 pl-7 text-xs` | 28px |
| `library/library-workbench.tsx:403` 过滤框 | `h-8 w-48` | 32px |

**影响**：`h-11` 显然是为了满足 44px 触控目标而设的基线，但它把整个标度压成了一个值，
结果调用方为了做出层级只能覆写——**恰恰在最需要点击的地方把目标压到 24px**。
「暂停 / 取消一个已经跑了 15 分钟的任务」是全应用风险最高、最需要点准的操作，
现在是三个 24px 高、挤在一行右侧、`text-xs` 的 ghost 按钮。移动端几乎点不中。

**判定**：**P0**。这是本轮最该先修的一条，因为它同时是可访问性缺陷和设计系统缺陷。

#### A2 ⚠️ 字号层级倒挂：12px 成了事实上的正文字号

**现状** —— 全仓库字号分布：

```
text-xs (12px)   225 处   ← 最多
text-sm (14px)   149 处
text-lg (18px)    11 处
text-xl (20px)     7 处
text-base(16px)    6 处   ← 几乎不用
text-2xl          4 处
text-3xl          1 处
```

单文件密度前五：`validation-panel.tsx` 19 处、`publication-metadata.tsx` 17 处、
`writing-workbench.tsx` 13 处、`export-center.tsx` 12 处、
`project-overview.tsx` / `job-progress-card.tsx` 各 10 处。

最严重的是 `jobs/job-progress-card.tsx`：一条**动辄 20 分钟、8 阶段、上百次 LLM 调用**
的任务，它的全部状态输出——阶段名（:117）、正在处理的对象（:119）、已用时长与百分比（:161）、
降级摘要（:221）、重连提示（:206）、阶段序列 pill（:237）——**全部是 12px**。

**影响**：违反 `ui-design.md` 原则 03「等待期不是空白期」的精神。等待期确实有信息，
但信息以最小字号呈现，用户要凑近屏幕才能读出"跑到哪了"。同时 `text-xs` +
`text-muted-foreground` 的组合虽然过了 AA 的 4.5:1（`globals.css:39` 实测 5.32:1），
但 12px 在 AA 下属于"正常文本"而非"大文本"，对比度余量本就不该再叠加尺寸劣势。

**判定**：**P0**。不是全局把 xs 换成 sm，而是建立**语义化字号层级**（见 §3.2），
逐个语义位置归位。

#### A3 圆角与层级无 token，5 种值混用

**现状**：`rounded-md` 75 处、`rounded-lg` 31 处、`rounded-full` 29 处、
`rounded-xl` 23 处、`rounded-2xl` 1 处。

- `Card` 用 `rounded-xl`（`ui/card.tsx:8`）
- `Button` / `Input` 用 `rounded-md`（`ui/button.tsx:6`、`ui/input.tsx:10`）
- 首页输入框用 `rounded-xl`（`home/prompt-canvas.tsx:176`）
- 虚线空态有的 `rounded-xl`（`library/entry-list.tsx:79`、`app/projects/page.tsx:236`）
- 概览页「下一步」用 `rounded-lg`（`project/project-overview.tsx:226`）
- `rounded-2xl` 全仓库只有 1 处 —— 孤例

`tailwind.config.ts:63-67` 已经定义了 `lg/md/sm` 三档映射到 `--radius`，
但 `rounded-xl` 和 `rounded-full` 绕过了这套映射，直接用 Tailwind 默认值。

**影响**：同一层级的容器（空态框 vs 概览 CTA vs Card）圆角不一致，
是"整体风格不统一"最容易被察觉的来源之一。

**判定**：**P1**。收敛为 3 档语义圆角。

#### A4 Card 与「无 Card」两套规范并存且互相矛盾

**现状**：全仓库 **43 个根 `<Card>` 实例**，分布在 17 个文件（157 个 Card 家族标签）。

> 说明：`ui-design.md` §3.5 记的「135 处 `<Card`」是把 `CardHeader` / `CardContent` /
> `CardTitle` 一起数进去的口径。按根实例算，当前是 43。两个数字口径不同，
> 不能直接比较增减。

分布：

```
validation-panel.tsx    11   ← 剩下的重灾区
settings-page.tsx        5
export-center.tsx        5
scope-editor.tsx         4
outline-editor.tsx       4
markdown-preview.tsx     2
search-stats.tsx         2
其余 10 个文件各 1
```

`project-overview.tsx` 已经按原则 07 重写成 0 Card 并且效果很好（用小标题 + 留白 +
分隔线分层，`:529-536` 的 `SectionTitle`）。但**右栏和工作台没有跟上**：

- `validation-panel.tsx` 在一个 **19rem 宽**的栏位里塞了 11 张 Card，
  每张自带 `rounded-xl border shadow-sm` + `p-5` 内边距（`ui/card.tsx:8,17,38`）。
  19rem = 304px，减去两侧 border 和 20px×2 padding，实际内容宽度只剩 ~260px。
- `export-center.tsx:246-252` 出现 **Card 套 Card**：外层 `<Card>` 包 `<CardContent className="divide-y p-0">` 再包一列行。这里 Card 只提供了一圈边框，语义为零。

**影响**：原则 07（Typography over containers）在概览页执行了，在其他页面没有。
同一个产品里存在两种分层语言——用户在概览页学到的"留白 = 分组"，
到写作台右栏变成"边框 = 分组"。这是"过多边框"最主要的来源。

**判定**：**P1**。

#### A5 状态标签有四套并存的表达方式

同一个语义（任务/条目的状态）在四个地方用了四种视觉：

| 位置 | 表达 |
|---|---|
| `project-overview.tsx:608-613` 近期任务 | 小圆点 + 定宽状态词（`text-xs` + 语义色文字） |
| `app/projects/page.tsx:304` 项目卡 | `<Badge variant>` 圆角实心块 |
| `validation-panel.tsx:69-92` 信任摘要 | `<Badge>` + 图标 |
| `job-progress-card.tsx:236-242` 阶段序列 | 自定义 pill（`rounded-full border px-2 py-0.5`），**未复用 Badge** |
| `project-pipeline-nav.tsx:138` 待办角标 | 自定义 `rounded-full bg-muted px-1.5 text-[11px]` |

`project-overview.tsx:594` 的注释明确说明了为什么改成圆点（"五行任务就是五个色块"）——
这个判断是对的，但只在那一个文件里执行了。

**影响**：状态语言不统一；`text-[11px]` 是全仓库唯一一个跌破 12px 下限的界面字号
（`provider-filter.tsx:59` 的注释说"最小字号统一到 12px"，但 pipeline-nav 破了这条）。

**判定**：**P1**。

### B. 信息架构与核心路径

#### B1 🔴 存在两条互相冲突的「新建项目」路径

**现状**：

| | 首页 Prompt Canvas | `/projects` 四步向导 |
|---|---|---|
| 文件 | `home/prompt-canvas.tsx` (449 行) | `projects/new-project-wizard.tsx` (418 行) |
| 交互 | 单个多行输入框 + 行内下拉 + 折叠「更多设置」 | `Dialog` + 4 步 Stepper + 6 个表单控件 |
| 题目 | **自动推导**（`deriveTitle` :18，截断 60 字） | **必填**（`:91` `canNext` 校验） |
| 主题 | 输入框正文即 topic | 独立可选字段 |
| 默认引用样式 | review→gbt7714 / original→ieee（:105） | 同左（:73） |
| 贡献点 | ❌ 无入口 | ✅ `ContributionEditor` (:359) |
| 文件上传 | ✅ 拖拽 + 自动切类型（:93-100） | ❌ 无 |
| 落地 | `/projects/{id}`（或 `/assets` 若上传失败） | `/projects/{id}` |
| 入口 | 侧栏「新论文」+ 首页 | 侧栏「项目」→ 右上「新建项目」+ 空态两张卡 |

两条路径都能建出项目，但**能力集不同**：走首页拿不到「贡献点」，
走向导拿不到「上传素材」。而研究型论文这两者恰恰都是核心输入。

`ui-design.md` §3.2 原计划是「向导本身保留，作为 `/projects` 页的入口」——
但当时没有预见到两者会分叉出**不同的能力集**。现在的状态是：
用户必须先猜自己要哪些能力，才知道该从哪个入口进。

**影响**：直接违反本次的验收标准「新建研究任务的操作路径清晰，主次输入明确」。
也是"表单堆叠 + 冗余操作步骤"最大的一处——向导的第 3 步（模板/语言/引用样式）
和第 4 步（写作模式）**全部字段都有合理默认值**，四步里有两步可以完全不填。

**判定**：**P1（最高优先级的结构性改动）**。方案见 §4.2。

#### B2 首页无法在数秒内说明产品是什么

**现状** —— `home/prompt-canvas.tsx:156-162`：

```tsx
<h1>{greeting()}</h1>              {/* 「下午好」 */}
<p>今天想研究什么？</p>
```

页面上没有任何一处说明 PaperForge **做什么、产出什么**。
`RecentPapers` 在零项目时 `return null`（`:417`），所以**新用户看到的是**：
一句问候 + 一句提问 + 一个空输入框 + 一个「开始」按钮。

产品的三条核心承诺（成稿优先 / 引用真实 / 产出 LaTeX+PDF）只出现在
侧栏 logo 下方 12px 的一行副标题里（`layout/sidebar.tsx:92`）。

**影响**：**直接不满足本次验收标准第 1 条**（"用户进入首页后能够在数秒内理解
PaperForge 的用途和主要入口"）。

注意这**不是**要把首页改回 Dashboard——`ui-design.md` §4.2 已裁决过。
需要的只是在 Prompt Canvas 的框架内补上「这是什么」和「零项目时该怎么开始」。

**判定**：**P1**。

#### B3 🔴 失败与重试的入口随任务条一起消失

**现状**：单阶段重跑的唯一入口在 `jobs/job-progress-card.tsx:269-277`——
`showDetail` 展开后、`warnings` 列表里、每条 warning 右侧的「重跑」按钮。

`project-shell.tsx:164` 只在 `tracked` 存在时渲染 `JobProgressCard`。
`tracked` 来自 `useJobTracker`，任务进入终态后即被清空。

于是：**任务失败或降级完成之后，重跑入口就不存在了**。

`project-overview.tsx:595-624` 的「近期任务」列表能看到「失败 / 降级完成」，
但每一行都是纯文本 `<li>`，**不可点、不可展开、没有重跑**（:608-619）。
用户看得到"检索失败了"，但没有任何一个可以点的东西。

`retryStage` 的能力是齐的（`project-shell.tsx:89-106`，覆盖 8 个阶段），
只是暴露面太窄。

**影响**：违反本次验收标准「任务运行状态和下一步操作始终可见」。
gate-free 的产品哲学是"失败降级不阻断"，但降级之后用户必须能**回头补救**——
现在这条路径断了。

**判定**：**P2（高价值，纯前端，无需后端改动）**。

#### B4 产物之间的关系没有统一视图

**现状**：单向关系都做了，但没有汇总：

- 文献 → 章节：`lib/citation-usage.ts` + `entry-list.tsx:215` 的 `Provenance` ✅（第一轮成果，做得很好）
- 图表 → 章节：`lib/figure-numbering.ts` 有全文编号，但视觉工作台不显示"这张图在第几节"
- 导出产物 → 文稿版本：`export-center.tsx` 按"运行"分组，`project-overview.tsx:690` 的
  `VersionSummary` 单独显示 `v{n}`，两者**不关联**
- 引用审计 → 文献库：`audit.hallucinated_cite_keys` 只在写作台右栏出现，
  点不进文献库

**影响**：验收标准「文献、引用、图表和论文产物之间的关系清楚」当前只满足了一条边。
用户拿到一份 PDF，无法回答"这份 PDF 是哪个版本的文稿、含哪些图、引了哪些文献"。

**判定**：**P2**。

### C. 页面级问题

#### C1 文献工作台右栏有一张永不变化的静态说明卡

`library/library-workbench.tsx:483-492`：一张标题为「引用真实性」的 Card，
内容是 R1/R2/R3 三条规则的文字说明。它**没有任何数据绑定**，
在 20rem 的固定右栏里永久占位。

这是文档内容放进了产品界面。应收进一次性提示或帮助入口。

#### C2 写作台有一个永久禁用的按钮

`writing/writing-workbench.tsx:651-658`：「重写本章」`disabled` 硬编码，
注释说明"后端只有整份文稿的 POST /sections/generate，没有按 section 的端点"，
并主张"显示但禁用，让用户看得见能力边界"。

这个理由在第一轮成立，但代价是：写作台工具行 4 个控件里有 1 个**永远点不了**，
而 `disabled:opacity-50`（`ui/button.tsx:6`）让它看起来只是"暂时不可用"。
用户会反复尝试。`title` 提示在移动端根本不可见。

改为：从工具行移除，把这条能力边界写进「重新生成全文」的确认对话框描述里。
**不改后端。**

#### C3 写作台头部动作过密

`writing-workbench.tsx:558-575`：`WorkbenchHeader` 的 actions 有 4 项
（保存状态 / 放弃草稿 / 保存本节 / 重新生成全文），
下方工具行再有 4 项（字数 / 重写本章 / 校验 / 专注模式）。
`WorkbenchHeader` 用 `flex-wrap`（`workbench-header.tsx:23`），
窄屏会换行成 2–3 行按钮堆。

#### C4 导出中心 Card 套 Card

`export-center.tsx:246-252`、`:353`、`:420`：三处 Card 只提供边框，
其中 `:246` 是 `<Card><CardContent p-0><行列表>` —— 纯装饰。

#### C5 首页移动端首屏位置偏低

`home/prompt-canvas.tsx:156`：`pt-[12vh]`。桌面端合适；
移动端叠加 `AppShell` 的 `pt-14`（`auth/app-shell.tsx:27`）后，
小屏上输入框会被推到接近首屏中部偏下。

### D. 可访问性

| # | 问题 | 位置 | 等级 |
|---|---|---|---|
| D1 | 任务控制按钮触控目标 24px（<44px） | `job-progress-card.tsx:127,141,153` | **严重**（同 A1） |
| D2 | `text-[11px]` 跌破 12px 下限 | `project-pipeline-nav.tsx:138` | 中 |
| D3 | `role="listbox"` 内每个 `option` 都是 tab stop，未用 roving tabindex；行会随虚拟滚动卸载，焦点会丢 | `library/entry-list.tsx:91,148` | 中 |
| D4 | 状态变化的 live region 覆盖不足：全仓库仅 5 处 `aria-live`（任务条 1、toast 2、写作台 2）。批量入库/排除、文献勾选、导出触发均无播报 | 多处 | 中 |
| D5 | `disabled` 按钮无可访问的原因说明（仅 `title`，移动端与读屏不可靠） | `writing-workbench.tsx:651` | 低（C2 一并解决） |

**做得好、不要动的**：`ui/tabs.tsx` 的完整 WAI-ARIA 标签组实现（方向键 + Home/End +
roving tabindex，`:76-90`）、`globals.css:236` 的 `prefers-reduced-motion`、
`globals.css:16-62` 每一条色值旁的实测对比度注释、`lib/useFocusTrap.ts`。
对比度体系已经过实测验算，**本轮不改任何色值**。

### E. 移动端

| # | 问题 | 位置 |
|---|---|---|
| E1 | Pipeline 导航横向滚动无边缘渐变提示，且**当前步不会自动滚入视野**——第 6 步时用户看到的仍是第 1–3 步 | `project-pipeline-nav.tsx:58` |
| E2 | 写作台在 `<xl` 塌成单列，章节树（`12rem` 网格列）在移动端变成全宽块，正文被推到首屏之下 | `writing-workbench.tsx:611-623` |
| E3 | 文献列表 `max-h-[calc(100vh-18rem)]`：移动端 `18rem` 的预留是按桌面端筛选区高度算的，窄屏筛选区换行后更高，列表可视区被压到很小 | `entry-list.tsx:89` |
| E4 | 文献筛选区在窄屏换行成 3–4 行（Tabs + 计数 + 排序 + 搜索框 + 全选行 + 批量按钮组） | `library-workbench.tsx:373-458` |
| E5 | `WorkbenchHeader` actions 在窄屏换行成多行按钮堆（7 个工作台全部受影响） | `workbench-header.tsx:23` |
| E6 | 断点使用分布：`sm:` 42、`lg:` 42、`md:` 10、`2xl:` 9、`xl:` 3。`md`（768px，平板竖屏 + 侧栏出现的临界点）几乎没被用来做布局决策，而侧栏正是在 `md` 出现的（`sidebar.tsx:155`） | 全局 |

---

## 2. 改版方案总览与页面优先级

### 2.1 三条主线

```
主线一：收敛设计 token        —— 让"统一、克制"有可执行的定义（P0）
主线二：合并建项目路径        —— 让"新建任务"只有一条路径、一套能力（P1）
主线三：补齐失败态与产物关系  —— 让"下一步"在任何状态下都可见（P2）
```

### 2.2 页面优先级

| 序 | 页面 / 组件 | 主要问题 | 优先级 |
|---|---|---|---|
| 1 | `ui/button.tsx` + 12 处覆写调用方 | A1 触控目标 + 标度塌陷 | **P0** |
| 2 | `jobs/job-progress-card.tsx` | A1 + A2 + A5，长任务的唯一反馈面 | **P0** |
| 3 | 建项目路径（`prompt-canvas` + `new-project-wizard`） | B1 双路径分叉 | **P1** |
| 4 | 首页 `home/prompt-canvas.tsx` | B2 + C5 | **P1** |
| 5 | `writing/validation-panel.tsx` | A4（11 张 Card / 19rem）+ A2（19 处 xs） | **P1** |
| 6 | `project/project-overview.tsx` 近期任务 | B3 失败不可重试 | **P2** |
| 7 | `library/library-workbench.tsx` | C1 静态卡 + E3/E4 移动端 | **P2** |
| 8 | `export/export-center.tsx` | C4 + B4 产物关系 | **P2** |
| 9 | `writing/writing-workbench.tsx` | C2 + C3 + E2 | **P2** |
| 10 | `project/project-pipeline-nav.tsx` | E1 + D2 | **P3** |
| 11 | `settings/settings-page.tsx`、`scope/scope-editor.tsx`、`outline/outline-editor.tsx` | A4 去 Card | **P3** |

---

## 3. 设计规范（新增，落到代码）

原则：**所有 token 落到 `tailwind.config.ts` 与 `globals.css`，组件只消费语义名，不写裸值。**

### 3.1 颜色 —— 本轮不改任何色值

`globals.css:15-119` 的暖色体系与 `-strong` 变体已经过实测验算，
每一条旁边都有 WCAG 相对亮度算出的比值。**本轮零改动。**

只补一条**使用规范**（当前靠注释口口相传，无强制）：

| 语义 | 用法 | 禁止 |
|---|---|---|
| `--success` / `--warning` / `--destructive` | 实心填充的背景色 | 用作压浅底的文字色 |
| `-strong` 三件套 | 彩色文字压同色 `/10`~`/20` 浅底 | 用作背景色 |
| `--muted-foreground` | 元数据、次要说明、未选中态 | 用于任何需要被读的**主要**信息 |
| `--warning-foreground` | 仅用于 `bg-warning/10`~`/20` 浅底上的文字（见 `globals.css:102-110`） | 压实心 `bg-warning` |

### 3.2 字体层级（新增，取代当前的 xs/sm 二元对立）

在 `tailwind.config.ts` 的 `fontSize` 里注册语义档位，组件只用语义名：

| Token | 字号 / 行高 | 用途 | 替换当前的 |
|---|---|---|---|
| `text-display` | 30px / 1.2 · serif | 首页问候、品牌 | `text-3xl` |
| `text-title` | 24px / 1.3 · serif | 页面标题（`PageHeader`） | `text-2xl` |
| `text-heading` | 20px / 1.4 · serif | 区块标题、「下一步」、论文题目 | `text-xl` |
| `text-subheading` | 18px / 1.4 · serif | 工作台标题、项目卡题目 | `text-lg` |
| `text-body` | **15px** / 1.6 | **界面默认正文** —— 任务状态、表单值、列表主文本、按钮 | 当前散落的 `text-sm` 与**被误用为正文的 `text-xs`** |
| `text-meta` | 13px / 1.5 | 元数据、时间戳、计数、辅助说明 | 当前正当使用的 `text-xs` |
| `text-micro` | 12px / 1.4 | **下限**：角标、pill 内数字。不得再低 | `text-[11px]`（消除） |

**长文阅读不变**：`--reading-font-size: 17px` / `line-height: 1.75`（`globals.css:77-78`）
与 `.pf-prose` / `.pf-paper` 原样保留。

**Serif 规则不变**：`ui-design.md` §4.3 的「仅 ≥18px 用 serif」继续有效——
上表中 `display/title/heading/subheading` 用 serif，`body/meta/micro` 一律 sans。

**迁移策略**：不做全局 `sed`。按 §5 的分期，逐组件把每个位置**判断其语义**后归位。
判断准则：*"这行字是用户需要读的主要信息，还是它旁边的注解？"* 主要信息 → `text-body`。

### 3.3 间距标度

统一到 4 的倍数，禁用 `space-y-1.5` / `gap-1.5` 之外的奇数值（1.5 保留为唯一半档）：

| 场景 | 值 |
|---|---|
| 行内元素间距（图标↔文字） | `gap-2` (8px) |
| 表单控件内部（label↔input） | `space-y-1.5` (6px) |
| 列表项之间 | `space-y-2` (8px) |
| 区块内部元素 | `space-y-3` (12px) |
| 同级区块之间 | `space-y-6` (24px) |
| 页面主分组之间 | `space-y-10` (40px)（`project-overview.tsx:224` 已是此值，作为基准） |

### 3.4 圆角（收敛为 3 档 + 1 特例）

在 `tailwind.config.ts:63` 扩展：

| Token | 值 | 用途 |
|---|---|---|
| `rounded-sm` | `calc(var(--radius) - 4px)` = 6px | 角标、pill、内嵌小块 |
| `rounded-md` | `calc(var(--radius) - 2px)` = 8px | **按钮、输入框、选择器**（不变） |
| `rounded-lg` | `var(--radius)` = 9.6px | **所有容器**：Card、空态框、对话框、抽屉、首页输入框 |
| `rounded-full` | — | 仅用于圆点与头像 |

**改动**：`ui/card.tsx:8` 的 `rounded-xl` → `rounded-lg`；
`prompt-canvas.tsx:176`、`entry-list.tsx:79`、`app/projects/page.tsx:236,360`、
`export-center.tsx:447` 的 `rounded-xl` → `rounded-lg`；
删除全仓库唯一的 `rounded-2xl`。

### 3.5 按钮（修复 A1）

```ts
size: {
  xs:      'h-8  px-2.5 text-meta gap-1.5',   // 新增：密集工具栏、行内动作
  sm:      'h-9  px-3   text-body',           // 修正：真正比 default 小
  default: 'h-11 px-4   text-body',           // 不变（44px 基线）
  lg:      'h-12 px-6   text-body',           // 修正
  icon:    'h-11 w-11',                       // 不变
  'icon-sm':'h-9 w-9',                        // 新增
}
```

**触控目标规则**：
- 移动端（`<md`）出现的**任何**可点元素，实际高度 ≥ 44px。
- `xs` / `sm` / `icon-sm` **只允许在 `md:` 以上生效**，写法为
  `size="default" className="md:h-8 md:px-2.5 md:text-meta"`，
  或（推荐）在 Button 内提供 `responsive` 开关，由组件统一处理。
- **删除全部 12 处手写 `h-6` / `h-7` / `h-8` 覆写**，改用新 size。

### 3.6 输入框

- 高度对齐按钮标度：默认 `h-11`，`md:` 以上密集场景可 `h-9`。
- 删除 `library-workbench.tsx:403` 的 `h-8 w-48`、`scope-editor.tsx:399` 的 `h-8 text-xs`、
  `cite-key-picker.tsx:57` 的 `h-7`。
- **每个输入必须有可见 label 或 `aria-label`**（当前 `prompt-canvas.tsx:180` 的
  `sr-only` label 是正确示范）。
- 错误提示：`role="alert"`，紧贴输入框下方，`text-meta text-destructive-strong`，
  并在输入框上加 `aria-invalid` 与 `aria-describedby`。

### 3.7 卡片与容器（落实原则 07）

**只有三种情况允许用 `<Card>`**：

1. 它是一个**可点击 / 可选中的对象**（项目卡、视觉卡）；
2. 它承载**独立于页面背景的浮层**（对话框、抽屉、popover）；
3. 它是**列表中的一项**且需要与相邻项在视觉上分离。

**不允许**：把一个语义段落套 Card（用 §3.3 的 `space-y-6` + `SectionTitle` 分层）；
Card 套 Card；用 Card 只为了画一圈边框。

复用 `project-overview.tsx:530` 已有的 `SectionTitle` —— 提升为公共组件
`components/ui/section-title.tsx`（当前是私有函数，`validation-panel` 等无法复用）。

### 3.8 状态标签（统一为一套）

新建 `components/ui/status.tsx`，导出两个组件，**取代当前四套表达**：

| 组件 | 形态 | 用于 |
|---|---|---|
| `<StatusDot state>` | 1.5px 圆点 + 文字，无底色 | 列表内的状态（近期任务、阶段序列、项目进度） |
| `<StatusBadge state>` | 现有 `Badge` 的语义封装 | 需要被一眼抓到的**单个**关键状态（信任摘要、撤稿标记） |

`state` 取值统一：`idle | running | done | degraded | failed | paused`。
文案与颜色映射集中在一处，取代 `project-overview.tsx:627` 的 `jobState`、
`app/projects/page.tsx:46` 的 `progressOf`、`job-progress-card.tsx:236` 的内联 pill。

**规则**：一屏内 `StatusBadge` 不超过 2 个；列表一律用 `StatusDot`
（`project-overview.tsx:594` 的注释已论证过原因，这里把它上升为规范）。

### 3.9 弹窗与提示

- 对话框：`rounded-lg`，宽度 `max-w-md`（确认）/ `max-w-2xl`（表单），
  焦点陷阱（`lib/useFocusTrap.ts` 已有），Esc 关闭，标题用 `text-heading`。
- 破坏性确认：主按钮 `variant="destructive"`，**且描述必须说明后果与可逆性**
  （`app/projects/page.tsx:262` 是正确示范，保留）。
- Toast：保持现有 `aria-live` 分级（`ui/toast.tsx:82`）。
- 行内提示三档：
  - `info` —— `bg-muted/40` + `text-meta`
  - `warning` —— `border-warning/40 bg-warning/10 text-warning-foreground`
  - `error` —— `border-destructive/40 bg-destructive/10 text-destructive-strong`

  当前这三种写法在 6+ 处重复内联（`project-shell.tsx:177,205`、
  `prompt-canvas.tsx:308`、`new-project-wizard.tsx:163` 等）→ 抽成
  `components/ui/callout.tsx`。

### 3.10 响应式断点（明确各断点的语义）

| 断点 | 宽度 | 语义 |
|---|---|---|
| base | <640 | 手机竖屏：单列，侧栏为抽屉，所有触控目标 ≥44px |
| `sm` | ≥640 | 手机横屏 / 小平板：表单可两列 |
| `md` | ≥768 | **侧栏出现**（`sidebar.tsx:155`）。主内容区从此减少 224px |
| `lg` | ≥1024 | 工作台可出现右侧 Inspector 栏 |
| `xl` | ≥1280 | 写作台出现章节树列 |
| `2xl` | ≥1536 | 写作台三栏成立（`writing-workbench.tsx:621`，此判断已实测验证，保留） |

**需补的**：`md` 当前只有 10 处使用，但它是侧栏出现、主内容突然变窄 224px 的临界点。
所有在 `lg` 做两列决策的布局，都要复核 `md`–`lg` 区间（768–1024px，实际内容宽 544–800px）
是否成立。

---

## 4. 分期执行计划

### P0 —— 设计系统地基（1–1.5 天，纯前端）

> 先做这一期，因为后面每一期都要消费这里的 token。

#### P0-1 修复 Button 尺寸标度

- `components/ui/button.tsx`：按 §3.5 重写 `size` variants，新增 `xs` / `icon-sm`。
- 删除全部 12 处高度覆写，改用新 size：
  `job-progress-card.tsx:127,141,153`、`project-shell.tsx:186,193`、
  `scope-editor.tsx:320,399`、`cite-key-picker.tsx:57`、`library-workbench.tsx:403`。
- **任务控制按钮（暂停 / 取消 / 跳过润色）在移动端必须 ≥44px**：
  base 用 `default`，`md:` 以上降为 `xs`。
- 回归：`tests/action-menu.test.tsx`、`tests/job-progress-card.test.tsx`。

#### P0-2 注册字体层级 token

- `tailwind.config.ts`：按 §3.2 注册 7 档 `fontSize`。
- **不做全局替换**，只在本期把 `job-progress-card.tsx` 一个文件迁完，作为样板
  （10 处 `text-xs` → 主状态行 `text-body`、时间/百分比 `text-meta`、
  阶段 pill `text-micro`）。
- 其余文件在 P1–P3 各自的改动中顺带归位。

#### P0-3 圆角与 Callout 收敛

- `tailwind.config.ts` 按 §3.4 补 `sm` 档；`ui/card.tsx:8` 改 `rounded-lg`。
- 全仓库 `rounded-xl` → `rounded-lg`（23 处），删除 `rounded-2xl`（1 处）。
- 新建 `components/ui/callout.tsx`，替换 6+ 处内联提示条。

#### P0-4 公共组件抽取

- `components/ui/section-title.tsx`（从 `project-overview.tsx:530` 提升）。
- `components/ui/status.tsx`（`StatusDot` / `StatusBadge`，§3.8）。
- `project-pipeline-nav.tsx:138` 的 `text-[11px]` → `text-micro`（消除 D2）。

**验收**：全仓库 0 处手写按钮高度覆写；0 处 `text-[11px]`；`rounded-*` 只剩 4 种；
13 个测试文件全绿。

---

### P1 —— 入口与核心输入（2–3 天，纯前端）

#### P1-1 🔴 合并两条建项目路径

**方案：以 Prompt Canvas 为唯一入口，向导降级为它的「完整表单」模式。**

理由：`ui-design.md` 原则 02（Start from intent）已裁定入口从意图开始；
向导的 4 步里有 2 步全是可默认字段；而向导独有的能力（贡献点）
可以放进 Prompt Canvas 的渐进式披露层。

具体：

1. **Prompt Canvas 补齐能力**（`home/prompt-canvas.tsx`）：
   - 「更多设置」折叠区内，当 `paperType === 'original'` 时显示
     `ContributionEditor`（**直接复用** `new-project-wizard.tsx:359` 的组件，
     先抽到 `components/projects/contribution-editor.tsx`）。
   - 题目：保持自动推导，但在折叠区内提供「自定义题目」输入框，
     留空则用 `deriveTitle` 的结果。消除"必填 vs 推导"的分叉。

2. **`/projects` 页的「新建项目」改为跳转首页**（`app/projects/page.tsx:164`）：
   `<Button onClick={openWizard}>` → `<Link href="/">`。
   空态两张引导卡（`:403-414`）改为 `href="/?type=review"` / `?type=original`，
   由 Prompt Canvas 读 searchParam 预设类型。

3. **删除 `components/projects/new-project-wizard.tsx`**（418 行）
   及其在 `app/projects/page.tsx` 的引用（`:27,252-256`）与状态
   （`wizardOpen`、`wizardType`，`:63-64`）。
   保留并移出 `ContributionEditor`。

4. **`Dialog` / `Stepper` 是否还有用**：`Stepper`（`:295`）仅在本文件内使用一次
   （`:160`）→ 一并删除。`Dialog` 在其他 **9 个文件**仍在用
   （import-dialog / diff-preview-dialog / ai-generation-dialog / visual-card /
   outline-editor / library-workbench / assets-center / writing-workbench /
   `app/projects/page.tsx` 的删除确认）→ 保留。

**接口零改动**：仍然只调 `createProject`，请求体字段完全一致
（`lib/types.ts` 的 `CreateProjectRequest` 不动）。

**风险**：`/projects` 上"新建"与"看列表"分离到两个页面。
缓解：`/projects` 页顶部保留一个显眼的「新建论文 →」链接指向首页。

#### P1-2 首页补上「这是什么」与零项目态

`home/prompt-canvas.tsx`：

- 输入框下方一行 `text-meta`（**只在零项目时显示**，有项目后自动隐藏）：
  > 描述你的研究主题，PaperForge 会检索真实文献、生成大纲与正文，产出可投稿的 LaTeX + PDF 初稿。
- `RecentPapers` 零项目时不再 `return null`（`:417`），改为渲染一个
  **两条管线的说明块**（复用 `app/projects/page.tsx:393` 的 `FirstRunState` 文案，
  但改为纯文字 + 留白，不用 `StarterCard` 的边框卡）。
- `pt-[12vh]` → `pt-8 md:pt-[12vh]`（修 C5）。

#### P1-3 `validation-panel.tsx` 去 Card 化

11 张 Card → 0 张。改用 `SectionTitle` + `space-y-6` + 分隔线（对齐 `project-overview` 的语言）。
19 处 `text-xs` 按 §3.2 归位。
19rem 栏位内因此多出约 44px 的可用横向空间。

回归：`tests/writing-workbench.test.tsx`。

**验收**：只有一条建项目路径；首页零项目态能说清产品；写作台右栏 0 Card。

---

### P2 —— 状态、失败与产物关系（2–3 天，纯前端）

#### P2-1 🔴 让失败与重试在任务结束后仍然可达

1. **「近期任务」行可展开**（`project-overview.tsx:595-624`）：
   每行改为可点击，展开后显示该任务的 `error.warnings` 明细，
   并对 `RETRYABLE` 集合内的阶段给出「重跑」按钮。
   - 复用 `project-shell.tsx:89` 的 `retryStage`——**将其从 `ShellBody` 提升到
     `ProjectContext`**（`project/project-context.tsx`），使概览页也能调用。
     这是本期唯一的结构性重构，约 20 行。
   - 复用 `job-progress-card.tsx:52` 的 `RetryableStage` 类型与 `RETRYABLE` 集合
     （移到 `lib/pipeline.ts`，两处共用）。

2. **失败任务给出「下一步」**：失败行展开后除「重跑」外，
   给一句可执行的建议（如检索失败 → 「或改用其他检索源」链接到文献工作台的 ProviderFilter）。

3. **`aria-live` 补齐**（D4）：批量入库/排除（`library-workbench.tsx:195`）、
   导出触发、重跑触发，全部通过已有的 `toast`（`ui/toast.tsx:82` 已带 `aria-live`）播报——
   多数已经在调 toast，只需补齐遗漏处。

#### P2-2 产物关系视图

在 `project-overview.tsx` 的「稿件」区块（`:372`）内补一条**产物关系行**，
把当前分散的四条信息串成一句可读的话：

```
稿件                                              32,481 字
文稿 v3 · 46 篇文献（128 处引用）· 5 张图 · 3 个导出产物
最新 PDF 生成于 2 小时前，含全部 5 张图
```

数据全部来自**已有的**调用（`getVersionHistory` / `getCitationAudit` /
`progress.visuals` / `listExports`，`:101-118` 已经全拉了），
只是当前分散在四个区块（`:372`、`:427-433`）。**零新增请求。**

「最新 PDF 是否含全部图」的判断逻辑 `export-center.tsx:296` 的 `outdated` 已经实现，
抽到 `lib/` 复用。

#### P2-3 文献工作台与导出中心精修

- 删除 `library-workbench.tsx:483-492` 的静态「引用真实性」说明卡（C1），
  移到右栏底部一行 `text-meta` 的「了解引用真实性规则 →」链接（指向文档）。
- `export-center.tsx` 三处装饰性 Card（`:246,353,420`）去掉（C4），
  改用 `SectionTitle` + `divide-y`。
- 写作台：移除永久禁用的「重写本章」（`writing-workbench.tsx:651-658`），
  把能力边界写进「重新生成全文」确认框的描述（C2）。
- 写作台头部动作精简（C3）：`保存状态` + `保存本节` + `更多 ▾`（放弃草稿 / 重新生成全文）。

**验收**：任务失败后重跑入口始终可达；概览页一句话说清产物关系；
写作台无永久禁用按钮。

---

### P3 —— 移动端与收尾（1.5–2 天，纯前端）

#### P3-1 移动端布局修复

| 项 | 改动 |
|---|---|
| E1 Pipeline 导航 | 当前步骤 `scrollIntoView({ block:'nearest', inline:'center' })`；两侧加 `mask-image` 渐变提示可滚动 |
| E2 写作台 | `<xl` 时章节树改为顶部的**折叠式**章节选择器（当前节名 + 下拉），正文直接占首屏 |
| E3 文献列表 | `max-h-[calc(100vh-18rem)]` → 用 `ResizeObserver` 实测筛选区高度（组件内已有 ResizeObserver，`entry-list.tsx:61`），或改为 `md:` 以上才限高 |
| E4 文献筛选区 | `<md` 时收成「筛选 ▾」单按钮 + 抽屉；批量操作条固定在列表底部 |
| E5 `WorkbenchHeader` | 新增 `overflowActions` 参数：`<md` 时次要动作收进 `ActionMenu`（`ui/action-menu.tsx` 已有），只留主 CTA |
| E6 `md` 断点复核 | 逐页在 768–1024px 检查（见 §6 清单） |

#### P3-2 剩余去 Card 与字号归位

`settings-page.tsx`（5）、`scope-editor.tsx`（4）、`outline-editor.tsx`（4）、
`search-stats.tsx`（2）、`markdown-preview.tsx`（2）、`provider-filter.tsx`（1）、
`assets-center.tsx`（1）。

目标终态：**根 `<Card>` 从 43 降至 ≤12**，且全部符合 §3.7 的三条准入条件
（项目卡 1、视觉卡 1、5 个 auth 页各 1、对话框/抽屉内部若干）。

同步把各文件的 `text-xs` 按 §3.2 归位。

#### P3-3 空态 / 加载态 / 错误态统一

当前空态有 4 种写法（`entry-list.tsx:79`、`app/projects/page.tsx:236,395`、
`export-center.tsx:236`、`visuals-workbench.tsx:404`）。

抽成 `components/ui/empty-state.tsx`，统一结构：
**图标（可选）→ 一句话说明现状 → 一句话说明下一步 → 一个主动作按钮**。

- 加载态：统一用 `Skeleton`，且**骨架形状要贴近真实内容**
  （当前 `app/projects/page.tsx:183` 的 `h-40` 方块 vs 真实项目卡结构差异较大）。
- 错误态：统一走 `layout/load-state.tsx` 与 `layout/module-error.tsx`（已有且设计合理），
  确保每个错误都带**重试按钮**和**人话描述**（`lib/errors.ts` 的 `describeError` 已具备）。

---

## 5. 需要删除或合并的重复 / 废弃 / 冲突项

| # | 项 | 处置 | 期 |
|---|---|---|---|
| 1 | 12 处手写按钮高度覆写（`h-6`/`h-7`/`h-8`） | 删除，改用新 size | P0 |
| 2 | `text-[11px]`（`pipeline-nav.tsx:138`） | → `text-micro` | P0 |
| 3 | `rounded-2xl`（全仓库 1 处孤例） | 删除 | P0 |
| 4 | 6+ 处内联提示条样式 | → `ui/callout.tsx` | P0 |
| 5 | 3 套状态映射函数（`jobState` / `progressOf` / 内联 pill） | → `ui/status.tsx` | P0 |
| 6 | `new-project-wizard.tsx` 全文（418 行）+ `Stepper` | **删除**，`ContributionEditor` 移出保留 | P1 |
| 7 | `app/projects/page.tsx` 的 `wizardOpen` / `wizardType` 状态与 `StarterCard` | 删除 / 改链接 | P1 |
| 8 | `library-workbench.tsx:483-492` 静态 R1/R2/R3 说明卡 | 删除，改链接 | P2 |
| 9 | `writing-workbench.tsx:651-658` 永久禁用的「重写本章」 | 删除，说明移入确认框 | P2 |
| 10 | `export-center.tsx` 三处装饰性 Card | 删除 | P2 |
| 11 | 31 张不符合 §3.7 准入条件的 `<Card>` | 逐个替换为排版分层 | P1–P3 |
| 12 | 4 种空态写法 | → `ui/empty-state.tsx` | P3 |
| 13 | `apps/web/_tmp_*`、`_probe_ovr.txt` 等残留临时文件 | 删除（与 UI 无关，顺手清理） | P3 |

**冲突项复核（确认无需处理）**：`globals.css` 与 `tailwind.config.ts` 中的
`--font-serif` 映射**已被使用**——全仓库 `font-serif` 出现 **15 次、分布 10 个文件**
（`sidebar.tsx:91,172`、`page-header.tsx:16`、`prompt-canvas.tsx:157,160,439`、
`project-overview.tsx:239`、`workbench-header.tsx:25`、`app/projects/page.tsx:318`、
`app-shell.tsx:42` 等）。`ui-design.md` §3.3 记录的"全仓库 `.tsx` 里零引用"
在第一轮已修复，本轮无需再处理。

---

## 6. 桌面端与移动端检查清单

每一期结束后按此表逐页走一遍。

### 6.1 桌面端（1440px / 1280px / 1024px）

| 页面 | 检查点 |
|---|---|
| 首页 | 输入框居中不偏；零项目态有产品说明；「更多设置」展开后不遮挡「开始」 |
| `/projects` | 卡片网格 3 列（`lg`）→ 2 列（`sm`）；回收站折叠正常；「新建」跳转首页 |
| 项目概览 | 「下一步」是唯一带边框块；产物关系行不换行溢出；成本/版本两列在 1024px 成立 |
| 文献 | 右栏 20rem 在 1024px 不挤压主列表；虚拟列表滚动无露白；批量条常驻 |
| 大纲 | 拖拽热区 ≥44px；去 Card 后层级仍清晰 |
| 写作台 | 三栏仅在 ≥1536px；正文 72ch 限宽；焦点模式隐藏两侧栏；右栏无 Card |
| 视觉 | 卡片网格对齐；生成中状态有动效反馈 |
| 导出 | PDF iframe 高度合理；产物行 `divide-y` 对齐；历史运行可折叠 |
| 设置 | 表单两列在 `md` 成立；保存有 toast 反馈 |

### 6.2 移动端（390px / 430px / 768px）

| 页面 | 检查点 |
|---|---|
| 全局 | 顶栏 56px 不遮内容（`app-shell.tsx:27` 的 `pt-14`）；抽屉可 Esc / 点遮罩关闭；**无横向滚动条** |
| 全局 | **所有可点元素 ≥44px**（重点：任务条的暂停/取消/跳过润色） |
| 首页 | `pt-8` 后输入框在首屏内；文件 chip 换行不溢出；类型下拉 `bottom-full` 定位不出屏 |
| 项目概览 | 「下一步」按钮不与标题挤在一行；`md:grid-cols-2` 在 768px 复核 |
| Pipeline 导航 | 当前步自动滚入视野；边缘渐变提示；每个步骤热区 ≥44px |
| 文献 | 筛选收进抽屉；列表可视高度 ≥50vh；行高 92px 内三行文字不截断 |
| 写作台 | 章节树折叠为下拉；正文占首屏；校验抽屉可达；工具行不换行成 3 行 |
| 导出 | PDF 预览改为「下载 / 新窗口打开」（移动端 iframe 内嵌 PDF 体验差） |
| 对话框 | 全屏或近全屏；底部按钮不被虚拟键盘遮挡 |

---

## 7. 验收标准与回归保障

### 7.1 对照本次任务的验收标准

| 验收标准 | 由哪些改动兑现 |
|---|---|
| 首页数秒内理解用途和主要入口 | **P1-2** |
| 新建研究任务路径清晰、主次输入明确 | **P1-1**（单一路径）+ §3.2 字号层级 |
| 任务运行状态和下一步操作始终可见 | **P0-2**（状态字号）+ **P2-1**（失败后仍可重跑） |
| 文献 / 引用 / 图表 / 产物关系清楚 | **P2-2**（产物关系行）+ 已有的 citation-usage |
| 风格统一，无错位/溢出/重复按钮/无反馈操作 | **P0**（token 收敛）+ **P2-3**（删除永久禁用按钮）+ **P3**（移动端 + 空/加载/错误态） |
| 不破坏任何现有功能和接口 | 见 §7.2 |

### 7.2 回归保障

**硬约束**：

1. **零后端改动。** 不修改 `services/`、`packages/`，不新增/修改任何 API 调用。
   `lib/api.ts`（1102 行）与 `lib/types.ts`（842 行）**只读**。
   唯一例外是 P2-1 把 `retryStage` 从 `ShellBody` 提升到 `ProjectContext`——
   这是组件间的函数搬家，调用的端点集合完全不变。
2. **现有 13 个测试文件必须全绿**，且不通过修改测试来适配：
   `assets-center` / `project-overview` / `job-progress-card` / `use-visuals` /
   `errors` / `section-positions` / `visuals-workbench` / `publication-metadata` /
   `writing-workbench` / `action-menu` / `pipeline` / `figure-numbering` / `helpers`。
   > 环境提示：本机不能直接跑 `pnpm test`，需按 `MEMORY.md` 的「Running the test suites」
   > 用一次性容器执行。
3. **删除 `new-project-wizard.tsx` 前**，确认无测试引用它
   （已核：13 个测试文件中无引用）。
4. **不改任何色值**（§3.1）。`globals.css` 的对比度注释是实测结果，
   本轮所有改动都不触及色值定义。

**新增测试建议**（P0 结束时补）：

- `tests/button-sizes.test.tsx`：断言各 size 的高度类名，防止标度再次塌陷。
- `tests/status.test.tsx`：断言 6 个 state 的文案与语义色映射。

---

## 8. 本轮明确不做

| 不做 | 理由 |
|---|---|
| 改任何颜色值 | `globals.css` 的比值是实测算出来的，当前全部达标（§3.1） |
| 动 `ui-design.md` §2 的八项「已成事实」 | 本轮复核确认全部仍成立且已落实 |
| 把 Pipeline 导航换成一行状态 | `ui-design.md` §4.1 已裁决；按 `writing_mode` 分叉的现方案正确 |
| 首页改回 Dashboard | `ui-design.md` §4.2 已裁决 |
| 全面 serif | `ui-design.md` §4.3 已裁决；≥18px 阈值继续有效 |
| 引入 UI 组件库 / 动画库 / 虚拟列表库 | 现有手写实现（tabs 的 ARIA、entry-list 的虚拟化、useFocusTrap）质量高且无依赖，替换是纯损失 |
| 重构 `lib/api.ts` / `lib/types.ts` | 超出 UI/UX 范围 |
| 改写作台三栏断点（2xl） | `writing-workbench.tsx:614-620` 的注释记录了实测数据，判断正确 |

---

## 9. 工作量汇总

| 期 | 内容 | 预估 | 依赖 |
|---|---|---|---|
| **P0** | 设计 token 地基：按钮标度、字号层级、圆角、公共组件 | 1–1.5 天 | 无 |
| **P1** | 建项目路径合并、首页说明、右栏去 Card | 2–3 天 | P0 |
| **P2** | 失败可重试、产物关系、页面精修 | 2–3 天 | P0 |
| **P3** | 移动端、剩余去 Card、空/加载/错误态 | 1.5–2 天 | P0–P2 |

合计 **7–9.5 天**，全部纯前端。P1 与 P2 在 P0 完成后可并行。

---

## 附录：审计数据

采集于 2026-07-29，`apps/web`（排除 `node_modules` / `.next`）：

```
TS/TSX 总行数            18,713
根 <Card> 实例              43   （Card 家族标签 157，分布 17 文件）
text-xs                    225
text-sm                    149
text-base                    6
text-lg / xl / 2xl / 3xl  11 / 7 / 4 / 1
rounded-md/lg/full/xl/2xl  75 / 31 / 29 / 23 / 1
size="sm" 调用              50
手写按钮高度覆写             12
aria-live                    5
断点 sm/lg/md/2xl/xl       42 / 42 / 10 / 9 / 3
```

最大的五个组件：`writing-workbench` 1113、`publication-metadata` 724、
`project-overview` 722、`export-center` 654、`outline-editor` 605。
