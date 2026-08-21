"""SCREEN stage: auditable topic eligibility before expensive full-text work."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from db import list_entries, set_entry_status, upsert_eligibility_decision

from paperforge_worker.context import JobContext

# Isolated from CARDS/EVIDENCE until a human promotes them (N3 / N-RC6).
UNCERTAIN_STATUS = "candidate_uncertain"
# SEARCH can retain more than the repository's UI-oriented default page of
# 500 candidates.  SCREEN is a batch stage and must evaluate the whole pool.
SCREEN_CANDIDATE_LIMIT = 5_000


@dataclass
class ScreenOutcome:
    included: int = 0
    excluded: int = 0
    uncertain: int = 0
    pinned_preserved: int = 0
    selected: int = 0
    budget_limited: int = 0
    #: Kept selected despite an anchor miss, because SEARCH had already picked them.
    uncertain_retained: int = 0
    #: Promoted purely on relevance rank to reach the review corpus floor.
    backfilled: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "included": self.included,
            "excluded": self.excluded,
            "uncertain": self.uncertain,
            "pinned_preserved": self.pinned_preserved,
            "selected": self.selected,
            "budget_limited": self.budget_limited,
            "uncertain_retained": self.uncertain_retained,
            "backfilled": self.backfilled,
            "decisions": self.decisions[:30],
        }


async def screen_eligibility(
    context: JobContext,
    *,
    scope: dict[str, Any],
    focus_questions: list[dict[str, Any]] | None = None,
    preserve_selected: bool = False,
    additional_budget: int = 0,
    deprioritize_work_ids: set[Any] | None = None,
) -> ScreenOutcome:
    """把候选池筛成写作语料。

    ``deprioritize_work_ids`` 是「已经链到目标问题」的文献。定向补证要买的是
    *第二个独立来源*，如果追加预算又被同一批文献占满，补证就等于没做——所以
    这些文献在配额轮询里排到最后。
    """
    criteria = dict(scope.get("eligibility_criteria") or {})
    anchor_groups = _anchor_groups(criteria)
    anchors = _phrases(criteria.get("required_anchor_facets"))
    if not anchor_groups and not anchors:
        anchors = _first_facet(scope)
    exclusions = _phrases(criteria.get("exclusion_domains"))
    outcome = ScreenOutcome()
    # SEARCH 的 top-K 是全自动写作语料预算。SCREEN 可以用结构化条件替换
    # top-K 中不合格的条目，但不能把整个候选池都晋升后交给 CARDS/EVIDENCE。
    # 用户显式 pin/core 的条目不受自动预算限制。
    selected_budget = max(
        0,
        int(
            getattr(
                getattr(context, "settings", None),
                "search_auto_select_top_k",
                30,
            )
        ),
    )
    async with context.session() as session:
        rows = [
            *await list_entries(session, context.project_id, status="selected"),
            *await list_entries(session, context.project_id, status=UNCERTAIN_STATUS),
            # SEARCH keeps non-top-K results as candidates.  A structured
            # eligibility screen is stronger evidence than the retrieval rank,
            # so matching candidates must be able to enter the evidence corpus;
            # otherwise refining the scope can only remove papers, never recover
            # a relevant lower-ranked defense/benchmark paper.
            *await list_entries(
                session,
                context.project_id,
                status="candidate",
                limit=SCREEN_CANDIDATE_LIMIT,
            ),
        ]
        entries = list({work.id: (entry, work) for entry, work in rows}.values())
        records: list[dict[str, Any]] = []
        for entry, work in entries:
            text = " ".join(f"{work.canonical_title}\n{work.abstract or ''}".casefold().split())
            group_hits = {
                group["name"]: [
                    phrase for phrase in group["terms"] if _phrase_in_text(text, phrase)
                ]
                for group in anchor_groups
            }
            grouped_anchor_hit = bool(anchor_groups) and all(group_hits.values())
            legacy_anchor_hit = (
                any(_phrase_in_text(text, phrase) for phrase in anchors) if anchors else False
            )
            anchor_hit = grouped_anchor_hit if anchor_groups else legacy_anchor_hit
            exclusion_hits = [phrase for phrase in exclusions if _phrase_in_text(text, phrase)]
            if exclusion_hits:
                decision, reason = "exclude", "matched exclusion domain"
            elif (anchor_groups or anchors) and anchor_hit:
                decision, reason = "include", "matched required anchor facet"
            elif not anchor_groups and not anchors:
                # No structured criteria → keep selected but mark uncertain for audit.
                decision, reason = "uncertain", "no required anchor facet configured"
            else:
                decision, reason = "uncertain", "no required anchor facet matched"
            criterion_hits = {
                "anchors": [phrase for phrase in anchors if _phrase_in_text(text, phrase)],
                "anchor_groups": group_hits,
                "exclusions": exclusion_hits,
            }
            await upsert_eligibility_decision(
                session,
                project_id=context.project_id,
                work_id=work.id,
                decision=decision,
                criterion_hits=criterion_hits,
                anchor_facet_hit=anchor_hit,
                reason=reason,
            )
            count_attr = f"{decision}d" if decision != "uncertain" else "uncertain"
            setattr(outcome, count_attr, getattr(outcome, count_attr) + 1)
            records.append(
                {
                    "entry": entry,
                    "work": work,
                    "decision": decision,
                    "anchor_facet_hit": anchor_hit,
                    # Hitting *some* anchor group is far weaker than hitting all
                    # of them, but it is the difference between "adjacent to the
                    # topic" and "a heart-failure guideline".  Backfill needs it.
                    "partial_anchor_hit": bool(anchor_groups) and any(group_hits.values()),
                    "criterion_hits": criterion_hits,
                    "pinned": bool(
                        entry.user_pinned or getattr(entry, "literature_role", "general") == "core"
                    ),
                    "focus_scores": _focus_scores(text, focus_questions or []),
                }
            )

        pinned_ids = {record["work"].id for record in records if record["pinned"]}
        preserved_ids = {
            record["work"].id
            for record in records
            if preserve_selected
            and record["entry"].status == "selected"
            and record["decision"] == "include"
        }
        # A literal-anchor miss is not evidence that SEARCH was wrong.  SEARCH
        # picks its top-K with a weighted multi-signal ranker plus an LLM
        # reranker; demoting those picks because a substring test disagreed lets
        # the crudest stage in the pipeline overrule the most informed one.
        # Keep them, and let the exclusion list — which is precise — do the
        # actual rejecting.
        retained_ids = {
            record["work"].id
            for record in records
            if record["decision"] == "uncertain" and record["entry"].status == "selected"
        }
        selection_ids = pinned_ids | preserved_ids | retained_ids
        effective_budget = max(
            selected_budget,
            len(selection_ids) + max(0, int(additional_budget)),
        )
        remaining = max(0, effective_budget - len(selection_ids))
        included = [
            record for record in records if record["decision"] == "include" and not record["pinned"]
        ]
        if focus_questions and remaining:
            # Round-robin question quotas prevent a broad global Top-K from
            # spending the entire corpus budget on the easiest sub-question.
            demoted = deprioritize_work_ids or set()
            pools = [
                sorted(
                    (record for record in included if record["focus_scores"][index] > 0),
                    key=lambda record: (
                        record["work"].id not in demoted,
                        record["focus_scores"][index],
                        float(getattr(record["entry"], "relevance_score", 0.0) or 0.0),
                    ),
                    reverse=True,
                )
                for index in range(len(focus_questions))
            ]
            cursor = 0
            while remaining and any(pools):
                pool = pools[cursor % len(pools)]
                while pool and pool[0]["work"].id in selection_ids:
                    pool.pop(0)
                if pool:
                    selection_ids.add(pool.pop(0)["work"].id)
                    remaining -= 1
                cursor += 1
        if remaining:
            for record in sorted(
                included,
                key=lambda item: float(getattr(item["entry"], "relevance_score", 0.0) or 0.0),
                reverse=True,
            ):
                if record["work"].id in selection_ids:
                    continue
                selection_ids.add(record["work"].id)
                remaining -= 1
                if not remaining:
                    break

        backfilled_ids = _backfill_to_floor(
            records,
            selection_ids,
            settings=getattr(context, "settings", None),
            ceiling=effective_budget,
        )
        outcome.backfilled = len(backfilled_ids)
        for record in records:
            if record["work"].id not in backfilled_ids:
                continue
            # The verdict stays `uncertain` — the anchors really did not all
            # match.  Only the provenance changes, so an audit can always tell a
            # relevance-backfilled work from one the criteria admitted.
            await upsert_eligibility_decision(
                session,
                project_id=context.project_id,
                work_id=record["work"].id,
                decision="uncertain",
                criterion_hits=record["criterion_hits"],
                anchor_facet_hit=record["anchor_facet_hit"],
                reason="backfilled by relevance rank to reach the review corpus floor",
                decided_by="deterministic_backfill",
            )

        for record in records:
            entry = record["entry"]
            work = record["work"]
            decision = record["decision"]
            if record["pinned"]:
                outcome.pinned_preserved += 1
                await set_entry_status(session, entry, "selected")
            elif decision == "exclude":
                await set_entry_status(session, entry, "excluded")
            elif decision == "uncertain":
                # Retained (SEARCH had picked it) or backfilled by rank.  An
                # uncertain work that is in neither set keeps whatever status it
                # arrived with — SCREEN only ever *adds* to the corpus here.
                if work.id in selection_ids:
                    # Two different facts, counted apart: SEARCH's ranker already
                    # wanted this work, or nothing wanted it and rank alone pulled
                    # it in to reach the floor.  Summing them would make
                    # `backfilled` unreadable against the retained total.
                    if work.id not in backfilled_ids:
                        outcome.uncertain_retained += 1
                    await set_entry_status(session, entry, "selected")
            elif work.id in selection_ids:
                await set_entry_status(session, entry, "selected")
            else:
                await set_entry_status(session, entry, "candidate")
                outcome.budget_limited += 1
            outcome.decisions.append(
                {
                    "work_id": str(work.id),
                    "decision": decision,
                    "anchor_facet_hit": record["anchor_facet_hit"],
                }
            )
        outcome.selected = len(selection_ids)
    return outcome


def _backfill_to_floor(
    records: list[dict[str, Any]],
    selection_ids: set[Any],
    *,
    settings: Any,
    ceiling: int,
) -> set[Any]:
    """Top the corpus up to the review floor from the highest-ranked near-misses.

    The anchor gate is a literal-substring test over an LLM's English phrasing,
    so it under-counts badly: one real run matched 9 of 764 works and then spent
    much of the manuscript describing the evidence gap that shortfall created.
    The top-K budget was never the constraint — 21 of its 30 slots went unused.

    Only works that hit *some* anchor group and cleared the relevance floor are
    eligible, and ``selection_ids`` is mutated in place so the caller's status
    write-back sees them.  Returns the ids that were added.
    """
    floor = min(
        int(getattr(settings, "library_backfill_floor", 36) or 0),
        max(0, int(ceiling)),
    )
    if len(selection_ids) >= floor:
        return set()
    minimum = float(getattr(settings, "library_backfill_min_relevance", 0.28) or 0.0)
    eligible = sorted(
        (
            record
            for record in records
            if record["decision"] == "uncertain"
            and record["partial_anchor_hit"]
            and record["work"].id not in selection_ids
            and float(getattr(record["entry"], "relevance_score", 0.0) or 0.0) >= minimum
        ),
        key=lambda record: float(getattr(record["entry"], "relevance_score", 0.0) or 0.0),
        reverse=True,
    )
    added: set[Any] = set()
    for record in eligible:
        if len(selection_ids) >= floor:
            break
        selection_ids.add(record["work"].id)
        added.add(record["work"].id)
    return added


def _focus_scores(text: str, questions: list[dict[str, Any]]) -> list[float]:
    have = _routing_terms(text)
    scores: list[float] = []
    for question in questions:
        aliases = question.get("term_aliases") or {}
        alias_values = (
            [value for values in aliases.values() for value in values]
            if isinstance(aliases, dict)
            else []
        )
        query = " ".join(
            str(value or "")
            for value in (
                question.get("search_query"),
                question.get("text"),
                *alias_values,
            )
        )
        wanted = _routing_terms(query)
        scores.append(len(have & wanted) / max(1, len(wanted)))
    return scores


def _routing_terms(text: str) -> set[str]:
    latin = {
        token
        for token in re.findall(r"[a-z][a-z0-9-]{1,}", text.casefold())
        if token not in {"the", "and", "for", "with", "from", "what", "how"}
    }
    runs = re.findall(r"[\u3400-\u9fff]+", text)
    return latin | {run[index : index + 2] for run in runs for index in range(len(run) - 1)}


def _phrases(values: Any) -> list[str]:
    return [" ".join(str(value).casefold().split()) for value in values or [] if str(value).strip()]


def _phrase_in_text(text: str, phrase: str) -> bool:
    """Match short ASCII acronyms as tokens, not arbitrary substrings.

    In particular, the eligibility alias ``AI`` must not match ``domain``,
    ``chain`` or ``training`` and accidentally admit most of a search corpus.

    Longer phrases first try plain substring containment, so every match the
    literal gate used to find is still found.  A miss then retries against a
    stemmed form of both sides: SCOPE writes anchors like
    ``"sequential recommendation"`` while the literature says ``"sequential
    recommenders"``, and a raw substring test scores that as off-topic.  The
    stemmer is deliberately crude — it only has to mangle both sides the same
    way, not be linguistically right.
    """
    if re.fullmatch(r"[a-z0-9]+", phrase) and len(phrase) <= 3:
        return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None
    if phrase in text:
        return True
    needle = _stem_text(phrase)
    if not needle:
        return False
    haystack = _stem_text(text)
    if re.search(r"[a-z0-9]", needle):
        # Token-anchored so a stemmed "system" cannot match inside "systemic".
        return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None
    # CJK anchors have no word boundaries to anchor against.
    return needle in haystack


#: Folded away before matching, longest first.  ``ers`` precedes ``er`` precedes
#: ``s`` so "recommenders" and "recommender" both land on "recommend".
_INFLECTIONS = ("ations", "ation", "ers", "er", "ing", "es", "s")
#: Never strip a word down past this, or "papers" would become "pap".
_MIN_STEM = 4


@lru_cache(maxsize=4096)
def _stem_text(text: str) -> str:
    """Fold punctuation, hyphenation and English inflection out of ``text``.

    Cached because SCREEN tests every anchor term against the same title +
    abstract blob, and the pool runs to thousands of works.
    """
    cleaned = re.sub(r"[^0-9a-z\u3400-\u9fff]+", " ", text.casefold())
    return " ".join(_stem_word(word) for word in cleaned.split())


def _stem_word(word: str) -> str:
    for suffix in _INFLECTIONS:
        if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM:
            word = word[: -len(suffix)]
            break
    # "sequence"/"sequences" and "study"/"studies" only converge after the
    # stem's own trailing vowel is normalised too.
    if len(word) - 1 >= _MIN_STEM:
        if word.endswith("e"):
            return word[:-1]
        if word.endswith("y"):
            return f"{word[:-1]}i"
    return word


def _first_facet(scope: dict[str, Any]) -> list[str]:
    groups = scope.get("keyword_groups") or []
    if not groups or not isinstance(groups[0], dict):
        return []
    return _phrases(groups[0].get("keywords"))


def _anchor_groups(criteria: dict[str, Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for index, item in enumerate(criteria.get("required_anchor_groups") or []):
        if not isinstance(item, dict):
            continue
        terms = _phrases(item.get("terms"))
        if not terms:
            continue
        groups.append(
            {
                "name": " ".join(str(item.get("name") or f"group_{index + 1}").split()),
                "terms": terms,
            }
        )
    return groups


__all__ = ["ScreenOutcome", "screen_eligibility", "UNCERTAIN_STATUS"]
