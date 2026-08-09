from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from paperforge_worker.pipelines.fulltext import FulltextSource, acquire_fulltexts


async def test_acquire_fulltexts_reuses_durable_parses_without_network(monkeypatch) -> None:
    work_id = uuid4()
    entry = SimpleNamespace(relevance_score=0.9)
    work = SimpleNamespace(id=work_id)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def _list_entries(*args, **kwargs):
        return [(entry, work)]

    async def _load(*args, **kwargs):
        return {
            str(work_id): FulltextSource(
                work_id=str(work_id),
                document_file_id=uuid4(),
                access_scope="shared",
                text="persisted full text",
            )
        }

    events: list[tuple[str, dict]] = []

    async def _emit(event_type, payload, **kwargs):
        events.append((event_type, payload))

    monkeypatch.setattr("paperforge_worker.pipelines.fulltext.list_entries", _list_entries)
    monkeypatch.setattr(
        "paperforge_worker.pipelines.fulltext.load_persisted_fulltext_sources", _load
    )

    context = SimpleNamespace(
        project_id=uuid4(),
        session=lambda: _Session(),
        emit=_emit,
    )
    outcome, texts = await acquire_fulltexts(context)

    assert texts == {str(work_id): "persisted full text"}
    assert outcome.selected == 1
    assert outcome.reused == 1
    assert outcome.planned == 0
    assert outcome.coverage == 1.0
    assert events[-1][0] == "ingest.fulltext"
