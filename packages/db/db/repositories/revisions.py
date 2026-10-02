"""Explicit candidate selection is scoped to one async execution context."""

import copy
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import select

from db.models.paper import CitationUsage, PaperDocument, PaperProject

_selected: ContextVar[tuple | None] = ContextVar("revision_document", default=None)


def selected_document(project_id):
    selected = _selected.get()
    return selected[1] if selected and selected[0] == project_id else None


@contextmanager
def revision_document(project_id, document_id):
    token = _selected.set((project_id, document_id))
    try:
        yield
    finally:
        _selected.reset(token)


async def clone_document(session, source, *, status="revision_candidate"):
    from db.repositories.documents import (
        create_document,
        list_sections,
        replace_citation_usage,
        upsert_section,
    )

    await session.scalar(
        select(PaperProject.id).where(PaperProject.id == source.project_id).with_for_update()
    )
    await session.scalar(
        select(PaperDocument.id).where(PaperDocument.id == source.id).with_for_update()
    )
    candidate = await create_document(
        session, project_id=source.project_id, outline_id=source.outline_id, status=status
    )
    candidate.writing_state_json = copy.deepcopy(source.writing_state_json)
    for row in await list_sections(session, source.id):
        created = await upsert_section(
            session,
            document_id=candidate.id,
            section_key=row.section_key,
            title=row.title,
            order_no=row.order_no,
            parent_key=row.parent_key,
            body_ir=copy.deepcopy(row.body_ir_json),
            cite_keys=copy.deepcopy(row.cite_keys_json),
            asset_refs=copy.deepcopy(row.asset_refs_json),
            status=row.status,
            model=row.model,
        )
        created.generation_json = copy.deepcopy(row.generation_json)
        usages = await session.scalars(
            select(CitationUsage).where(CitationUsage.section_id == row.id)
        )
        await replace_citation_usage(
            session,
            project_id=source.project_id,
            section_id=created.id,
            usages=[
                {"work_id": u.work_id, "cite_key": u.cite_key, "context_snippet": u.context_snippet}
                for u in usages
            ],
        )
    return candidate
