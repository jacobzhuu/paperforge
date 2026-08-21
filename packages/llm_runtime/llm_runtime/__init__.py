"""PaperForge LLM 运行时：OpenAI-compatible provider seam + 角色路由 + JSON 净化。

迁移自 DeepSearch services/orchestrator/app/llm/ 与 literature_review/json_utils.py。
"""

from llm_runtime.client import create_llm_provider, preflight_llm_provider
from llm_runtime.config import (
    DEFAULT_MODEL_PRICES,
    DEFAULT_ROLE_MODELS,
    DEFAULT_ROLE_RETRY,
    DEFAULT_ROLE_THINKING,
    LLMConfig,
    ModelPrice,
    Role,
    parse_model_prices,
)
from llm_runtime.json_utils import CiteKeyViolation, clean_and_parse_json, purify_llm_json
from llm_runtime.providers import (
    LLMProvider,
    NoopLLMProvider,
    OpenAICompatibleLLMProvider,
    UnsupportedLLMProvider,
    build_chat_completions_url,
    clamp_max_output_tokens,
    sanitize_openai_compatible_base_url,
)
from llm_runtime.retry_policy import (
    DEFAULT_TRUNCATION_RETRY_POLICY,
    TruncationRetryPolicy,
    parse_role_retry,
    resolve_truncation_policy,
)
from llm_runtime.runner import TRUNCATED_AT_CEILING, JsonResult, LLMCallRecord, LLMRunner
from llm_runtime.types import LLMError, LLMRequest, LLMResponse

__all__ = [
    "DEFAULT_MODEL_PRICES",
    "DEFAULT_ROLE_MODELS",
    "DEFAULT_ROLE_RETRY",
    "DEFAULT_ROLE_THINKING",
    "DEFAULT_TRUNCATION_RETRY_POLICY",
    "TRUNCATED_AT_CEILING",
    "CiteKeyViolation",
    "LLMConfig",
    "LLMError",
    "LLMProvider",
    "JsonResult",
    "LLMCallRecord",
    "LLMRequest",
    "LLMResponse",
    "LLMRunner",
    "ModelPrice",
    "NoopLLMProvider",
    "OpenAICompatibleLLMProvider",
    "Role",
    "TruncationRetryPolicy",
    "UnsupportedLLMProvider",
    "build_chat_completions_url",
    "clamp_max_output_tokens",
    "clean_and_parse_json",
    "create_llm_provider",
    "parse_model_prices",
    "parse_role_retry",
    "preflight_llm_provider",
    "purify_llm_json",
    "resolve_truncation_policy",
    "sanitize_openai_compatible_base_url",
]
