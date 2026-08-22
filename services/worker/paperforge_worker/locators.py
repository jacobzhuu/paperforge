"""证据单元的「定位」：机器判定与人可读表示出自同一处。

此前 ``located`` 在管线里有三份互不知情的定义：

* ``evidence._fulltext_grade``  —— ``page or section or paragraph``，满足即定级
  ``B_located_prose``（「已定位的散文」），不满足才降到 ``C_fulltext_unlocated``；
* ``writing.enforce_sentence_evidence_rules`` 的 R6 —— ``page or object_ref``，
  不满足就把数字句**整句删掉**；
* ``quality`` 的 R6 —— 同一条判定的第二份实现，产出 ``numeric_locator_missing``。

生产实测（2026-08-22，10,132 条证据单元）：管线自己标成 ``C_fulltext_unlocated``
的只有 26 条，而 R6 把另外 5,256 条**已定级为已定位**的单元当成未定位——202 倍。
缺口全部落在 ``prose_only``：正文散文抽出来的证据准确记着它在第几节第几段，而
``page`` 有没有取决于 PDF 解析给不给页码，与证据质量无关。

更尖锐的一点是：写作提示词里的证据台账**本来就把 ``section_path`` 显示给模型看**
（``writing._evidence_context_block``）。提示词说「这条证据在 §Results」，模型据此
写了一句，R6 再以「未定位」为由把它删掉。

这里给出唯一一份判定。不变量：

    ``locator_of(unit) is not None``  ⟺  这条单元可以被引用者查证

人可读串与机器判定同源，所以不可能出现「判定说可定位、却给不出定位」的组合。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

#: 定位的强度档位，由强到弱。``object`` 指向一个可寻址对象（表 3、图 1），
#: ``page`` 指向页码，``section`` 指向章节（可能再细到段落序号）。
LocatorKind = Literal["object", "page", "section"]


@dataclass(frozen=True)
class EvidenceLocator:
    """一条证据的定位：机器可判定的强度 + 引用者能照着翻的字符串。"""

    kind: LocatorKind
    display: str


def _clean(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _int_or_none(value: Any) -> int | None:
    # 布尔是 int 的子类，但 `page=True` 不是页码。
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def evidence_locator(
    *,
    page: Any = None,
    object_ref: Any = None,
    section_path: Any = None,
    paragraph_index: Any = None,
) -> EvidenceLocator | None:
    """由四个定位字段解析出定位；都没有则返回 ``None``。

    ``paragraph_index`` 只用来细化 ``section_path``，从不单独成立：生产库里
    10,132 条单元中，带段落序号却没有章节的**一条都没有**，而一个没有章节的
    段落序号也没法让人翻到原文。

    :param page: 页码；非整数视为缺失。
    :param object_ref: 可寻址对象引用，如 ``table:3``。
    :param section_path: 章节路径，如 ``Results``。
    :param paragraph_index: 章节内段落序号。
    :returns: 定位，或 ``None`` 表示这条证据确实无从查证。
    """
    page_number = _int_or_none(page)
    obj = _clean(object_ref)
    section = _clean(section_path)
    paragraph = _int_or_none(paragraph_index)

    parts: list[str] = []
    if page_number is not None:
        parts.append(f"p.{page_number}")
    if obj:
        parts.append(obj)
    if section:
        parts.append(f"§{section} ¶{paragraph}" if paragraph is not None else f"§{section}")
    if not parts:
        return None

    kind: LocatorKind = "object" if obj else "page" if page_number is not None else "section"
    return EvidenceLocator(kind=kind, display=", ".join(parts))


def locator_of(unit: Mapping[str, Any] | Any) -> EvidenceLocator | None:
    """同 :func:`evidence_locator`，但从证据单元的 dict 或对象上取字段。

    管线里的证据单元有两种形态：大纲 bundle 里的 dict、抽取阶段的
    ``EvidenceCandidate`` 对象。两种都要能问同一个问题。
    """
    if isinstance(unit, Mapping):
        get = unit.get
    else:
        def get(key: str, default: Any = None) -> Any:
            return getattr(unit, key, default)
    return evidence_locator(
        page=get("page"),
        object_ref=get("object_ref"),
        section_path=get("section_path"),
        paragraph_index=get("paragraph_index"),
    )


def is_located(unit: Mapping[str, Any] | Any) -> bool:
    """这条证据能不能被引用者查证。R6 判定数字句是否可以留在正文里用的就是它。"""
    return locator_of(unit) is not None


def locator_display(unit: Mapping[str, Any] | Any) -> str | None:
    """定位的人可读串；无法定位时为 ``None``。

    语言相关的兜底文案（「未定位」/ ``unlocated``）留给调用方，因为它取决于
    正在生成哪一种语言的产物。
    """
    locator = locator_of(unit)
    return locator.display if locator else None


__all__ = [
    "EvidenceLocator",
    "LocatorKind",
    "evidence_locator",
    "is_located",
    "locator_display",
    "locator_of",
]
