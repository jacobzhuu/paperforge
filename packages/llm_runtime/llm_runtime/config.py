from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
    "section_reviewer": "gpt-4o",
    "synthesizer": "gpt-4o-mini",
}

# New semantic roles inherit the deployment's existing model tiers unless an
# operator explicitly routes them.  This keeps old production env files valid
# while allowing qmatrix classification to use a different thinking policy
# from mechanical card extraction.
ROLE_MODEL_FALLBACKS: dict[str, str] = {
    # 问题—证据判定要的是判断力，不是按 schema 读文本。实测（项目 6a6bbf18 的
    # 真实候选集，每臂 4 个子问题）：
    #   flash + 思考开（原配置）：1/4 截断，17 条链接，中位 76.4s
    #   flash + 思考关：0 截断但**退化**——4 个问题里 3 个返回 0 条，第 4 个把 24 个
    #                   候选全连上（半数标 not_comparable）。这不是判断，是放弃判断。
    #   pro   + 思考关：0 截断，34 条链接，中位 6.8s，且**完全覆盖** flash+思考开
    #                   找到的那 17 条。
    # 所以这里换档位而不是照搬写作角色的「关思考」——那条路会悄悄毁掉证据链接。
    "evidence_classifier": "planner",
    "evidence_classifier_fallback": "planner",
    # 结构化实验抽取和卡片抽取一样是「按封闭 schema 读文本」，不是规划或写作，
    # 因此沿用部署已有的 extractor 档位，旧 env 文件无需改动。
    "experiment_extractor": "extractor",
    # 跨研究综合是推理，不是按 schema 读文本：判断"同一可比条件下这些结果说明了
    # 什么"需要规划档位的模型，因此回退到 planner 而不是 extractor。
    "synthesizer": "planner",
    # 语义评审（这一节答没答上、这些研究是什么关系）同样是判断题，走 planner 档。
    "section_reviewer": "planner",
}

# DeepSeek V4 defaults to high-effort thinking.  That is useful for planning, but it
# wastes latency/output budget on tasks whose prompts already define a closed JSON
# schema — and on any task whose own output is large enough to compete with the
# reasoning for the same `max_output_tokens`.  Keep the remaining roles on the
# provider default unless a deployment opts in.
DEFAULT_ROLE_THINKING: dict[str, str] = {
    "extractor": "disabled",
    "reranker": "disabled",
    # Claim entailment is a bounded classification task with a closed JSON schema.  Measured by an
    # out-of-band replay over a 91-pair cohort (not a pipeline run): with thinking disabled all 91
    # pairs resolved in 9 calls, against 39 for the same cohort with thinking on, which spent most
    # of the output budget on hidden reasoning.  Note the pipeline itself caps a single pass at
    # MAX_CLAIM_EVIDENCE_CHECKS (60), so "91/91" is a property of the replay, not an invariant any
    # quality job can report.
    "verifier": "disabled",
    # 结构化实验抽取是按封闭 schema 读文本，和卡片抽取同类。开着思考会把输出预算
    # 烧在推理上：生产实测 26 次调用里 15 次 `output_truncated`，13 篇论文有 6 篇
    # 一条结果都没抽出来。
    "experiment_extractor": "disabled",
    # 写作是本管线输出最大的一步（目标 1200 字，外加每句回抄 evidence_ids），它和
    # 推理抢的是同一份 max_output_tokens，而 deepseek 系被 clamp_max_output_tokens()
    # 压在 8192——加预算这条路没有余量。生产实测（项目 6a6bbf18，2026-08-14）：21 次
    # writer 调用 11 次零内容返回，截断调用平均 79s 且产出 0 token，11 节里 5 节因此
    # 从未经过模型，降级路径把证据原文当正文交了出去。
    "writer": "disabled",
    # 证据分类同样被推理吃预算：生产历史 77 次调用 39 次 `output_truncated`（51%），
    # 而截断之后只能回落到词汇匹配——那条路只会输出 stance="supports"，于是整个
    # 生产库 429 条链接里 **一条 contradicts 都没有**，综述的「冲突识别」形同虚设。
    # 但这里**不能**照搬写作角色的做法：见 ROLE_MODEL_FALLBACKS 上方的实测，
    # flash 关掉思考会直接退化成不判断。关思考的前提是同时换到 planner 档。
    "evidence_classifier": "disabled",
    "evidence_classifier_fallback": "disabled",
    # 语义评审输出的是一份闭集 JSON 判定，不需要长推理；而按 evidence_classifier
    # 那一轮的实测，判断力来自档位（planner）而不是思考开关。
    "section_reviewer": "disabled",
}


@dataclass(frozen=True)
class ModelPrice:
    """单价，单位是**每百万 token 的货币金额**（业界标准报价单位）。

    按每百万计而不是每 token，是为了让配置里写的就是服务商官网上的数字，
    抄进来时不需要换算——换算出错的账单看起来和正确的一模一样。
    """

    input_per_mtok: float
    output_per_mtok: float

    def estimate(self, *, input_tokens: int | None, output_tokens: int | None) -> float | None:
        """算一次调用的费用；用量缺失时返回 ``None``，绝不当成 0。

        「花了 0 元」和「不知道花了多少」在成本面板上必须是两件事。
        """
        if input_tokens is None and output_tokens is None:
            return None
        return (
            (input_tokens or 0) * self.input_per_mtok + (output_tokens or 0) * self.output_per_mtok
        ) / 1_000_000


def parse_model_prices(raw: Any) -> dict[str, ModelPrice]:
    """把 ``{"model": {"input": 0.27, "output": 1.1}}`` 解析成价目表。

    坏条目跳过而不是整份丢弃：漏配一个模型只该让那个模型显示为"未定价"，
    不该把整个部署的成本记账一起关掉。
    """
    if not isinstance(raw, dict):
        return {}
    prices: dict[str, ModelPrice] = {}
    for model, value in raw.items():
        if not isinstance(model, str) or not isinstance(value, dict):
            continue
        try:
            input_price = float(value.get("input", value.get("input_per_mtok")))
            output_price = float(value.get("output", value.get("output_per_mtok")))
        except (TypeError, ValueError):
            continue
        if input_price < 0 or output_price < 0:
            continue
        prices[model.strip().casefold()] = ModelPrice(
            input_per_mtok=input_price,
            output_per_mtok=output_price,
        )
    return prices


# 故意留空。价格随服务商、合同与时间变化，硬编码一份会在某一天开始**自信地报错数**,
# 而一个错的金额比一个诚实的「未定价」危险得多。由部署通过 LLM_MODEL_PRICES 提供；
# 没配的模型在成本面板上显式计为未定价。
DEFAULT_MODEL_PRICES: dict[str, ModelPrice] = {}


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
    # 模型 → 单价。空表示这个部署没配价格，于是所有调用都记为未定价。
    model_prices: dict[str, ModelPrice] = field(default_factory=lambda: dict(DEFAULT_MODEL_PRICES))

    def price_for_model(self, model: str) -> ModelPrice | None:
        """精确匹配优先，其次取最长的前缀匹配。

        服务商经常在模型名后面挂日期或版本后缀（``deepseek-v4-pro-0711``）。
        配了 ``deepseek-v4-pro`` 就该覆盖它的所有快照，否则每次服务商发新版本，
        成本面板都会毫无征兆地退回未定价。
        """
        key = (model or "").strip().casefold()
        if not key:
            return None
        exact = self.model_prices.get(key)
        if exact is not None:
            return exact
        best: tuple[int, ModelPrice] | None = None
        for candidate, price in self.model_prices.items():
            if key.startswith(candidate) and (best is None or len(candidate) > best[0]):
                best = (len(candidate), price)
        return best[1] if best else None

    def estimate_cost(
        self, model: str, *, input_tokens: int | None, output_tokens: int | None
    ) -> float | None:
        price = self.price_for_model(model)
        if price is None:
            return None
        return price.estimate(input_tokens=input_tokens, output_tokens=output_tokens)

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
        """部署显式配置优先，其次落到本文件的默认档位。

        没有这层回退，新增角色就永远拿不到自己该有的思考策略——部署的
        ``LLM_ROLE_THINKING`` 是一份写死的全量映射，不会因为代码里多了个角色而更新。
        故意**不**沿用 ``ROLE_MODEL_FALLBACKS``：``evidence_classifier`` 与
        ``extractor`` 共用模型档位，但思考策略是分开的（见上方注释）。
        """
        for source in (self.role_thinking, DEFAULT_ROLE_THINKING):
            value = str(source.get(role) or "").strip().lower()
            if value in {"enabled", "disabled"}:
                return value
        return None
