"""Five-arm retrieval replay; existing matrix links are proxy labels, never human gold."""

import argparse
import asyncio
import json
import math
import time
import uuid
from pathlib import Path

from db import list_question_evidence_links, list_research_questions
from db.session import make_engine, make_session_factory
from paperforge_worker.pipelines.qmatrix import _diverse_ranked_candidates, _rank_candidates
from retrieval.search import index_project, search, visible_evidence

from evals.agent_writing.run import evaluation_url


def metrics(predicted, relevant):
    hits = [int(key in relevant) for key in predicted[:10]]
    ideal = sum(1 / math.log2(i + 2) for i in range(min(10, len(relevant))))
    return {
        "proxy_recall_at_10": sum(hits) / len(relevant) if relevant else None,
        "proxy_ndcg_at_10": sum(hit / math.log2(i + 2) for i, hit in enumerate(hits)) / ideal
        if ideal
        else None,
    }


async def run(projects, output):
    output.mkdir(parents=True, exist_ok=False)
    engine = make_engine(evaluation_url())
    factory = make_session_factory(engine)
    results = []
    try:
        for project_id in projects:
            indexing = await index_project(factory, project_id)
            async with factory() as session:
                questions = await list_research_questions(session, project_id, kind="sub")
                links = await list_question_evidence_links(session, project_id)
                corpus = list((await session.scalars(visible_evidence(project_id))).all())
                for question in questions[:10]:
                    relevant = {
                        str(link.evidence_unit_id)
                        for link in links
                        if link.research_question_id == question.id
                        and link.stance in {"supports", "conditional", "contradicts"}
                    }
                    for mode in ("legacy", "lexical", "vector", "hybrid", "hybrid_rerank"):
                        started = time.monotonic()
                        if mode == "legacy":
                            ranked = _diverse_ranked_candidates(
                                _rank_candidates(
                                    question,
                                    corpus,
                                    expected_kinds=set(question.expected_evidence_kinds_json or []),
                                    task_id=question.task_id,
                                )
                            )
                            found = {
                                "results": [
                                    {"evidence_id": str(unit.id)} for unit, _ in ranked[:12]
                                ],
                                "warnings": [],
                                "vector_backend": "none",
                                "reranked": False,
                            }
                        else:
                            found = await search(session, project_id, question.text, mode=mode)
                        results.append(
                            {
                                "project_id": str(project_id),
                                "question_id": str(question.id),
                                "mode": mode,
                                "seconds": time.monotonic() - started,
                                **metrics([r["evidence_id"] for r in found["results"]], relevant),
                                "warnings": found["warnings"],
                                "reranked": found["reranked"],
                                "vector_backend": found["vector_backend"],
                                "indexing": indexing,
                            }
                        )
                        (output / "results.json").write_text(json.dumps(results, indent=2))
        summary = {
            "runs": len(results),
            "paid_model_cost_cny": 0,
            "human_gold": False,
            "release_eligible": False,
            "limitation": "Existing classifier links are incomplete proxy labels; "
            "cold/warm and human relevance comparisons remain necessary.",
        }
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
        return summary
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", action="append", type=uuid.UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.project_id, args.output))))
