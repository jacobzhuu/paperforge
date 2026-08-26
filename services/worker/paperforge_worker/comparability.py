"""比较句的可比性判定：一处定义，写作 R5 与质检共用。

R5 要拦的是**拿不可比的数字说事**——「A 优于 B」，而 A 的数字来自一套实验设置、
B 的来自另一套。判据是两边的量测有没有共享的 ``comparability_key``。

生产实测（2026-08-26）说明这条判据被用错了地方：

* ``classify_claim`` 把 ``numeric`` 排在 ``comparison`` **之前**，所以判成
  ``comparison`` 的句子必然**不含数字**——27 条被 R5 删掉的句子，含数字的是 0 条。
* 这 27 条里，26 条的证据单元**没有任何量测**。要求共享量测键，对它们只能返回
  False。
* 于是 R5 退化成「删除所有定性比较句」：生产全量 102 条 comparison 类论断，
  ``support_status='supported'`` 的是 **0** 条。

被误删的两类，都不是 R5 想拦的东西：

1. **研究内比较**（27 条里 17 条只引 1 篇文献）。「GSPAttack 优于所有基线方法」
   引的是提出它的那篇论文自己的实验——这是综述里最常见的句式，由该研究自身的
   实验支持。``< 2 篇文献`` 返回 False 的写法把「不是跨研究比较」当成了
   「跨研究比较且不可比」。
2. **定性的跨研究综合**。「多项研究一致采用基于梯度的优化框架」比较的是方法路线，
   不是数值；量测可比性判据对它无话可说。最刺眼的是，被删的句子里有三条正是
   综述在说明「结果不可直接比较」——R5 删掉了它自己要强制的那句话。

所以判定分三种情形，只有第三种才是 R5 真正的战场；那一支逐字不变。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

#: 一个比较句的比较范围。
ComparisonScope = Literal["no_evidence", "within_study", "cross_study"]


def _get(unit: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    if isinstance(unit, Mapping):
        return unit.get(key, default)
    return getattr(unit, key, default)


def _work_ids(units: list[Any]) -> set[str]:
    return {str(_get(unit, "work_id")) for unit in units if _get(unit, "work_id")}


def _measurement_keys(unit: Mapping[str, Any] | Any) -> set[str]:
    return {
        str(_get(item, "comparability_key"))
        for item in (_get(unit, "measurements") or [])
        if _get(item, "comparability_key")
    }


def comparison_scope(units: list[Any]) -> ComparisonScope:
    """这个比较跨了几项研究。

    :param units: 该句绑定的证据单元。
    :returns: 无证据 / 研究内 / 跨研究。
    """
    works = _work_ids(units)
    if not works:
        return "no_evidence"
    return "within_study" if len(works) < 2 else "cross_study"


def measured_comparability(units: list[Any]) -> bool | None:
    """跨研究的**量测**是否可比。

    :returns: ``True`` 共享至少一个 ``comparability_key``；``False`` 各自有量测但
        没有交集；``None`` 表示可比的量测不足两组——此时这条判据无话可说，
        不能当成「不可比」。
    """
    key_sets = [keys for keys in (_measurement_keys(unit) for unit in units) if keys]
    if len(key_sets) < 2:
        return None
    return bool(set.intersection(*key_sets))


def comparison_admissible(units: list[Any]) -> bool:
    """这个比较句能不能留在正文里。

    :param units: 该句绑定的证据单元。
    :returns: 可留为 ``True``。研究内比较由该研究自己的实验支持；跨研究比较在
        两边都有量测时必须共享 ``comparability_key``；没有量测可比时本判据让位，
        由证据等级（R4）、定位（R6）与质检的词面支持度继续把关。
    """
    scope = comparison_scope(units)
    if scope == "no_evidence":
        return False
    if scope == "within_study":
        return True
    measured = measured_comparability(units)
    return True if measured is None else measured


__all__ = [
    "ComparisonScope",
    "comparison_admissible",
    "comparison_scope",
    "measured_comparability",
]
