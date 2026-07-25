from __future__ import annotations

from dataclasses import dataclass, field

# PaperForge 扩展：把 DeepSearch 的全局 Settings 依赖解耦为显式注入的配置对象，
# 并新增「角色 → 模型」路由（见 docs/design.md §4.9）。
# 角色档位：planner / extractor / reranker / writer / polisher / verifier。

Role = str

DEFAULT_ROLE_MODELS: dict[str, str] = {
    "planner": "gpt-4o-mini",
    "extractor": "gpt-4o-mini",
    "reranker": "gpt-4o-mini",
    "writer": "gpt-4o",
    "polisher": "gpt-4o",
    "verifier": "gpt-4o-mini",
}


@dataclass(frozen=True)
class LLMConfig:
    """单一 provider 的连接与重试配置（构造注入，取代旧 Settings 全局）。"""

    provider: str = "noop"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    enabled: bool = True
    timeout_seconds: float = 60.0
    max_retries: int = 2
    trust_env_proxy: bool = False
    total_deadline_seconds: float | None = 180.0
    retry_backoff_seconds: float = 1.0
    # 角色 → 模型 覆盖映射；缺省回退到 DEFAULT_ROLE_MODELS，再回退到 self.model。
    role_models: dict[str, str] = field(default_factory=dict)

    def model_for_role(self, role: Role) -> str:
        if role in self.role_models and self.role_models[role].strip():
            return self.role_models[role].strip()
        if role in DEFAULT_ROLE_MODELS:
            return DEFAULT_ROLE_MODELS[role]
        return self.model
