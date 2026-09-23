# 交互式科研分析 Agent（research-v1）

基于 2026-09-23 工作区增量实现。保留 ARQ、项目授权、执行租约、PaperIR、数字来源、既有修复图和正式质量门。没有将 Jev 接成最终裁判，也没有改成通用多 Agent 平台。

## 用户流程

原创论文的素材中心提供实验数据分析。上传 CSV/TSV/XLSX，输入研究目标，选择仅分析或目标章节。任务先读取原文件并暂停，用户选择工作表、数值字段、分组和每字段单位后继续。支持均值、中位数、样本标准差、范围、样本数和缺失数，不执行显著性检验或推断因果。

结果包括表、图和复现包（原始文件、固定计算实现、参数、依赖版本与结果）。修改提案只向指定章节追加确定性的统计说明、表和图；不让模型重写现有实验数字。确认后创建新文档版本，拒绝不修改正文。需要自由文本局部重写时仍使用现有写作工具；此版本没有宣称已支持任意统计分析代码。

## 组件与事实来源

- `ResearchAnalysis` 保存输入哈希、累计模型回合、累计执行时间、分析结果和修改提案；正式文稿仍在 PaperDocument/PaperSection。
- LangGraph `research-v1` 保存 inspect/clarify/execute 的执行位置，使用现有独立 PostgreSQL checkpoint schema。等待用户时释放 worker；独立响应接口重新入队且沿用 thread ID。
- Pi Agent Core 0.87.1 在无凭据 Node 子进程内执行工具循环，采用本项目私有 JSONL 桥接协议，不冒充 Coding Agent CLI 的 RPC。只有 read_table_summary、compute_descriptive、read_results、propose_patch 四种工具，无 shell、用户扩展或文件编辑工具。
- 模型网关增加独立 ToolChatProvider，把多轮消息和工具声明原生发送给现有 OpenAI-compatible provider。现有 LLMRunner 承担限流、预算预留、取消与费用账本；模型固定到创建任务时的 planner 配置，未知价格拒绝付费启动。
- 固定 Python 计算器运行在独立进程中。Linux Landlock 限制读取到运行库、写入到临时目录；seccomp 拒绝网络、进程执行与文件截断，补足宿主 Landlock ABI 1 的缺口。CPU 45 秒、地址空间 2 GiB、父进程超时 60 秒。环境中不传生产密钥，不执行上传代码或工作簿公式。依赖隔离不可用时失败关闭。
- 派生素材保存原素材 ID、原始字节哈希、分析方案、计算版本与结果哈希。表格数字由代码产生，接入现有 numlint 与导出；图用 Matplotlib 生成。

## 接口与并发

新增项目级 research-runs（创建/列表）、research-runs/{job}/responses（澄清）、research-runs/{job}/resume（暂停/失败后续跑）、analysis-results/{id}（结果）、chart、reproducibility、artifact-proposals/{id}/decision。

任务复用 generation_job/job_dispatch，不新增第二套队列。提交先获取项目与文稿锁，检查文档 ID、版本对应的正文哈希、源文件哈希、派生产物及引用白名单。重复确认返回同一文档；陈旧提案返回 409。新版本不继承旧质量结论，仍需正常核验与导出。

一条分析最多 20 个模型回合、10 分钟累计执行时间，恢复不清零。模型底层重试仍受共享预算控制。确定性产物使用稳定 ID/对象键，恢复可复用结果；外部付费请求不能承诺 exactly-once。

## 核验与 Jev

软引用 verifier v3 检查缺失、重复、越界索引与非法分数。只补查未完成条目一次，仍缺失标记 unverified；UI 不将其显示为弱相关或已通过。正式 claim-entailment gate 不变。

生产 shadow 在 baseline 完成后冻结输入与结果到 citation_shadow outbox。worker 每分钟最多消费一项，仅在没有 queued/running 任务时执行，并使用 shadow 配额优先级。一天前未处理的任务过期，执行不明确的任务记 interrupted，不以补跑掩盖未知费用。Jev 不再位于该软检查的前台等待路径。旧离线校准保留，v3 对照必须重新冻结数据；不沿用旧阈值资格。

## 运行与验收

迁移 0035 仅添加研究分析与 citation shadow 表。Pi/npm、Python 依赖和 Node 镜像固定版本。部署仍运行 scripts/dev restart，并要求 Funnel 目标和公网 /login 字节一致；不得停止持有在途任务的旧部署。

自动测试覆盖真实隔离计算、统计边界、多工作表/公式拒绝、Pi 进程工具循环、原生多轮模型接口、跨项目授权、人工输入恢复、单次版本提交、冲突拒绝、核验补查和异步 shadow。模型替身测试不代表真实供应商能力或科研质量提升。最终执行结果另见本次验收记录。
