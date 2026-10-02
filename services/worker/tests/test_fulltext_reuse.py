from __future__ import annotations

from threading import Lock
from time import sleep
from types import SimpleNamespace
from uuid import uuid4

from paperforge_worker.pipelines import fulltext
from paperforge_worker.pipelines.fulltext import FulltextSource, acquire_fulltexts
from scholar_gateway.fulltext import OaFulltextTarget


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


def test_fulltext_link_discovery_is_bounded_and_ordered(monkeypatch) -> None:
    lock = Lock()
    active = 0
    peak = 0
    calls: dict[str, list[str]] = {}

    def discover(source, target, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.setdefault(target.work_id, []).append(source)
        sleep(0.01)
        with lock:
            active -= 1
        return ({"url": f"https://example.org/{target.work_id}/{source}"},)

    monkeypatch.setattr(
        fulltext,
        "discover_unpaywall_links",
        lambda target, **kwargs: discover("unpaywall", target),
    )
    monkeypatch.setattr(
        fulltext,
        "discover_doaj_links",
        lambda target, **kwargs: discover("doaj", target),
    )
    targets = [
        OaFulltextTarget(work_id=str(index), links=({"url": f"original-{index}"},))
        for index in range(6)
    ]
    enriched = fulltext._enrich_unpaywall_targets(targets, None, "contact@example.org", 2)

    assert peak == 2
    assert [target.work_id for target in enriched] == [str(index) for index in range(6)]
    for target in enriched:
        assert calls[target.work_id] == ["unpaywall", "doaj"]
        assert [link["url"] for link in target.links] == [
            f"original-{target.work_id}",
            f"https://example.org/{target.work_id}/unpaywall",
            f"https://example.org/{target.work_id}/doaj",
        ]
