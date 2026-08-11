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
    "evidence_classifier": "gpt-4o-mini",
    "evidence_classifier_fallback": "gpt-4o",
    "experiment_extractor": "gpt-4o-mini",
    "synthesizer": "gpt-4o-mini",
}

# New semantic roles inherit the deployment's existing model tiers unless an
# operator explicitly routes them.  This keeps old production env files valid
# while allowing qmatrix classification to use a different thinking policy
# from mechanical card extraction.
ROLE_MODEL_FALLBACKS: dict[str, str] = {
    "evidence_classifier": "extractor",
    "evidence_classifier_fallback": "planner",
    # 结构化实验抽取和卡片抽取一样是「按封闭 schema 读文本」，不是规划或写作，
    # 因此沿用部署已有的 extractor 档位，旧 env 文件无需改动。
    "experiment_extractor": "extractor",
    # 跨研究综合是推理，不是按 schema 读文本：判断"同一可比条件下这些结果说明了
    # 什么"需要规划档位的模型，因此回退到 planner 而不是 extractor。
    "synthesizer": "planner",
}

# DeepSeek V4 defaults to high-effort thinking.  That is useful for planning and
# long-form writing, but it wastes latency/output budget on bounded extraction and
# reranking tasks whose prompts already define a closed JSON schema.  Keep the
# quality-critical roles on the provider default unless a deployment opts in.
DEFAULT_ROLE_THINKING: dict[str, str] = {
    "extractor": "disabled",
    "reranker": "disabled",
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
    # 角色 → enabled/disabled。未配置的角色保留 provider 默认思考档位。
    role_thinking: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROLE_THINKING))

    def model_for_role(self, role: Role) -> str:
        if role in self.role_models and self.role_models[role].strip():
            return self.role_models[role].strip()
        fallback_role = ROLE_MODEL_FALLBACKS.get(role)
        if fallback_role and self.role_models.get(fallback_role, "").strip():
            return self.role_models[fallback_role].strip()
        if role in DEFAULT_ROLE_MODELS:
            return DEFAULT_ROLE_MODELS[role]
        return self.model

    def thinking_for_role(self, role: Role) -> str | None:
        value = str(self.role_thinking.get(role) or "").strip().lower()
        return value if value in {"enabled", "disabled"} else None
