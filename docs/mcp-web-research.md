# MCP 网页研究辅助

PaperForge 是受控 MCP Host：Coordinator 通过官方 Python MCP SDK 连接
`https://mcp.exa.ai/mcp`，仅调用 `web_search_exa` 与 `web_fetch_exa`。
不提供 MCP Server，不向 Writer 开放工具，也不启动外部子进程。

在项目的文献页启用「补充网页检索」，会向 Exa 发送主题及研究问题。
之后文献准备流程自动补充网页资料，也可手动刷新。默认每个项目关闭。
网页快照只用于研究辅助；明确 DOI/arXiv 链接须经现有权威来源核验，
才能作为候选论文入库。网页正文不会进入 Evidence 或 Writer。

## 运维

配置见 `.env.production.example` 的 `MCP_*` 项及 `EXA_API_KEY`。
密钥只通过请求头传输，缺省使用匿名限额，不会自动启动 OAuth。
默认每次运行最多 10 次工具调用（含重试），每次 30 秒，MCP 阶段自运行
创建起最多 180 秒；暂停不会重置该时限。论文反查另有 120 秒上限。
每日额度按 UTC，在 PostgreSQL 中原子预留：每用户 100 次、全站 1000 次。
这些是 PaperForge 发出的 MCP 调用上限，不包含 Exa 内部重试，也不等同于
美元费用；界面明确显示「费用未计价」。

每次运行独立连接，固定端点，不允许配置任意主机。
连接使用独立直连 TLS，不继承部署中供 LLM 使用的 `HTTPS_PROXY` 中继。
工具 schema 及策略版本随运行固定，恢复遇到变化时停止补充。
服务返回文本是非可信数据，前端转义
展示。远端网页抓取由 Exa 执行，本地 URL/DNS 检查不是远端跳转控制的保证。

`web_research_run`、`web_research_source` 保存项目私有资料；
`mcp_tool_invocation` 记录预留、结果、trace 及未知执行状态。
`mcp_daily_budget` 跨部署共享，删除项目不会返还当日预算。
恢复继承原运行，复用成功调用；不明确的调用不退还预算。
故障只降级网页阶段；用户停止及执行租约丢失继续传播。

## 验收与回滚

测试需要单独的 PostgreSQL 测试库（`PAPERFORGE_TEST_DATABASE_URL`），
pytest 会重建测试 schema，绝不可设置成正式库。API 与 Worker 测试分别运行，
避免仓库既有同名 `conftest` 导入冲突。

上线执行 `./scripts/dev restart`，必须通过 Funnel 目标与公开 `/login`
内容一致性检查，随后在授权项目验证真实检索、来源展示和任务记录。
回滚可设置 `MCP_WEB_ENABLED=false` 并滚动部署；已有资料仍可读取。
保留旧部署的在途任务，不回退数据库新增表。

## 2026-09-17 验收记录

- 官方 SDK 固定为 `mcp==2.2.0`；按线上真实 schema 支持搜索的 `objective` 参数。
- API 回归 98 项、MCP runtime 18 项、网页研究 Worker 9 项、相关前端 22 项通过。
  数据库测试使用独立 PostgreSQL，未跳过；迁移升级、回退、再升级通过。
- 生产部署 `paperforge-deploy-20260917-151410`，Funnel 指向 3005；公开 `/login`
  与新部署字节一致。旧在途部署保留。
- 公开浏览器实际启用项目、刷新并读取资料：6 次调用，5 个网页，5 个正文片段；
  无浏览器错误，390px 移动视口无横向溢出。
- 第二次真实研究运行：6 次调用、5 个网页，发现并通过权威来源反查核验 1 篇论文。
- 首次部署验收发现 LLM 专用代理不适用于 MCP 并发连接；已隔离代理并重新部署验证。
