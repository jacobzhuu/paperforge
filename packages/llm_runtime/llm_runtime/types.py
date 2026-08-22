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
    # Runner-level retry provenance. Providers leave this at zero; ``LLMRunner`` increments it
    # when it has already spent the one allowed larger-budget truncation retry. Structured JSON
    # parsing uses the marker to avoid starting a second, nested retry sequence.
    truncation_retries: int = 0


class LLMError(RuntimeError):
    def __init__(
        self,
        *,
        provider: str,
        error_code: str,
        message: str,
        status_code: int | None = None,
        retryable: bool = False,
        finish_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.error_code = error_code
        self.status_code = status_code
        self.retryable = retryable
        # 失败也有 finish_reason。截断时它是 "length"，而这正是台账里区分
        # 「预算不够」与「模型自己停了」的那一位信息——只在成功路径上记录，
        # 恰好把最需要它的那一类调用漏掉了。
        self.finish_reason = finish_reason

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "error_code": self.error_code,
            "status_code": self.status_code,
            "message": str(self),
            "retryable": self.retryable,
            "finish_reason": self.finish_reason,
        }
