"""Intent task using the existing job lifecycle, events, accounting and full pipeline."""

from __future__ import annotations

import uuid

from db import get_project, list_assets, update_job
from db.intake import material_issues, type_is_locked
from db.models.paper import GenerationJob, PaperProject
from pydantic import ValidationError
from sqlalchemy import select

from paperforge_worker.config import get_settings
from paperforge_worker.context import job_context
from paperforge_worker.pipelines.intake import understand


async def run_intake_pipeline(ctx, project_id, job_id=None, *, version, auto_continue=True):
    from paperforge_worker.worker import _finish, _mark_running, run_full_pipeline

    project_uuid = uuid.UUID(project_id)
    continue_full = False
    completed = False
    async with job_context(
        project_id=project_uuid,
        job_id=uuid.UUID(job_id) if job_id else None,
        settings=ctx.get("settings") or get_settings(),
        session_factory=ctx.get("session_factory"),
        scholar_cache=ctx.get("scholar_cache"),
        event_publisher=ctx.get("redis"),
    ) as context:
        await context.raise_if_stopped()
        await _mark_running(context)
        async with context.session() as session:
            project = await get_project(session, project_uuid)
            state = dict((project.scope_json or {}).get("intake") or {})
            topic = (project.scope_json or {}).get("topic", "")
            assets = await list_assets(session, project_uuid)
        if state.get("version") != version:
            await _finish(context, delivered=False)
            return {"superseded": True}
        await context.emit("intake.started", {}, stage="intake", progress=0.03)
        try:
            # A paused full pipeline resumes from its existing understanding.
            result = (
                state
                if state.get("status") == "ready"
                else await understand(
                    context.llm_runner(),
                    topic=topic,
                    overrides=state.get("overrides", {}),
                    answers=state.get("answers", []),
                    assets=assets,
                )
            )
        except Exception as error:
            async with context.session() as session:
                project = await session.scalar(
                    select(PaperProject).where(PaperProject.id == project_uuid).with_for_update()
                )
                current = (project.scope_json or {}).get("intake", {})
                if current.get("version") == version:
                    project.scope_json = {
                        **project.scope_json,
                        "intake": {
                            **current,
                            "status": "failed",
                            "error": "需求理解暂未完成，请重试；项目和材料已保留。",
                        },
                    }
            detail = None
            if isinstance(error, ValidationError):
                detail = {
                    "validation": [
                        {"field": ".".join(str(part) for part in item["loc"]), "type": item["type"]}
                        for item in error.errors(include_input=False, include_url=False)
                    ]
                }
            context.warn("intake", type(error).__name__, detail)
            await _finish(context, delivered=False)
            return {"status": "failed"}
        await context.raise_if_stopped()
        async with context.session() as session:
            project = await session.scalar(
                select(PaperProject).where(PaperProject.id == project_uuid).with_for_update()
            )
            current = (project.scope_json or {}).get("intake", {})
            if current.get("version") != version:
                raise ValueError("研究需求已更新，旧结果不再适用")
            if (
                current.get("overrides", {}) != state.get("overrides", {})
                or (project.scope_json or {}).get("topic", "") != topic
            ):
                raise ValueError("手动设置已更新，请重新理解研究目标")
            ready = not result.get("questions")
            scope = result.get("scope", {}) if ready else {}
            overrides = current.get("overrides", {})
            # Only intake may change type, and only before downstream work exists.
            chosen_type = overrides.get("paper_type") or result.get("paper_type")
            locked = await type_is_locked(session, project_uuid)
            if ready and chosen_type != project.paper_type and locked:
                raise ValueError("研究管线已启动，不能覆盖论文类型")
            if ready:
                project.paper_type = chosen_type
                project.language = overrides.get("language") or result["language"]
                result = {**result, "paper_type": project.paper_type, "language": project.language}
                scope = {**scope, "language": project.language}
            issues = material_issues(assets) if ready and project.paper_type == "original" else []
            persisted = {
                **current,
                **{k: v for k, v in result.items() if k != "scope"},
                "status": "ready" if ready else "needs_input",
                "material_issues": issues,
                "job_id": job_id,
                "error": None,
            }
            project.scope_json = {**(project.scope_json or {}), **scope, "intake": persisted}
            continue_full = (
                ready and not issues and auto_continue and project.writing_mode == "auto"
            )
            if continue_full:
                job = await session.get(GenerationJob, context.job_id)
                job.kind = "full"
            elif not ready and context.job_id:
                job = await session.get(GenerationJob, context.job_id)
                await update_job(session, job, status="needs_input", stage="intake", progress=1)
        await context.emit(
            "intake.completed",
            {"status": persisted["status"]},
            stage="intake",
            checkpoint={"intake": {"version": version}},
        )
        if not ready:
            await context.emit("job.finished", {"status": "needs_input"}, stage="intake")
        elif not continue_full:
            await _finish(context, delivered=True)
        else:
            await context.raise_if_stopped()
        completed = True
    if not completed:
        return {"stopped": True}
    if continue_full:
        return await run_full_pipeline(
            ctx, project_id, job_id, quality_profile="draft", regenerate_scope=False
        )
    return {"status": persisted["status"]}
