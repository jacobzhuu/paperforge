"""Persistent research-intent state shared by API and worker."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from db.models.library import LibraryEntry
from db.models.paper import GenerationJob, Outline, PaperDocument, ResearchQuestion, SearchRun

INTAKE_KEY = "intake"


def initial_intake(overrides: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 0,
        "status": "pending",
        "overrides": overrides,
        "answers": [],
        "questions": [],
        "materials": [],
    }


async def type_is_locked(session, project_id) -> bool:
    # Uploads and intake jobs are deliberately excluded. Even a failed downstream
    # job fixes the pipeline contract: it may already have written partial artifacts.
    job = await session.scalar(
        select(GenerationJob.id)
        .where(
            GenerationJob.project_id == project_id,
            GenerationJob.kind != "intake",
        )
        .limit(1)
    )
    if job is not None:
        return True
    for model in (LibraryEntry, Outline, PaperDocument, ResearchQuestion, SearchRun):
        if await session.scalar(select(model.id).where(model.project_id == project_id).limit(1)):
            return True
    return False


def material_issues(assets: list[Any]) -> list[dict[str, str]]:
    has_results = has_method = False
    for asset in assets:
        parsed = asset.parsed_json if isinstance(asset.parsed_json, dict) else {}
        if asset.kind in {"dataset", "result_table"}:
            has_results |= bool(
                parsed.get("rows") and (parsed.get("numeric_cells") or parsed.get("numbers"))
            )
        if asset.kind in {"method_note", "code"}:
            has_method |= bool(str(parsed.get("text") or asset.description or "").strip())
    return [
        {"code": code, "message": message}
        for ok, code, message in (
            (
                has_results,
                "result_material_missing",
                "请上传包含数据行和可解析数值的结果表或数据集",
            ),
            (has_method, "method_material_missing", "请上传可解析的方法笔记或代码"),
        )
        if not ok
    ]
