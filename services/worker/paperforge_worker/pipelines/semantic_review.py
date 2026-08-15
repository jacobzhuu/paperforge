"""语义评审：这一节**回答了它的子问题吗**，以及**这些研究彼此是什么关系**。

到上一轮为止，质量门问的都是结构性问题——有没有正文、语种对不对、数字有没有出处、
是不是照抄。这些全部通过，稿子仍然可能只是「围绕问题写了一段通顺的话」。判断
「答没答上」需要语义判断，写不成词表。

本模块只提供**两个**评审器，共用同一套调用与准入纪律：

1. :func:`review_section` —— 一节 vs 它的子问题。给出四项判断（是否回答、论断是否
   有支撑、结论强度是否与证据相称、是综合还是罗列），外加一个**病因**。
2. :func:`compare_findings` —— 一个子问题下的证据彼此是什么关系。这条路不依赖
   结构化测量：绝大多数生物医学 OA 证据没有可比性键（生产库 7823 条证据里只有
   284 条带测量值，且实测每个可比簇都只有 1 条），于是「文献存在分歧」这件事在
   现有机制下永远表达不出来。

设计纪律照抄 ``synthesis_llm``：模型只能在**给定的 evidence_id 集合内**说话，越界的
条目整条丢弃；判断不出来就返回 None，让调用方保持既有的确定性行为。评审器永远
不改证据归属、引用与出处——它只贴标签。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from llm_runtime import LLMRunner

#: 判断类角色。按上一轮实测（真实候选集三臂对比）：判断力跟档位走，思考开关反而
#: 会让弱档退化成不判断，所以这个角色走 planner 档并关掉思考。
REVIEWER_ROLE = "section_reviewer"

MAX_OUTPUT_TOKENS = 2000
#: 一次评审最多看多少条证据。够覆盖一个子问题的证据面，又不至于把预算烧在上下文上。
MAX_EVIDENCE_PER_REVIEW = 14
EVIDENCE_TEXT_CHARS = 700
SECTION_TEXT_CHARS = 4000

ANSWER_LEVELS = ("full", "partial", "no")
SUPPORT_LEVELS = ("sufficient", "thin", "unsupported")
CALIBRATION_LEVELS = ("matched", "overclaimed", "underclaimed")
SYNTHESIS_MODES = ("synthesized", "mixed", "listed")
#: 病因决定修哪里。这是本模块存在的理由之一：不同的病因对应完全不同的修复动作，
#: 一律重写正文对「证据本来就不够」的章节毫无作用，只是把钱烧掉。
DIAGNOSES = ("none", "evidence_gap", "synthesis_gap", "writing_gap")

#: 分歧关系。四类，取自审稿人实际会做的区分。
RELATIONS = ("contradictory", "moderated", "complementary", "incomparable")
#: `moderated` 必须说清「为什么看起来不一致」，且只能从这个闭集里选——
#: 允许自由文本会让模型用一句空话把任何冲突解释掉。
MODERATORS = ("population", "dataset", "method", "condition", "endpoint", "scope")
POLARITIES = ("supports", "opposes", "mixed")


@dataclass(frozen=True)
class SectionVerdict:
    """一节的语义评审结论。"""

    section_key: str
    answers_question: str
    support: str
    calibration: str
    synthesis_mode: str
    diagnosis: str
    rationale: str
    unanswered_aspects: tuple[str, ...] = ()
    unsupported_claims: tuple[str, ...] = ()
    #: 证据确实不够时，正文有没有**明说**这个缺口。说了就不算失败——
    #: 目标明确要求「证据不足就报告缺口，而不是把段落灌长」。
    gap_declared: bool = False

    @property
    def acceptable(self) -> bool:
        """验收判据。写死在一处，避免调用方各自解释一遍。

        允许 ``partial``——前提是正文自己承认了缺口。评审器判定证据不足而正文却
        写得像回答完整，那才是问题。
        """
        if self.answers_question == "no":
            return False
        if self.answers_question == "partial" and not self.gap_declared:
            return False
        return (
            self.support != "unsupported"
            and self.calibration != "overclaimed"
            and self.synthesis_mode != "listed"
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "section_key": self.section_key,
            "answers_question": self.answers_question,
            "support": self.support,
            "calibration": self.calibration,
            "synthesis_mode": self.synthesis_mode,
            "diagnosis": self.diagnosis,
            "gap_declared": self.gap_declared,
            "acceptable": self.acceptable,
            "unanswered_aspects": list(self.unanswered_aspects),
            "unsupported_claims": list(self.unsupported_claims),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class ComparabilityGroup:
    """同一个论点下若干条证据的关系。"""

    claim_topic: str
    relation: str
    members: tuple[dict[str, Any], ...]
    moderator: str | None = None
    rationale: str = ""

    @property
    def work_ids(self) -> set[str]:
        return {str(item.get("work_id") or "") for item in self.members}

    def to_payload(self) -> dict[str, Any]:
        return {
            "claim_topic": self.claim_topic,
            "relation": self.relation,
            "moderator": self.moderator,
            "rationale": self.rationale,
            "evidence_ids": [str(item.get("evidence_id")) for item in self.members],
            "members": list(self.members),
        }


@dataclass
class ReviewOutcome:
    verdicts: list[SectionVerdict] = field(default_factory=list)
    groups: dict[str, list[ComparabilityGroup]] = field(default_factory=dict)
    calls: int = 0
    failed_calls: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "sections_reviewed": len(self.verdicts),
            "sections_failing": [v.section_key for v in self.verdicts if not v.acceptable],
            "diagnoses": {v.section_key: v.diagnosis for v in self.verdicts if not v.acceptable},
            "verdicts": [v.to_payload() for v in self.verdicts],
            "comparability": {
                question_id: [group.to_payload() for group in groups]
                for question_id, groups in self.groups.items()
            },
            "calls": self.calls,
            "failed_calls": self.failed_calls,
        }


_SECTION_PROMPT_ZH = """你是学术综述的审稿人。判断给定章节**有没有真正回答**它对应的子问题。

只输出 JSON：
{"answers_question":"full|partial|no",
 "unanswered_aspects":["子问题里没被回答的方面"],
 "support":"sufficient|thin|unsupported",
 "unsupported_claims":["正文里缺乏所列证据支撑的论断"],
 "calibration":"matched|overclaimed|underclaimed",
 "synthesis_mode":"synthesized|mixed|listed",
 "gap_declared":true|false,
 "diagnosis":"none|evidence_gap|synthesis_gap|writing_gap",
 "rationale":"一两句话说明判断依据"}

判据：
- answers_question：把子问题拆成它实际问的几个方面，逐一看正文答没答。只沾边不算答。
- support：正文的论断能不能由下面列出的证据支撑。注意是「这些证据」，不是你的知识。
- calibration：结论强度与证据强度是否相称。证据只来自单一体系却写成普适结论＝overclaimed；
  证据充分却只敢说「可能有关」＝underclaimed。
- synthesis_mode：跨研究综合（比较、归纳条件、指出分歧）＝synthesized；
  逐篇复述「文献A做了X、文献B做了Y」＝listed。
- gap_declared：正文有没有**明说**哪些方面证据不足。承认缺口是合格行为，不是失分项。
- diagnosis：只在不合格时给出病因——
  evidence_gap：给的证据本身就答不了这个问题，重写正文没用；
  synthesis_gap：证据够，但没做跨研究综合（罗列、或结论与证据不相称）；
  writing_gap：证据与综合都够，是行文/论证结构的问题。"""

_COMPARE_PROMPT_ZH = """你判断同一个子问题下的多条证据**彼此是什么关系**，用于综述里
「这些研究一致 / 存在分歧 / 条件不同 / 无法比较」的表述。

只输出 JSON：
{"groups":[{"claim_topic":"这一组共同讨论的具体论点",
  "relation":"contradictory|moderated|complementary|incomparable",
  "moderator":"population|dataset|method|condition|endpoint|scope|null",
  "members":[{"evidence_id":"给定的 ID","polarity":"supports|opposes|mixed",
              "condition":"该条成立的条件"}],
  "rationale":"为什么是这个关系"}]}

关系定义：
- contradictory：就**同一个论点、可直接比较的条件**给出相反结论。必须跨 ≥2 篇文献，
  且组内同时存在 supports 与 opposes。
- moderated：结论看起来不同，但可由 moderator 里的某一项解释（人群/数据集/方法/
  条件/终点/范围不同）。必须指明是哪一项。
- complementary：讨论同一主题的不同侧面，互为补充，不构成分歧。
- incomparable：现有信息不足以判断它们是否可比。

铁律：**不要为了让综述好看而制造冲突**。看起来不同但能被条件解释的，是 moderated；
拿不准的，是 incomparable。只能使用给定的 evidence_id。"""


def _evidence_block(evidence: list[dict[str, Any]]) -> str:
    lines = []
    for item in evidence[:MAX_EVIDENCE_PER_REVIEW]:
        locator = ", ".join(
            value
            for value in (
                f"p.{item.get('page')}" if item.get("page") else "",
                str(item.get("section_path") or ""),
            )
            if value
        )
        lines.append(
            f"- evidence_id={item.get('evidence_id')} work={item.get('work_id')} "
            f"cite={item.get('cite_key')} grade={item.get('grade')} "
            f"locator={locator or 'unlocated'}\n"
            f"  text: {str(item.get('text') or '')[:EVIDENCE_TEXT_CHARS]}"
        )
    return "\n".join(lines) or "(no evidence)"


async def _judge(
    runner: LLMRunner | None,
    *,
    system_prompt: str,
    user_prompt: str,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """跑一次评审调用。判断不出来就返回 None——调用方保持既有行为，绝不臆造判定。"""
    if runner is None or not runner.enabled:
        return None
    result = await runner.agenerate_json(
        REVIEWER_ROLE,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        temperature=0.0,
        metadata=metadata,
    )
    if not result.ok or not isinstance(result.value, dict):
        return None
    return result.value


def _one_of(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _strings(value: Any, *, limit: int = 6) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out = [str(item).strip()[:200] for item in value if str(item or "").strip()]
    return tuple(out[:limit])


def build_section_verdict(
    payload: dict[str, Any],
    *,
    section_key: str,
) -> SectionVerdict:
    """把模型输出收进闭集。纯函数，便于直接断言。

    每个字段都落到枚举里，拿不准一律取**最宽松**的那一档：评审器的作用是发现问题，
    不是靠解析噪声制造问题。
    """
    answers = _one_of(payload.get("answers_question"), ANSWER_LEVELS, "full")
    support = _one_of(payload.get("support"), SUPPORT_LEVELS, "sufficient")
    calibration = _one_of(payload.get("calibration"), CALIBRATION_LEVELS, "matched")
    mode = _one_of(payload.get("synthesis_mode"), SYNTHESIS_MODES, "synthesized")
    diagnosis = _one_of(payload.get("diagnosis"), DIAGNOSES, "none")
    verdict = SectionVerdict(
        section_key=section_key,
        answers_question=answers,
        support=support,
        calibration=calibration,
        synthesis_mode=mode,
        diagnosis=diagnosis,
        rationale=str(payload.get("rationale") or "")[:400],
        unanswered_aspects=_strings(payload.get("unanswered_aspects")),
        unsupported_claims=_strings(payload.get("unsupported_claims")),
        gap_declared=bool(payload.get("gap_declared")),
    )
    if verdict.acceptable:
        # 合格的章节没有病因可言；模型偶尔会两边都填，以验收判据为准。
        return SectionVerdict(**{**verdict.__dict__, "diagnosis": "none"})
    if verdict.diagnosis == "none":
        # 判为不合格却没说病因：按判定本身推一个，别让它落进「无从修复」。
        inferred = (
            "evidence_gap"
            if verdict.support == "unsupported" or verdict.answers_question == "no"
            else "synthesis_gap"
            if verdict.synthesis_mode == "listed" or verdict.calibration == "overclaimed"
            else "writing_gap"
        )
        return SectionVerdict(**{**verdict.__dict__, "diagnosis": inferred})
    return verdict


#: 修复动作。一律重写是上一代的做法，对「证据本来就不够」的章节毫无作用。
REPAIR_ROUTES = ("none", "retrieve", "resynthesize", "rewrite")


#: 判定说证据「薄」，但这一节的证据池里还有这么多条一次都没被引用——那就不是没检索到，
#: 是检索到了没用上。实测（项目 6a6bbf18 第 6 版）：合格的 s1/s3/s4 分别只剩 2/3/2 条没用，
#: 用掉了池子的 86%/77%/83%；不合格的 s2/s5/s6 各剩 17/18/13 条，只用掉 29%/25%/46%。
#: 两组之间没有重叠，取 6 条 + 六成这两道线把它们分开，且证据池本来就小的章节不会被误伤。
UNUSED_EVIDENCE_FLOOR = 6
LOW_UTILISATION = 0.6


def _shelf_is_stocked(pool_size: int, unused_evidence: int) -> bool:
    """这一节手上是不是还压着一批没写进去的证据。"""
    if pool_size <= 0 or unused_evidence < UNUSED_EVIDENCE_FLOOR:
        return False
    return (pool_size - unused_evidence) / pool_size < LOW_UTILISATION


def repair_route(
    verdict: SectionVerdict,
    *,
    attempted: frozenset[str] = frozenset(),
    pool_size: int = 0,
    unused_evidence: int = 0,
) -> str:
    """按病因决定修哪里，并在同一条路走不通时升级。

    顺序不是随意的，它对应「哪一步坏了就修哪一步」：

    1. 子问题里有整块没答上、而且给的证据本来就撑不住 → **补检索**。这时先重写正文
       是白花钱：证据不在手上，改写只能把缺口写得更委婉。
    2. 证据够但只是在罗列 → **重跑综合**。写作器拿到的 [SYNTHESIS] 指引本身就是空的，
       让它再写一遍还是罗列。
    3. 有超出证据的论断或结论过强 → **重写正文**，把话收回到证据能支撑的范围。

    ``attempted`` 让循环升级：同一条路走过一次而判定没变，就换下一条，而不是
    对着同一个病因重复付钱。

    ``pool_size`` / ``unused_evidence`` 是一道不花钱的闸：判定说「证据薄」有两种可能，
    真的没检索到，和检索到了没写进去。后者再补检索只会把没人用的那堆垒得更高，
    该做的是让写作器把手上的证据用起来。实测 s5 就是这一种——24 条证据用了 6 条。
    """
    if verdict.acceptable:
        return "none"
    stocked = _shelf_is_stocked(pool_size, unused_evidence)
    routes: list[str] = []
    if verdict.unanswered_aspects and verdict.support in {"thin", "unsupported"}:
        routes.append("retrieve")
    if verdict.synthesis_mode == "listed":
        routes.append("resynthesize")
    if verdict.unsupported_claims or verdict.calibration == "overclaimed":
        routes.append("rewrite")
    if verdict.answers_question == "no":
        routes.append("retrieve")
    # 模型自己的病因排在确定性规则之后：它是输入，不是最终裁决。
    routes.append(
        {
            "evidence_gap": "retrieve",
            "synthesis_gap": "resynthesize",
            "writing_gap": "rewrite",
        }.get(verdict.diagnosis, "rewrite")
    )
    routes.append("rewrite")
    for route in routes:
        if route == "retrieve" and stocked:
            continue
        if route not in attempted:
            return route
    return "none"


async def review_section(
    *,
    section_key: str,
    question: str,
    prose: str,
    evidence: list[dict[str, Any]],
    runner: LLMRunner | None,
    language: str = "zh",
) -> SectionVerdict | None:
    """评审一节。拿不到判定返回 None（调用方视为「未评审」，不是「不合格」）。"""
    if not prose.strip() or not question.strip():
        return None
    payload = await _judge(
        runner,
        system_prompt=_SECTION_PROMPT_ZH,
        user_prompt=(
            f"子问题：{question}\n\n"
            f"本节正文：\n{prose[:SECTION_TEXT_CHARS]}\n\n"
            f"本节可用证据（正文只能靠这些支撑）：\n{_evidence_block(evidence)}"
        ),
        metadata={"stage": "section_review", "section": section_key},
    )
    if payload is None:
        return None
    return build_section_verdict(payload, section_key=section_key)


def build_comparability_groups(
    payload: dict[str, Any],
    *,
    evidence: list[dict[str, Any]],
) -> list[ComparabilityGroup]:
    """把模型给的分组过一遍准入规则。

    三条硬规则，全部指向同一件事——**不许凭空造出「文献存在分歧」**：
    1. 只认给定的 evidence_id；剩下不足两条的组整组丢弃。
    2. ``contradictory`` 必须跨 ≥2 篇文献，且组内同时出现 supports 与 opposes；
       否则降级为 ``complementary``——同一篇文献内部的措辞差异不是学界分歧。
    3. ``moderated`` 必须指明闭集里的某个 moderator；说不出来就降级为
       ``incomparable``——「大概是条件不同吧」不是一个可核验的判断。
    """
    rows = {str(item.get("evidence_id")): item for item in evidence if item.get("evidence_id")}
    groups: list[ComparabilityGroup] = []
    for item in payload.get("groups") or []:
        if not isinstance(item, dict):
            continue
        members: list[dict[str, Any]] = []
        for raw in item.get("members") or []:
            if not isinstance(raw, dict):
                continue
            evidence_id = str(raw.get("evidence_id") or "")
            row = rows.get(evidence_id)
            if row is None:
                continue
            members.append(
                {
                    "evidence_id": evidence_id,
                    "work_id": str(row.get("work_id") or ""),
                    "cite_key": row.get("cite_key"),
                    "polarity": _one_of(raw.get("polarity"), POLARITIES, "supports"),
                    "condition": str(raw.get("condition") or "")[:200],
                }
            )
        if len(members) < 2:
            continue
        relation = _one_of(item.get("relation"), RELATIONS, "incomparable")
        moderator = str(item.get("moderator") or "").strip().lower() or None
        if moderator not in MODERATORS:
            moderator = None
        polarities = {member["polarity"] for member in members}
        work_ids = {member["work_id"] for member in members if member["work_id"]}
        if relation == "contradictory" and not (
            len(work_ids) >= 2 and {"supports", "opposes"} <= polarities
        ):
            relation = "complementary"
        if relation == "moderated" and moderator is None:
            relation = "incomparable"
        groups.append(
            ComparabilityGroup(
                claim_topic=str(item.get("claim_topic") or "")[:200],
                relation=relation,
                members=tuple(members),
                moderator=moderator,
                rationale=str(item.get("rationale") or "")[:300],
            )
        )
    return groups


async def compare_findings(
    *,
    question: str,
    evidence: list[dict[str, Any]],
    runner: LLMRunner | None,
) -> list[ComparabilityGroup] | None:
    """判断一个子问题下证据彼此的关系。少于两条证据无从比较，直接返回空。"""
    if len(evidence) < 2 or not question.strip():
        return []
    payload = await _judge(
        runner,
        system_prompt=_COMPARE_PROMPT_ZH,
        user_prompt=(f"子问题：{question}\n\n候选证据：\n{_evidence_block(evidence)}"),
        metadata={"stage": "comparability"},
    )
    if payload is None:
        return None
    return build_comparability_groups(payload, evidence=evidence)


__all__ = [
    "ANSWER_LEVELS",
    "REPAIR_ROUTES",
    "DIAGNOSES",
    "MODERATORS",
    "RELATIONS",
    "REVIEWER_ROLE",
    "ComparabilityGroup",
    "ReviewOutcome",
    "SectionVerdict",
    "build_comparability_groups",
    "build_section_verdict",
    "compare_findings",
    "repair_route",
    "review_section",
]
