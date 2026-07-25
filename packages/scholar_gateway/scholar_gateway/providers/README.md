# scholar_gateway/providers — 五源检索适配器

对应设计方案 §3.1 与 §9 附录。DeepSearch 的 `literature_review/adapters.py`（1961 行，五源合一）
已**按 provider 拆分**迁移完成；`snowball.py` / `oa_fulltext.py` / `http_cache.py` /
`runtime.py` / `acquisition/http_client.py` 一并迁入本包。

## 迁移结果（M0 收尾完成）
| 目标 | 源（DeepSearch） | 迁移要点 |
|---|---|---|
| `crossref.py` | `adapters.py`（Crossref 部分） | 去 occurrence 落账；polite pool mailto 走 config |
| `openalex.py` | `adapters.py`（OpenAlex 部分） | mailto/api_key 走 config；日预算耗尽熔断；撤稿标记入库 |
| `semantic_scholar.py` | `adapters.py`（S2 部分） | API key 走 config；按凭据档位 pacing；匿名严格单飞 |
| `arxiv.py` | `adapters.py`（arXiv 部分） | Atom 路径复用共享缓存/重试 GET |
| `europepmc.py` | `adapters.py`（EuropePMC 部分） | 时间窗以 FIRST_PDATE 子句入查询串 |
| `base.py` | `adapters.py` 的 `_HttpScholarlyDiscoveryAdapter` | 缓存优先 → 限速 → 重试/退避 → 配额诊断 |
| `mapping.py` | `adapters.py` 的 `_candidate_from_*` 族 | 字段名对齐 db 模型；OA pdf 链接标 `is_oa` |
| `query_syntax.py` | `adapters.py` 的查询构造段 | OpenAlex 字段前缀/年份管道净化、arXiv 前缀透传 |
| `../runtime.py` | `literature_review/runtime.py` + adapters 限速段 | pacer / 熔断器 / 配额诊断；构造注入取代全局 settings |
| `../snowball.py` | `literature_review/snowball.py` | 种子解耦为 `SnowballSeed`；缓存句柄注入 |
| `../fulltext.py` | `literature_review/oa_fulltext.py` | 解耦 ledger 写入链，只产出规划 + 字节 |
| `../cache.py` | `literature_review/http_cache.py` + `scholarly_http_cache` 表 | 抽象为 `HttpCacheBackend`，保留表结构 |
| `../http.py` | `acquisition/http_client.py` | 收敛：SSRF + 逐跳重定向校验 + 大小上限 + 礼貌 UA |

## 用法
```python
import httpx
from scholar_gateway import InMemoryHttpCache, ScholarlyDiscoveryQuery, build_adapter
from scholar_gateway.providers import ProviderConfig

adapter = build_adapter(
    "openalex",
    client=httpx.Client(),
    config=ProviderConfig(contact_email="you@example.com"),
    cache=InMemoryHttpCache(),
)
result = adapter.discover(ScholarlyDiscoveryQuery(query_text="retrieval augmented generation"))
```

适配器是**同步**实现（拷贝式迁移，最大化复用久经考验的限速/熔断/查询净化逻辑）；
worker 侧以 `asyncio.to_thread` 调用，避免 async/sync 撕裂。

## 契约
- 任何 provider 失败都返回带 `errors` 的 `ScholarlyDiscoveryResult`，绝不抛出（draft-first）。
- 凭据只在发请求时合并，绝不进缓存键、诊断或事件负载。
- 熔断打开期间一个请求都不发（礼貌性机制，非绕过）。
