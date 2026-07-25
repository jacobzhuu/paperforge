# latex_render — 模板体系与编译（M3 工作）

对应设计方案 §4.6。M0 仅落 body 渲染器 + 转义；以下为后续。

## 模板库 V1
IEEEtran、acmart、Elsevier(elsarticle)、Springer(llncs)、通用中文学位论文/学报模板、无格式 article。
模板 = 主 `.tex` 骨架 + 环境白名单 + 宏包锁定清单；**LLM 不可修改导言区**。
放在 `latex_render/templates/<name>/main.tex.j2`。

texd 镜像构建阶段会预热 `article`、`IEEEtran`、`acmart`、`llncs` 四个当前承诺的
document class；任何宏包缺失会直接令镜像构建失败，避免断网运行时才暴露。

## 渲染管线
`paper_ir → jinja2 模板 → LaTeX 工程(main.tex + sections/ + figures/ + refs.bib) → texd(Tectonic) 编译 → PDF + 日志`
- refs.bib 由 `paper_ir.bibtex.render_bibtex` 从库内元数据确定性生成（R3）。

## 编译失败自动修复（有界 ≤2 轮）
1. 确定性修复优先：转义特殊字符、去未定义环境、降级缺失宏包；
2. 其后才允许 LLM 针对报错行做最小修补；
3. 仍失败 → 交付 LaTeX 工程 + Markdown 预览 + 日志（draft-first，绝不阻断）。

## 环境白名单
LLM 生成的自由 LaTeX 仅允许出现在 equation/algorithm 块，且过白名单环境校验（防注入与编译失败）。

## 次要导出
pandoc → docx（国内投稿）、Markdown、纯 BibTeX。
