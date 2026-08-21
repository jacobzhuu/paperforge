"""按角色解析的截断重试策略。

一次 ``output_truncated`` 的唯一补救手段是「用更大的预算再问一遍」，而预算的
真实上限由模型决定（``providers.clamp_max_output_tokens``）。当加倍之后的预算
被 clamp 回原值时，重试发出的是**逐字相同的请求**，结果只能相同——那次调用是
纯粹的浪费。生产实测（2026-08-03 之后的 1,694 次调用）：writer 请求 8000，
加倍到 16000 再被 deepseek 系的 8192 clamp 回来，净增 192 token（2.4%），
60 次重试里 21 次再次截断，每次白烧一整轮 40-80 秒的调用。

策略因此是**配置**而不是常量：不同角色的输出规模差得很远（verifier 请求 2400，
writer 请求 8000），该不该重试、加多少，要能按部署调整。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 预算倍数的合理区间。1.0 表示「同样的预算再问一次」，那正是本模块要消灭的
#: 无效重试；上限防止一个笔误把预算推到远超任何模型上限的数值。
_MIN_MULTIPLIER = 1.0
_MAX_MULTIPLIER = 8.0
#: 重试次数上限。截断是确定性的：同一个提示词在同一个上限下必然再次截断，所以
#: 补救手段最多试几次就该让位给确定性回退，而不是把预算翻到天上去。
_MAX_ATTEMPTS_CEILING = 3
#: 增幅阈值的合理区间。1.0 表示「只要多一个 token 就重试」，那正是 writer 那 21
#: 次白烧调用的成因；上限之外的阈值等于永不重试，该用 ``max_attempts=0`` 表达。
_MIN_GROWTH_RATIO = 1.0
_MAX_GROWTH_RATIO = 4.0

DEFAULT_MULTIPLIER = 2.0
DEFAULT_MAX_ATTEMPTS = 1
#: 默认要求预算至少涨 25% 才值得重试。生产实测：writer 请求 8000，加倍后被
#: deepseek 系的 8192 clamp 回来，净增 192 token（1.024 倍），60 次重试里 21 次
#: 再次截断——一个被截断的输出不会因为多 192 个 token 就写得完，而那次重试要花
#: 一整轮 40-80 秒的调用。同一条规则对 verifier（请求 2400，加倍到 4800，2.0 倍）
#: 判定为值得重试，它救回了 17 次。
#:
#: 这个判定是**按模型算的**，所以同一个角色换到上限更高的模型会自动恢复重试：
#: writer 在 gpt-4.1（上限 32768）上是 8000 → 16000，2.0 倍。
DEFAULT_MIN_GROWTH_RATIO = 1.25


@dataclass(frozen=True)
class TruncationRetryPolicy:
    """一个角色对 ``output_truncated`` 的响应策略。"""

    #: 下一次尝试的预算相对本次的倍数。
    multiplier: float = DEFAULT_MULTIPLIER
    #: 首次尝试之外还允许几次。0 表示截断即回退，不重试。
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    #: 经模型上限 clamp 之后，预算至少要涨到原来的多少倍才值得重试。
    min_growth_ratio: float = DEFAULT_MIN_GROWTH_RATIO


DEFAULT_TRUNCATION_RETRY_POLICY = TruncationRetryPolicy()


def resolve_truncation_policy(raw: Any, *, path: str) -> TruncationRetryPolicy:
    """校验并冻结一份策略配置。

    非法值**抛异常**而不是静默取默认：一个拼错的键悄悄退回默认策略，会让部署以为
    自己调过参数，而生产行为一如既往（``AGENTS.md``「配错要吵」）。

    :param raw: 配置片段；``None`` 取默认策略。
    :param path: 出错信息里用来定位这份配置的路径，例如 ``LLM_ROLE_RETRY.writer``。
    :returns: 冻结的策略对象，可安全长期持有。
    :raises ValueError: 出现未知键、类型不对或取值越界时。
    """
    if raw is None:
        return DEFAULT_TRUNCATION_RETRY_POLICY
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be an object, got {type(raw).__name__}")

    unknown = set(raw) - {"multiplier", "max_attempts", "min_growth_ratio"}
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {', '.join(sorted(unknown))}")

    multiplier = raw.get("multiplier", DEFAULT_MULTIPLIER)
    if isinstance(multiplier, bool) or not isinstance(multiplier, int | float):
        raise ValueError(f"{path}.multiplier must be a number")
    multiplier = float(multiplier)
    if not _MIN_MULTIPLIER <= multiplier <= _MAX_MULTIPLIER:
        raise ValueError(
            f"{path}.multiplier must be between {_MIN_MULTIPLIER} and {_MAX_MULTIPLIER}"
        )

    max_attempts = raw.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise ValueError(f"{path}.max_attempts must be an integer")
    if not 0 <= max_attempts <= _MAX_ATTEMPTS_CEILING:
        raise ValueError(f"{path}.max_attempts must be between 0 and {_MAX_ATTEMPTS_CEILING}")

    min_growth_ratio = raw.get("min_growth_ratio", DEFAULT_MIN_GROWTH_RATIO)
    if isinstance(min_growth_ratio, bool) or not isinstance(min_growth_ratio, int | float):
        raise ValueError(f"{path}.min_growth_ratio must be a number")
    min_growth_ratio = float(min_growth_ratio)
    if not _MIN_GROWTH_RATIO <= min_growth_ratio <= _MAX_GROWTH_RATIO:
        raise ValueError(
            f"{path}.min_growth_ratio must be between {_MIN_GROWTH_RATIO} and {_MAX_GROWTH_RATIO}"
        )

    return TruncationRetryPolicy(
        multiplier=multiplier,
        max_attempts=max_attempts,
        min_growth_ratio=min_growth_ratio,
    )


def parse_role_retry(raw: Any) -> dict[str, TruncationRetryPolicy]:
    """把 ``{"writer": {"max_attempts": 0}}`` 解析成按角色的策略表。

    与 ``parse_model_prices`` 的宽容策略**相反**：那里跳过坏条目是因为漏配一个
    模型只该让它显示为「未定价」，而这里一个被忽略的条目意味着某个角色继续按
    旧策略烧钱，且没有任何迹象。

    :param raw: 解析后的 JSON 对象；``None`` 或空值返回空表。
    :returns: 角色到策略的映射。
    :raises ValueError: 任一条目非法时。
    """
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"LLM_ROLE_RETRY must be a JSON object, got {type(raw).__name__}")
    return {
        str(role): resolve_truncation_policy(value, path=f"LLM_ROLE_RETRY.{role}")
        for role, value in raw.items()
    }


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MIN_GROWTH_RATIO",
    "DEFAULT_MULTIPLIER",
    "DEFAULT_TRUNCATION_RETRY_POLICY",
    "TruncationRetryPolicy",
    "parse_role_retry",
    "resolve_truncation_policy",
]
