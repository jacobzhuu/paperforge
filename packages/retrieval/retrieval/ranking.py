"""Deterministic bilingual lexical retrieval and reciprocal-rank fusion."""

import math
import re
from collections import Counter, defaultdict

VERSION = "evidence-hybrid-v1"
STOPWORDS = frozenset("a an the of in on and or to for with is are was were by from".split())


def tokenize(text: str) -> list[str]:
    tokens = []
    for token in re.findall(r"[a-zA-Z0-9]+(?:[-_][a-zA-Z0-9]+)*|[\u4e00-\u9fff]+", text.lower()):
        if "\u4e00" <= token[0] <= "\u9fff":
            tokens.extend(token[i : i + 2] for i in range(max(1, len(token) - 1)))
        elif token not in STOPWORDS:
            tokens.append(token)
    return tokens


def lexical_rank(query: str, documents: dict[str, str], limit: int = 40) -> list[tuple[str, float]]:
    query_terms = set(tokenize(query))
    counts = {key: Counter(tokenize(value)) for key, value in documents.items()}
    lengths = {key: sum(value.values()) for key, value in counts.items()}
    average = sum(lengths.values()) / max(len(counts), 1) or 1
    frequency = Counter(term for row in counts.values() for term in query_terms if term in row)
    scores = {}
    for key, row in counts.items():
        score = 0.0
        for term in query_terms:
            tf = row[term]
            if tf:
                idf = math.log(1 + (len(counts) - frequency[term] + 0.5) / (frequency[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * lengths[key] / average))
        if score:
            scores[key] = score
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]


def rrf(*rankings: list[tuple[str, float]], limit: int = 40) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        seen = set()
        for position, (key, _) in enumerate(ranking, 1):
            if key not in seen:
                scores[key] += 1 / (60 + position)
                seen.add(key)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left or not all(math.isfinite(v) for v in left + right):
        raise ValueError("invalid embedding")
    denominator = math.sqrt(sum(v * v for v in left) * sum(v * v for v in right))
    if denominator == 0:
        raise ValueError("zero embedding")
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator
