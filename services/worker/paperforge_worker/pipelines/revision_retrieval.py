"""Bounded supplemental retrieval with separately durable step results."""

import uuid

from db import get_cards, get_project, list_entries, list_evidence_units, list_research_questions

from paperforge_worker.pipelines.revision_recovery import step


async def supplement(context, *, language, question_ids):
    from paperforge_worker import worker as w
    from paperforge_worker.pipelines.fulltext import load_persisted_fulltext_sources

    async def prepare():
        async with context.session() as session:
            project = await get_project(session, context.project_id)
            questions = await list_research_questions(session, context.project_id, kind="sub")
            selected = await list_entries(session, context.project_id, status="selected")
            linked = await w._question_linked_work_ids(session, context.project_id)
        gaps = w._classify_evidence_gaps(questions, None, question_ids=question_ids)
        repair_questions = [
            {"text": q.text, "search_query": q.search_query, "term_aliases": q.term_aliases_json}
            for q, _ in gaps
        ]
        scope = {**(project.scope_json or {}), "sub_questions": repair_questions}
        queries, remaining = [], 12
        for kind in ("needs_second_source", "no_candidates"):
            batch = w._gap_repair_queries([q for q, k in gaps if k == kind], kind)[: remaining // 3]
            remaining -= len(batch) * 3
            if batch:
                query_scope = (
                    w._widen_time_range(scope)
                    if kind == "no_candidates" and scope.get("review_style") != "systematic"
                    else scope
                )
                queries.append({"queries": batch, "scope": query_scope})
        return {
            "scope": scope,
            "questions": repair_questions,
            "queries": queries,
            "selected_before": [str(work.id) for _, work in selected],
            "deprioritized": sorted(
                {
                    str(work_id)
                    for q, kind in gaps
                    if kind == "needs_second_source"
                    for work_id in linked.get(q.id, set())
                }
            ),
        }

    plan = await step(context, "prepare", prepare)
    if not plan["questions"]:
        return
    for i, query in enumerate(plan["queries"]):

        async def search(query=query):
            return await w.run_search(
                context,
                scope=query["scope"],
                query_texts=query["queries"],
                providers=["openalex", "europe_pmc", "crossref"],
                limit_per_provider=min(12, context.settings.search_limit_per_provider),
            )

        await step(context, f"search:{i}", search)

    async def screen():
        result = await w.screen_eligibility(
            context,
            scope=plan["scope"],
            focus_questions=plan["questions"],
            preserve_selected=True,
            additional_budget=min(15, max(4, len(plan["questions"]) * 3)),
            deprioritize_work_ids={uuid.UUID(k) for k in plan["deprioritized"]},
        )
        await w._assign_keys(context)
        return result

    await step(context, "screen", screen)

    async def fulltext():
        outcome, _ = await w.acquire_fulltexts(
            context, max_works=context.settings.fulltext_max_works
        )
        return outcome

    await step(context, "fulltext", fulltext)
    async with context.session() as session:
        selected = {
            work.id
            for _, work in await list_entries(session, context.project_id, status="selected")
        }
    sources = await load_persisted_fulltext_sources(context, list(selected))
    fulltexts = {key: value.text for key, value in sources.items()}

    async def processing():
        async with context.session() as session:
            cards = await get_cards(session, context.project_id)
            evidence = await list_evidence_units(session, context.project_id)
        ids = w._repair_processing_ids(
            selected_work_ids=selected,
            new_work_ids=selected - {uuid.UUID(k) for k in plan["selected_before"]},
            fulltext_work_ids=set(fulltexts),
            card_work_ids=set(cards),
            eligible_evidence_work_ids={
                u.work_id
                for u in evidence
                if u.grade in {"A_located_structured", "B_located_prose", "C_fulltext_unlocated"}
            },
            checkpoint_work_ids=None,
        )
        return sorted(str(k) for k in ids)

    work_ids = [uuid.UUID(k) for k in await step(context, "processing", processing)]
    await step(
        context,
        "cards",
        lambda: w.generate_cards(
            context, language=language, fulltexts=fulltexts, work_ids=work_ids
        ),
    )
    await step(
        context,
        "evidence",
        lambda: w.extract_evidence_units(context, fulltexts=fulltexts, work_ids=work_ids),
    )
    await step(
        context, "matrix", lambda: w.build_question_evidence_matrix(context, language=language)
    )
    await step(context, "synthesis", lambda: w.synthesize_questions(context))
