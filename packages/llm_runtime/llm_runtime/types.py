from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 迁移自 DeepSearch services/orchestrator/app/llm/types.py（原样，零改动）。


@dataclass(frozen=True)
class LLMRequest:
    system_prompt: str
    user_prompt: str
    model: str
    max_output_tokens: int
    temperature: float = 0.0
    json_output: bool = False
    thinking_mode: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    provider: str
    usage: dict[str, Any] | None = None
    raw_response_id: str | None = None
    finish_reason: str | None = None


class LLMError(RuntimeError):
    def __init__(
        self,
        *,
        provider: str,
        error_code: str,
        message: str,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.error_code = error_code
        self.status_code = status_code
        self.retryable = retryable

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "error_code": self.error_code,
            "status_code": self.status_code,
            "message": str(self),
            "retryable": self.retryable,
        }
