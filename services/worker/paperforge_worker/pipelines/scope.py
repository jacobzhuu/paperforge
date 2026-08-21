"""SCOPE 阶段：主题 → 研究范围 + 关键词矩阵 + 时间窗 + 子主题。

改造自 DeepSearch literature_review/protocol_generation.py（1159 行）：
保留「LLM 起草 + 确定性回退 + 严格 JSON schema 校验」模式；
去掉 PICO/PECO 强制框架、协议锁定(locked)语义、版本裁决。产物可编辑、可随时重生成。

Draft-first：LLM 不可用/输出不合法时，确定性回退保证 SCOPE 永远有产物。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from llm_runtime import LLMRunner

MAX_KEYWORD_GROUPS = 6
MAX_KEYWORDS_PER_GROUP = 8
MAX_SUBTOPICS = 8
MAX_SUBQUESTIONS = 6
DEFAULT_YEAR_SPAN = 6

_SYSTEM_PROMPT_ZH = """你是科研文献调研的规划助手。根据论文主题输出检索范围规划。
只输出 JSON，不要解释。字段：
{
  "research_question": "一句话研究问题",
  "sub_questions": [
    {
      "text": "可由证据回答的子问题",
      "search_query": "compact English retrieval query for this sub-question",
      "task_id": "optional task slug if known",
      "comparison_dimensions": ["任务", "数据集", "评价指标", "样本/规模"],
      "expected_evidence_kinds": ["experimental_fact", "author_conclusion"],
      "term_aliases": {"投毒攻击": ["poisoning attack", "data poisoning"]}
    }
  ],
  "scope_summary": "2-3 句范围说明（包含/排除什么）",
  "keyword_groups": [{"name": "概念名", "keywords": ["term1", "term2"]}],
  "eligibility_criteria": {
    "required_anchor_groups": [
      {"name": "研究领域", "terms": ["domain synonym 1", "domain synonym 2"]},
      {"name": "核心主题", "terms": ["topic synonym 1", "topic synonym 2"]}
    ],
    "exclusion_domains": ["明确排除的英文领域词"]
  },
  "subtopics": ["子主题1", "子主题2"],
  "time_range": {"start_year": 2019, "end_year": 2025},
  "inclusion_notes": ["纳入偏好"],
  "exclusion_notes": ["排除偏好"]
}
要求：keyword_groups 覆盖主题的正交概念面（方法/任务/领域/评价），每组给同义词与常见缩写；
把核心问题拆成 3-6 个互不重复、可由文献证据回答的子问题，并为每个子问题列出横向比较维度；
**每个子问题必须给出纯英文 search_query**（供跨语言证据路由与检索），comparison_dimensions
中尽量包含英文数据集名/指标名（如 MovieLens、NDCG、HR@K）；
不要编造不存在的专有名词；时间窗按领域节奏给出合理区间。

**keywords 必须全部是英文检索词**（name 与其余叙述字段用中文）。
eligibility_criteria 至少给出“研究领域”和“核心主题”两组英文锚点；组内任一词命中即可，
但两组都必须命中才可自动纳入。锚点必须有区分度：用 poisoning attack、sequential
recommendation 这样的专指短语，不要用 robustness、defense、performance、model 这类
几乎每篇论文摘要都有的通用词——一个通用词会让整组形同虚设。
exclusion_domains 只列明确离题领域。
检索面向的是 OpenAlex / arXiv / Crossref / Europe PMC，
它们只索引英文题录：中文检索词几乎必然零召回，或召回完全无关的中文期刊文献。
请把主题翻译成该领域论文实际使用的英文术语，例如
「序列推荐系统的投毒攻击」→ "sequential recommendation"、"poisoning attack"、
"shilling attack"、"data poisoning"、"recommender system robustness"。"""

_SYSTEM_PROMPT_EN = """You plan literature searches for research papers. Given a paper topic,
output a search scope plan. Output JSON only, no commentary. Fields:
{
  "research_question": "one-sentence research question",
  "sub_questions": [
    {
      "text": "an evidence-answerable sub-question",
      "search_query": "compact English retrieval query for this sub-question",
      "task_id": "optional task slug if known",
      "comparison_dimensions": ["task", "dataset", "metric", "sample/scale"],
      "expected_evidence_kinds": ["experimental_fact", "author_conclusion"],
      "term_aliases": {"投毒攻击": ["poisoning attack", "data poisoning"]}
    }
  ],
  "scope_summary": "2-3 sentences on what is in and out of scope",
  "keyword_groups": [{"name": "concept", "keywords": ["synonym1", "synonym2"]}],
  "eligibility_criteria": {
    "required_anchor_groups": [
      {"name": "domain", "terms": ["domain synonym 1", "domain synonym 2"]},
      {"name": "topic", "terms": ["topic synonym 1", "topic synonym 2"]}
    ],
    "exclusion_domains": ["explicitly excluded domain term"]
  },
  "subtopics": ["subtopic1", "subtopic2"],
  "time_range": {"start_year": 2019, "end_year": 2025},
  "inclusion_notes": ["inclusion preference"],
  "exclusion_notes": ["exclusion preference"]
}
Requirements: keyword_groups must cover orthogonal facets (method/task/domain/evaluation)
with synonyms and common abbreviations; never invent proper nouns that do not exist.
Decompose the core question into 3-6 non-overlapping, evidence-answerable sub-questions and
list the comparison dimensions needed for each.
**Every sub-question MUST include a pure-English search_query** used for retrieval and
cross-language evidence routing; put English dataset/metric names in comparison_dimensions.

**Every keyword must be English**, even when the topic is written in another language.
Provide at least two eligibility anchor groups (domain and topic). A work must match at
least one English term in every group to be auto-included. Anchor terms must discriminate:
use specific phrases such as "poisoning attack" or "sequential recommendation", never
generic words such as "robustness", "defense", "performance" or "model" that appear in
almost every abstract — one of those makes its whole group a no-op.
The providers behind this plan (OpenAlex / arXiv / Crossref / Europe PMC) index
English metadata only, so non-English keywords either return
nothing or return unrelated foreign-language articles. Translate the topic into the
English terminology the field actually publishes under."""

_STOPWORDS_EN = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "for",
        "and",
        "or",
        "to",
        "in",
        "on",
        "with",
        "using",
        "via",
        "based",
        "study",
        "review",
        "survey",
        "analysis",
        "research",
        "paper",
        "towards",
        "toward",
        "about",
    }
)
_STOPWORDS_ZH = frozenset({"的", "了", "与", "和", "及", "研究", "综述", "分析", "方法", "基于"})

_CJK_CHAR_RE = re.compile(r"[一-鿿]+")
_LATIN_CHAR_RE = re.compile(r"[A-Za-z]")
# 中文虚词：在这些字处断开长串，得到可检索的概念片段。
_CJK_GLUE_RE = re.compile(r"[的了与和及在中对于之或等及以并而且把被从向为其所]")
# 通用后缀对检索没有区分度，剥掉后剩下的才是真正的概念词。
_CJK_GENERIC_SUFFIXES = ("研究综述", "综述", "研究", "分析", "方法", "技术", "进展", "应用")


async def generate_scope(
    topic: str,
    *,
    language: str = "en",
    paper_type: str = "review",
    runner: LLMRunner | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """生成研究范围。永远返回合法 scope（LLM 失败即走确定性回退）。"""
    fallback = deterministic_scope(topic, language=language, now=now)
    if runner is None or not runner.enabled:
        return fallback

    system_prompt = _SYSTEM_PROMPT_ZH if language == "zh" else _SYSTEM_PROMPT_EN
    user_prompt = (
        f"论文类型: {paper_type}\n主题: {topic}\n输出语言: 中文"
        if language == "zh"
        else f"Paper type: {paper_type}\nTopic: {topic}\nOutput language: English"
    )
    result = await runner.agenerate_json(
        "planner",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        # 推理型 planner 模型（deepseek-v4-pro 等）把思维链算进 max_tokens，
        # 1500 只够想不够写：JSON 会在中途断掉，整个 SCOPE 静默退回确定性回退。
        max_output_tokens=3000,
        temperature=0.2,
        metadata={"stage": "scope"},
    )
    if not result.ok or not isinstance(result.value, dict):
        return {**fallback, "generator": "deterministic_fallback", "fallback_reason": result.error}
    normalized = normalize_scope(result.value, topic=topic, language=language, now=now)
    normalized["generator"] = f"llm:{result.model or runner.model_for('planner')}"
    return normalized


def normalize_scope(
    raw: dict[str, Any],
    *,
    topic: str,
    language: str = "en",
    now: datetime | None = None,
) -> dict[str, Any]:
    """把 LLM 原始输出收敛到严格 schema；缺字段用确定性回退补齐。"""
    fallback = deterministic_scope(topic, language=language, now=now)
    groups: list[dict[str, Any]] = []
    for item in _as_list(raw.get("keyword_groups"))[:MAX_KEYWORD_GROUPS]:
        if isinstance(item, dict):
            name = _clean_text(item.get("name"))
            keywords = [
                kw for kw in (_clean_text(k) for k in _as_list(item.get("keywords"))) if kw
            ][:MAX_KEYWORDS_PER_GROUP]
        elif isinstance(item, str):
            name = _clean_text(item)
            keywords = [name] if name else []
        else:
            continue
        if name and keywords:
            groups.append({"name": name, "keywords": keywords})
    if not groups:
        groups = fallback["keyword_groups"]

    subtopics = [text for text in (_clean_text(s) for s in _as_list(raw.get("subtopics"))) if text][
        :MAX_SUBTOPICS
    ]
    sub_questions = _normalize_sub_questions(
        raw.get("sub_questions"),
        fallback=fallback["sub_questions"],
    )

    return {
        "topic": topic,
        "language": language,
        "research_question": (
            _clean_text(raw.get("research_question")) or fallback["research_question"]
        ),
        "scope_summary": _clean_text(raw.get("scope_summary")) or fallback["scope_summary"],
        "keyword_groups": groups,
        "eligibility_criteria": _normalize_eligibility_criteria(
            raw.get("eligibility_criteria"),
            fallback=fallback.get("eligibility_criteria"),
        ),
        "subtopics": subtopics or fallback["subtopics"],
        "sub_questions": sub_questions,
        "time_range": _normalize_time_range(raw.get("time_range"), fallback["time_range"]),
        "inclusion_notes": [
            text for text in (_clean_text(s) for s in _as_list(raw.get("inclusion_notes"))) if text
        ],
        "exclusion_notes": [
            text for text in (_clean_text(s) for s in _as_list(raw.get("exclusion_notes"))) if text
        ],
        "generated_at": (now or datetime.now(UTC)).isoformat(),
    }


def deterministic_scope(
    topic: str,
    *,
    language: str = "en",
    now: datetime | None = None,
) -> dict[str, Any]:
    """不依赖 LLM 的确定性回退：从主题切词构造关键词矩阵与近 N 年时间窗。"""
    clock = now or datetime.now(UTC)
    end_year = clock.year
    start_year = end_year - DEFAULT_YEAR_SPAN + 1
    terms = topic_terms(topic)
    groups = [{"name": term, "keywords": [term]} for term in terms[:MAX_KEYWORD_GROUPS]]
    if not groups:
        groups = [{"name": topic.strip() or "topic", "keywords": [topic.strip() or "topic"]}]
    if language == "zh":
        question = f"{topic} 的研究现状与关键进展是什么？"
        summary = f"围绕「{topic}」检索 {start_year}–{end_year} 年的同行评议文献与预印本。"
    else:
        question = f"What is the state of the art in {topic}?"
        summary = (
            f"Search peer-reviewed literature and preprints on {topic} "
            f"published between {start_year} and {end_year}."
        )
    subtopics = terms[:MAX_SUBTOPICS] or [topic]
    return {
        "topic": topic,
        "language": language,
        "research_question": question,
        "scope_summary": summary,
        "keyword_groups": groups,
        "eligibility_criteria": None,
        "subtopics": subtopics,
        "sub_questions": [
            {
                "text": (
                    f"{subtopic} 对核心研究问题提供了哪些证据？"
                    if language == "zh"
                    else f"What evidence does {subtopic} provide for the core question?"
                ),
                "comparison_dimensions": (
                    ["任务", "数据集", "评价指标", "样本或规模"]
                    if language == "zh"
                    else ["task", "dataset", "metric", "sample or scale"]
                ),
                "expected_evidence_kinds": [
                    "experimental_fact",
                    "author_conclusion",
                ],
            }
            for subtopic in subtopics[:MAX_SUBQUESTIONS]
        ],
        "time_range": {"start_year": start_year, "end_year": end_year},
        "inclusion_notes": [],
        "exclusion_notes": [],
        "generator": "deterministic",
        "generated_at": clock.isoformat(),
    }


#: Single words that appear in most machine-learning abstracts regardless of
#: subject.  As an anchor term one of these silently turns its whole group into
#: a pass: a review of *attacks* on sequential recommendation admitted a paper on
#: multimodal LLM recommendation because "robustness" occurred in its abstract.
#: Anchors have to discriminate; these do not.
_GENERIC_ANCHOR_TERMS = frozenset(
    {
        "accuracy",
        "algorithm",
        "analysis",
        "application",
        "architecture",
        "baseline",
        "benchmark",
        "dataset",
        "deep learning",
        "defense",
        "efficiency",
        "evaluation",
        "experiment",
        "framework",
        "machine learning",
        "method",
        "model",
        "network",
        "neural network",
        "optimization",
        "performance",
        "robustness",
        "security",
        "system",
        "training",
    }
)


def _is_generic_anchor(term: str) -> bool:
    return term.casefold().strip() in _GENERIC_ANCHOR_TERMS


def _normalize_eligibility_criteria(
    value: Any,
    *,
    fallback: Any = None,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return fallback if isinstance(fallback, dict) else None
    groups: list[dict[str, Any]] = []
    for item in _as_list(value.get("required_anchor_groups"))[:4]:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"))
        terms = [
            term
            for raw in _as_list(item.get("terms"))[:12]
            if (term := _clean_text(raw))
            and _is_english_query(term)
            and not _is_generic_anchor(term)
        ]
        if name and terms:
            groups.append({"name": name, "terms": list(dict.fromkeys(terms))})
    exclusions = [
        term
        for raw in _as_list(value.get("exclusion_domains"))[:24]
        if (term := _clean_text(raw)) and _is_english_query(term)
    ]
    if len(groups) < 2:
        return fallback if isinstance(fallback, dict) else None
    return {
        "required_anchor_groups": groups,
        "exclusion_domains": list(dict.fromkeys(exclusions)),
    }


def topic_terms(topic: str) -> list[str]:
    """切出主题中的实义词（中英双语），保持出现顺序且去重。"""
    text = (topic or "").strip()
    if not text:
        return []
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9\-]+|[一-鿿]+", text):
        for term in _split_cjk_run(token) if _is_cjk(token) else [token]:
            lowered = term.lower()
            if lowered in _STOPWORDS_EN or term in _STOPWORDS_ZH:
                continue
            if lowered in seen:
                continue
            seen.add(lowered)
            terms.append(term)
    return terms


def _normalize_sub_questions(value: Any, *, fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for item in _as_list(value)[:MAX_SUBQUESTIONS]:
        if isinstance(item, str):
            text = _clean_text(item)
            dimensions: list[str] = []
            evidence_kinds: list[str] = []
            search_query = ""
            task_id = ""
            term_aliases: Any = None
        elif isinstance(item, dict):
            text = _clean_text(item.get("text"))
            dimensions = _clean_string_list(item.get("comparison_dimensions"), limit=8)
            evidence_kinds = [
                kind
                for kind in _clean_string_list(item.get("expected_evidence_kinds"), limit=5)
                if kind
                in {
                    "experimental_fact",
                    "theoretical_derivation",
                    "author_conclusion",
                    "review_restatement",
                    "model_inference",
                }
            ]
            search_query = _clean_text(item.get("search_query") or item.get("english_query"))
            if search_query and not _is_english_query(search_query):
                search_query = ""
            task_id = _clean_text(item.get("task_id"))
            term_aliases = item.get("term_aliases") or item.get("term_aliases_json")
        else:
            continue
        if text:
            entry: dict[str, Any] = {
                "text": text,
                "comparison_dimensions": dimensions,
                "expected_evidence_kinds": evidence_kinds,
            }
            if search_query:
                entry["search_query"] = search_query
            if task_id:
                entry["task_id"] = task_id
            if term_aliases:
                entry["term_aliases"] = term_aliases
            questions.append(entry)
    return questions or fallback


def _clean_string_list(value: Any, *, limit: int) -> list[str]:
    return list(
        dict.fromkeys(text for item in _as_list(value)[:limit] if (text := _clean_text(item)))
    )


def _is_cjk(token: str) -> bool:
    return bool(token) and _CJK_CHAR_RE.fullmatch(token) is not None


def _split_cjk_run(run: str) -> list[str]:
    """把中文长串切成概念片段。

    中文不带空格，整串当一个词会得到「序列推荐系统的投毒攻击」这种没有任何检索源
    命中的巨型 token。这里在虚词处断开（的/在/中/与……）并剥掉「研究/综述」这类
    对检索毫无区分度的通用后缀，得到 ["序列推荐系统", "投毒攻击"]。
    不追求分词器级别的准确度，只求把正交概念面拆开。
    """
    segments = [segment for segment in _CJK_GLUE_RE.split(run) if len(segment) >= 2]
    trimmed: list[str] = []
    for segment in segments:
        while len(segment) > 2 and segment.endswith(_CJK_GENERIC_SUFFIXES):
            for suffix in _CJK_GENERIC_SUFFIXES:
                if segment.endswith(suffix) and len(segment) - len(suffix) >= 2:
                    segment = segment[: -len(suffix)]
                    break
            else:
                break
        trimmed.append(segment)
    return trimmed or ([run] if len(run) >= 2 else [])


def search_queries(
    scope: dict[str, Any],
    *,
    max_queries: int = 12,
    sub_question_queries: list[str] | None = None,
) -> list[str]:
    """由 scope 生成检索式：主查询 + 各概念面组合（provider 侧还会各自净化语法）。

    主查询优先用英文：五个检索源都只索引英文题录，把中文标题原样发出去要么零召回，
    要么召回中文期刊里字面碰巧重合的无关文献（曾经用「序列推荐系统的投毒攻击」
    检索，Europe PMC 返回的全是中文医学论文）。因此当主题不含拉丁字母时，
    改用 scope 里的英文关键词组当主查询，中文原标题只在没有英文关键词时才兜底。

    ``sub_question_queries`` 是 ``research_question`` 表里的检索面（问题的单一真源）。
    调用方拿得到 DB 就必须传：只读 ``scope_json['sub_questions']`` 会让用户在问题
    工作台上的修改对检索完全不起作用。没有问题树的管线（original 论文、纯单测）
    才回落到 scope 里的副本。
    """
    topic = _clean_text(scope.get("topic")) or ""
    groups = [group for group in _as_list(scope.get("keyword_groups")) if isinstance(group, dict)]
    lead = _primary_query(topic, groups)
    queries: list[str] = []
    if lead:
        queries.append(lead)
    if groups:
        joined = " AND ".join(
            "(" + " OR ".join(f'"{kw}"' for kw in _english_keywords(group)[:4]) + ")"
            for group in groups[:3]
            if _english_keywords(group)
        )
        if joined:
            queries.append(joined)
    # Each decomposition question gets a compact English query.  Do not send
    # mixed CJK/Latin strings to providers that only index English metadata.
    if sub_question_queries is None:
        sub_question_queries = [
            _clean_text(item.get("search_query") or item.get("english_query") or "")
            for item in _as_list(scope.get("sub_questions"))
            if isinstance(item, dict)
        ]
    for text in sub_question_queries:
        cleaned = _clean_text(text)
        if _is_english_query(cleaned):
            queries.append(cleaned)
    for group in groups:
        keywords = _english_keywords(group)
        if len(keywords) >= 2:
            queries.append(" ".join(keywords[:2]))
    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped[:max_queries]


def _has_latin(text: str) -> bool:
    return bool(_LATIN_CHAR_RE.search(text or ""))


def _is_english_query(text: str) -> bool:
    return bool(text and _has_latin(text) and not _CJK_CHAR_RE.search(text))


def _english_keywords(group: dict[str, Any]) -> list[str]:
    return [
        text
        for keyword in _as_list(group.get("keywords"))
        if _is_english_query(text := _clean_text(keyword))
    ]


def _primary_query(topic: str, groups: list[dict[str, Any]]) -> str:
    """主查询：纯英文主题直接用；含中文的主题改用英文关键词组拼装。"""
    if topic and not _CJK_CHAR_RE.search(topic):
        return topic
    leads: list[str] = []
    for group in groups:
        for keyword in _as_list(group.get("keywords")):
            text = _clean_text(keyword)
            if text and _has_latin(text):
                leads.append(text)
                break
        if len(leads) >= 3:
            break
    # 一个英文关键词都没有（LLM 不可用且主题为中文）时只能回落到原标题：
    # 召回大概率很差，但 SEARCH 阶段仍有产物，且低分候选不会被自动入库。
    return " ".join(leads) or topic


def scope_filters(scope: dict[str, Any]) -> dict[str, Any]:
    """把 scope 的时间窗翻译为 provider 通用 filters。"""
    time_range = scope.get("time_range")
    if not isinstance(time_range, dict):
        return {}
    bounds: dict[str, Any] = {}
    if isinstance(time_range.get("start_year"), int):
        bounds["start_year"] = time_range["start_year"]
    if isinstance(time_range.get("end_year"), int):
        bounds["end_year"] = time_range["end_year"]
    return {"time_range": bounds} if bounds else {}


def _normalize_time_range(raw: Any, fallback: dict[str, int]) -> dict[str, int]:
    if not isinstance(raw, dict):
        return fallback
    start = raw.get("start_year")
    end = raw.get("end_year")
    start = start if isinstance(start, int) and 1800 <= start <= 2200 else fallback["start_year"]
    end = end if isinstance(end, int) and 1800 <= end <= 2200 else fallback["end_year"]
    if start > end:
        start, end = end, start
    return {"start_year": start, "end_year": end}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())
