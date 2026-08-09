"""PaperForge 学术检索网关。

迁移自 DeepSearch literature_review 的确定性资产（设计 §3.1 / §9）：
标识符规范化、确定性去重、多源检索适配器（含限速/熔断/缓存）、引文雪球扩展、
OA 全文获取、合规 HTTP 客户端。
"""

from scholar_gateway.cache import (
    CachedHttpResponse,
    HttpCacheBackend,
    InMemoryHttpCache,
    NullHttpCache,
    SqlAlchemyHttpCache,
    canonical_request_url,
)
from scholar_gateway.dedupe import (
    DedupeCluster,
    DedupeConflict,
    DedupeResult,
    dedupe_scholarly_candidates,
)
from scholar_gateway.fulltext import (
    OaFulltextCandidate,
    OaFulltextDocument,
    OaFulltextPlan,
    OaFulltextTarget,
    acquire_oa_fulltext,
    discover_doaj_links,
    discover_unpaywall_links,
    plan_oa_fulltext,
)
from scholar_gateway.http import HttpFetchResult, SafeHttpClient
from scholar_gateway.models import (
    ScholarlyAuthorCandidate,
    ScholarlyDiscoveryAdapter,
    ScholarlyDiscoveryError,
    ScholarlyDiscoveryQuery,
    ScholarlyDiscoveryResult,
    ScholarlyIdentifier,
    ScholarlyLinkCandidate,
    ScholarlyWorkCandidate,
)
from scholar_gateway.normalize import (
    normalize_arxiv_id,
    normalize_corpus_id,
    normalize_doi,
    normalize_openalex_id,
    normalize_pmcid,
    normalize_pmid,
    normalize_semantic_scholar_id,
    normalize_title_for_dedupe,
    normalized_title_hash,
    token_set_jaccard,
)
from scholar_gateway.providers import (
    ALL_PROVIDERS,
    DEFAULT_PROVIDER_ORDER,
    ProviderConfig,
    build_adapter,
)
from scholar_gateway.runtime import (
    ProviderCircuitOpenError,
    provider_circuit_snapshot,
    reset_provider_rate_limit_state,
)
from scholar_gateway.snowball import (
    SnowballExpansionResult,
    SnowballSeed,
    expand_citation_snowball,
    rank_by_citation_influence,
    select_core_works_by_influence,
)
from scholar_gateway.verify import (
    VerificationRequest,
    VerificationResult,
    title_similarity,
    verify_reference,
)

__all__ = [
    "ALL_PROVIDERS",
    "DEFAULT_PROVIDER_ORDER",
    "CachedHttpResponse",
    "DedupeCluster",
    "DedupeConflict",
    "DedupeResult",
    "HttpCacheBackend",
    "HttpFetchResult",
    "InMemoryHttpCache",
    "NullHttpCache",
    "OaFulltextCandidate",
    "OaFulltextDocument",
    "OaFulltextPlan",
    "OaFulltextTarget",
    "ProviderCircuitOpenError",
    "ProviderConfig",
    "SafeHttpClient",
    "ScholarlyAuthorCandidate",
    "ScholarlyDiscoveryAdapter",
    "ScholarlyDiscoveryError",
    "ScholarlyDiscoveryQuery",
    "ScholarlyDiscoveryResult",
    "ScholarlyIdentifier",
    "ScholarlyLinkCandidate",
    "ScholarlyWorkCandidate",
    "SnowballExpansionResult",
    "SnowballSeed",
    "SqlAlchemyHttpCache",
    "VerificationRequest",
    "VerificationResult",
    "acquire_oa_fulltext",
    "discover_doaj_links",
    "discover_unpaywall_links",
    "build_adapter",
    "canonical_request_url",
    "dedupe_scholarly_candidates",
    "expand_citation_snowball",
    "normalize_arxiv_id",
    "normalize_corpus_id",
    "normalize_doi",
    "normalize_openalex_id",
    "normalize_pmcid",
    "normalize_pmid",
    "normalize_semantic_scholar_id",
    "normalize_title_for_dedupe",
    "normalized_title_hash",
    "plan_oa_fulltext",
    "provider_circuit_snapshot",
    "rank_by_citation_influence",
    "reset_provider_rate_limit_state",
    "select_core_works_by_influence",
    "title_similarity",
    "token_set_jaccard",
    "verify_reference",
]
