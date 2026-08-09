import uuid

import pytest
from db import comparability_key, create_project, create_user
from db.models.paper import ClaimEvidenceAnchor, PaperDocument, PaperSection, QualityReportRecord
from db.repositories.quality import _dedupe_claim_evidence_anchors, set_claim_manual_status


def test_comparability_key_is_normalized_and_dimension_sensitive():
    first = comparability_key(
        task=" Question Answering ",
        dataset="Natural Questions",
        metric_name="F1",
        split="Test",
    )
    assert first == comparability_key(
        task="question answering",
        dataset="natural questions",
        metric_name="f1",
        split="test",
    )
    assert first != comparability_key(
        task="question answering",
        dataset="TriviaQA",
        metric_name="f1",
        split="test",
    )


def test_claim_evidence_duplicates_are_deduplicated_before_flush():
    anchors = [
        {"claim_hash": "same", "cite_key": "ref2024"},
        {"claim_hash": "same", "cite_key": "ref2024"},
        {"claim_hash": "same", "cite_key": "other2024"},
    ]
    assert _dedupe_claim_evidence_anchors(anchors) == [anchors[0], anchors[2]]


def test_comparability_key_never_equates_unknown_protocols():
    common = {"task": "bgc.identification", "dataset": None, "metric_name": "AUROC", "split": None}
    assert comparability_key(**common, unknown_salt="evidence-a") != comparability_key(
        **common, unknown_salt="evidence-b"
    )


@pytest.mark.asyncio
async def test_manual_review_propagates_to_identical_anchor_in_newer_report(session):
    owner = await create_user(
        session,
        email=f"{uuid.uuid4()}@example.test",
        password_hash="!test-only",
        verified=True,
    )
    project = await create_project(
        session,
        title="Evidence review",
        paper_type="review",
        owner_id=owner.id,
    )
    document = PaperDocument(project_id=project.id, version=1, status="draft")
    session.add(document)
    await session.flush()
    section = PaperSection(
        document_id=document.id,
        section_key="introduction",
        order_no=1,
        title="Introduction",
        status="generated",
    )
    session.add(section)
    await session.flush()

    reports = []
    for suffix in ("old", "new"):
        report = QualityReportRecord(
            project_id=project.id,
            document_id=document.id,
            document_version=1,
            paper_snapshot_hash="same-snapshot",
            quality_profile="scholarly",
            review_style="narrative",
            readiness_status="needs_revision",
            stale=suffix == "old",
        )
        session.add(report)
        await session.flush()
        reports.append(report)

    anchors = []
    for report in reports:
        anchor = ClaimEvidenceAnchor(
            quality_report_id=report.id,
            project_id=project.id,
            document_id=document.id,
            section_id=section.id,
            section_key=section.section_key,
            claim_hash="same-claim",
            claim_text="A core claim",
            claim_kind="factual",
            is_core=True,
            cite_key="ref2025",
            source_key="cite:ref2025",
            source_kind="fulltext",
            evidence_excerpt="The exact supporting passage.",
            evidence_hash="same-evidence",
            support_status="insufficient_support",
            manual_status="unreviewed",
        )
        session.add(anchor)
        anchors.append(anchor)
    await session.flush()

    # The click targets the stale report still shown by the browser.
    await set_claim_manual_status(session, anchors[0], "confirmed")
    await session.refresh(anchors[1])

    assert anchors[0].manual_status == "confirmed"
    assert anchors[1].manual_status == "confirmed"
