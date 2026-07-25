# ingest — 抽取器迁移结果

对应设计方案 §3.1（parsing 复用）与 §9 附录。**M0 收尾已完成**。

| 目标 | 源（DeepSearch） | 迁移要点 |
|---|---|---|
| `document_extractors.py` | `parsing/document_extractors.py` | ✅ 零改动拷贝（仅 ParsedContent 导入指向 `ingest.types`）；PDF/DOCX/PPTX/XLSX，标准库实现，不执行宏 |
| `chunking.py` | `parsing/chunking.py` | ✅ 零改动拷贝：段落窗口 v1 稳定切块 |
| `quality.py` | `parsing/quality.py`（916 行） | ✅ 改造：保留 chunk 侧信息密度/样板/参考文献段识别，去掉域权威度、crawlability、vendor 域名等 OSINT 专属信号 |
| `text_extract.py` | `parsing/extractors.py`（HTML 段） | ✅ 收敛：剥 script/style、块级断段、取 `<title>`；去掉发布时间/作者元数据族 |
| `types.py` | `parsing/extractors.py` 的 `ParsedContent` | ✅ 迁入 |
| `extract.py` | 新写 | mime 分派 + `try_extract_and_chunk` 的 draft-first 包装 |
| `section_chunks.py` | `literature_review/section_chunks.py` | ✅ 纯函数，零改动 |

V2 评估 GROBID/marker 升级 PDF 结构化质量（方案 §3.1 备注）。

## 用途
- 综述管线 INGEST：OA 全文获取 → 解析 → section 感知切块，供 CARDS 抽取 literature_card。
- 研究型管线 INPUT：CSV/XLSX/图/笔记确定性解析入 `user_asset.parsed_json`，
  是「正文数字只能来自素材」这条红线的事实来源。

## 契约
- `extract_content` 对不支持的 mime 抛 `UnsupportedMimeTypeError`；
  `try_extract_and_chunk` 永不抛出，失败降级为「无全文」（draft-first）。
- `mime_policy_metadata()` 把「不执行宏 / 不加载外部资源 / 不展开嵌入对象」写进产物元数据。
