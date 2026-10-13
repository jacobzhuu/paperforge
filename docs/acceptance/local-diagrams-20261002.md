# 本地框图替代外部生图（2026-10-02）

## 行为与兼容

- 开发分支：`feat/glm-5.3-flash-migration`；开发基线 HEAD：`cf21fc6`。保留此前未提交的后台修稿恢复改动。
- 全流程摘要图改为一次文字模型规划，输出一张 `DiagramSpec`，使用 visuald / Graphviz 本地渲染。结构包含节点、分组和带标签的连线；输出 SVG、PNG、PDF。
- 自动摘要不再使用 Yunwu，不再执行图片提示词分析、外部生成和图片合规重试。正文模型与检索配置保持原样。
- 默认及本次部署的 `AI_IMAGES_ENABLED=false`。新建入口展示框架／流程图和数据图表；旧客户端请求 `ai_image` 草稿时转为框图。历史图片、审核和导出仍兼容，外部重新生成返回明确的 409 原因。
- 自动摘要要求 3–9 个节点、有效连线和真实章节键。非法结构、无依据建议、超时或模型失败时，保留具体任务提示并继续正文导出；不创建通用目录占位图。任务停止信号继续向上传播。
- 本地渲染失败保留资产的 failed 状态及原因；已就绪资产不会重复渲染。
- 部署检查实际渲染中文分组框图并校验三种格式与预检结果；关闭外部生图时不探测图像供应商。对象存储、TeX、Funnel 目标及公网内容比较仍为必要检查。

## 验证边界

测试使用隔离 PostgreSQL、模拟文字模型和本地对象存储，没有调用付费模型或运行完整论文生成。

排版样例取自仓库 `services/worker/tests/snapshots/section_zh/expected.md` 中的投毒路径与检测边界，人工整理为结构输入，没有添加数值。该来源是测试快照，不是独立核验的学术证据。本次验证的是结构保留、中文标签、箭头和分组的渲染及导出能力，不能据此声称真实模型的论文学术质量有所提升。

样例位于 `.paperforge/acceptance/local-diagrams-20261002/`：`sample.json`、`sample.svg`、`sample.png`、`sample.pdf`、`qa.json`。已目视检查 PNG：节点与关系标签完整，无明显重叠或裁切。预检通过，PNG 为 3048×815，最小边距 75 px；实际长标签与复杂论文的模型规划质量仍需后续真实使用评估。

## 检查结果

- 视觉规划、本地渲染服务与导出单元检查：43 passed，2 skipped（宿主机没有 Graphviz；真实 Graphviz 渲染另由容器检查执行）。
- 最终框图规划与数据库集成：24 passed，含落库、渲染失败保留、无效建议提示和已就绪资产不重复渲染。
- Worker 管线回归：此前同批检查中的 39 项通过；一项集成失败来自测试上下文缺少 owner_id，补齐后上述集成检查通过。
- 前端视觉工作台：27 passed；TypeScript、修改文件 Ruff、Shell 语法及 diff 空白检查通过。

- API 视觉与历史图像接口：19 passed，61 deselected。
- 此前后台修稿恢复回归：28 passed。
- 隔离测试数据库容器已移除。

## 公网发布完成

`./scripts/dev restart` 成功发布 `paperforge-deploy-20261002-182122`，本地端口 3005，独立 Redis DB 1。API 和 worker 的实际 `ai_images_enabled` 均为 False；新实例中的视觉规划及修稿恢复模块 SHA-256 与本地文件一致。

部署验收全部通过：TeX 离线 longtable、本地对象存储往返、中文分组框图 SVG/PNG/PDF 渲染与预检。日志明确确认关闭外部生图且未请求图像供应商。

Funnel 已指向 3005；公网 `https://paperforge-linux.tail53ab99.ts.net/login` 与新实例 `/login` 逐字节一致，SHA-256：`70212dbc787d03178694181e436a49efb3910cc5ba29f162ae6cd3c712270ae8`。

部署前因连接预算不足，使用仓库现有 `reap --apply`，只回收经两次队列/执行状态核实为空的四个非公网部署；当前旧公网实例 `paperforge-deploy-20260928-174108` 保留，允许切换前任务完成。未停止任何已确认有执行中任务的部署。

完整日志：`/tmp/paperforge-diagrams-deploy.log`。本次发布同时包含此前修稿恢复改动；代码尚未新增提交或推送。
