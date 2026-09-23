"""Research v1: inspect -> clarify -> Pi/tools -> validated, human-owned proposal."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from db import document_snapshot_hash, list_sections
from db.models.paper import PaperDocument, UserAsset
from db.models.research import ResearchAnalysis
from ingest.research import VERSION, derived_table, digest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import select
from storage import make_object_store

from paperforge_worker.context import JobStopped
from paperforge_worker.execution import fence_commit


class State(TypedDict, total=False):
    ready: bool


async def compute_process(content, filename, spec, *, inspect=False):
    workspace = tempfile.mkdtemp(prefix="paperforge-analysis-")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "paperforge_worker.orchestration.analysis_process",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/nonexistent",
            "ANALYSIS_WORKDIR": workspace,
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
        },
    )
    try:
        output, _ = await asyncio.wait_for(
            proc.communicate(
                json.dumps(
                    {
                        "content": base64.b64encode(content).decode(),
                        "filename": filename,
                        "spec": spec,
                        "inspect": inspect,
                    }
                ).encode()
            ),
            60,
        )
        result = json.loads(output)
        if proc.returncode or "error" in result:
            raise ValueError(result.get("error", "analysis process failed"))
        return result
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        shutil.rmtree(workspace, ignore_errors=True)


async def run_research(context, analysis_id, checkpointer):
    identifier = uuid.UUID(analysis_id)
    async with context.session() as session:
        item = await session.get(ResearchAnalysis, identifier)
        if item is None or item.project_id != context.project_id:
            raise ValueError("analysis not found")
        inputs = dict(item.input_json)
        if item.status in {"proposed", "completed", "accepted", "rejected"}:
            return
        if inputs.get("graph_version") != "research-v1":
            raise ValueError("unsupported graph version")
        asset = await session.get(UserAsset, uuid.UUID(inputs["asset_id"]))
        if asset is None or asset.project_id != context.project_id:
            raise ValueError("source asset missing")
        object_key, filename = asset.object_key, asset.title
    if inputs.get("model"):
        context.checkpoint["research_model"] = inputs["model"]
    store = await asyncio.to_thread(make_object_store, context.settings)
    content = await asyncio.to_thread(store.get, object_key)
    if hashlib.sha256(content).hexdigest() != inputs["source_hash"]:
        raise ValueError("source asset changed")

    async def save(status=None, result=None, proposal=None, update_inputs=None):
        async with context.session() as session:
            row = await session.scalar(
                select(ResearchAnalysis).where(ResearchAnalysis.id == identifier).with_for_update()
            )
            if status:
                row.status = status
            if result is not None:
                row.result_json = result
            if proposal is not None:
                row.proposal_json = proposal
            if update_inputs is not None:
                row.input_json = update_inputs
            await fence_commit(session)

    async def inspect(_):
        await context.raise_if_stopped()
        summary = await compute_process(content, filename, inputs["spec"], inspect=True)
        ready = bool(
            inputs["spec"].get("confirmed")
            and inputs["spec"].get("columns")
            and not summary.get("needs_sheet")
            and all(c in summary.get("headers", []) for c in inputs["spec"]["columns"])
            and all(
                str(inputs["spec"].get("units", {}).get(c, "")).strip()
                for c in inputs["spec"]["columns"]
            )
            and inputs["spec"].get("group_by") not in inputs["spec"]["columns"]
        )
        if not ready:
            async with context.session() as session:
                row = await session.get(ResearchAnalysis, identifier)
                existing = row.result_json or {}
            pending = {
                **summary,
                "interrupt_id": existing.get("interrupt_id") or str(uuid.uuid4()),
                "message": "请确认工作表、数值字段、分组关系和单位；本次仅做描述统计。",
            }
            await save(status="needs_input", result=pending)
        return {"ready": ready}

    async def clarify(_):
        interrupt({"analysis_id": analysis_id})
        return {"ready": True}

    computed = None
    completed_tools = set()

    async def reserve_turn():
        async with context.session() as session:
            row = await session.scalar(
                select(ResearchAnalysis).where(ResearchAnalysis.id == identifier).with_for_update()
            )
            current = dict(row.input_json)
            if current.get("model_calls", 0) >= 20:
                raise ValueError("已达到累计 20 回合上限")
            current["model_calls"] = current.get("model_calls", 0) + 1
            row.input_json = current
            await fence_commit(session)

    async def tool(name, args):
        nonlocal computed
        await context.raise_if_stopped()
        await context.emit("research.tool", {"name": name}, stage="research")
        if args:
            raise ValueError("工具不接受未批准的参数")
        if name == "read_table_summary":
            return await compute_process(content, filename, inputs["spec"], inspect=True)
        if name == "compute_descriptive":
            if computed is None:
                async with context.session() as session:
                    row = await session.get(ResearchAnalysis, identifier)
                    cached = row.result_json or {}
                if (
                    cached.get("version") == VERSION
                    and cached.get("source_hash") == inputs["source_hash"]
                ):
                    computed = cached
                else:
                    computed = await compute_process(content, filename, inputs["spec"])
                    image = base64.b64decode(computed.pop("chart_base64"))
                    key = (
                        f"users/{context.owner_id}/projects/{context.project_id}/"
                        f"analysis/{identifier}/chart.png"
                    )
                    await asyncio.to_thread(store.put, key, image, content_type="image/png")
                    computed.update(
                        {
                            "chart_key": key,
                            "source_hash": inputs["source_hash"],
                            "source_asset_id": inputs["asset_id"],
                            "validation": "deterministic",
                        }
                    )
                    import ingest.research as implementation

                    archive = io.BytesIO()
                    source_name = "input" + Path(filename).suffix.lower()
                    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                        bundle.writestr(source_name, content)
                        bundle.writestr("spec.json", json.dumps(inputs["spec"], ensure_ascii=False))
                        bundle.writestr("result.json", json.dumps(computed, ensure_ascii=False))
                        bundle.writestr("research.py", Path(implementation.__file__).read_bytes())
                        bundle.writestr("requirements.txt", "matplotlib==3.11.1\nopenpyxl==3.1.5\n")
                        bundle.writestr(
                            "reproduce.py",
                            "from pathlib import Path\nimport json\n"
                            "from research import read_table, analyze, chart_png\n"
                            "spec=json.loads(Path('spec.json').read_text())\n"
                            f"source={source_name!r}\n"
                            "data=Path(source).read_bytes()\n"
                            "table=read_table(data, source, spec.get('sheet'))\n"
                            "result=analyze(table,spec)\n"
                            "print(json.dumps(result,ensure_ascii=False,indent=2))\n"
                            "Path('chart.png').write_bytes(chart_png(result))\n",
                        )
                    bundle_key = key.replace("chart.png", "reproducibility.zip")
                    await asyncio.to_thread(
                        store.put, bundle_key, archive.getvalue(), content_type="application/zip"
                    )
                    computed["bundle_key"] = bundle_key
                    await save(result=computed)
            completed_tools.add(name)
            return computed
        if name == "read_results":
            if computed is None:
                raise ValueError("请先执行计算")
            return computed
        if name == "propose_patch":
            if computed is None:
                raise ValueError("请先执行计算")
            completed_tools.add(name)
            return {"status": "proposal_requested", "requires_human_confirmation": True}
        raise ValueError("tool not allowed")

    async def execute(_):
        nonlocal inputs
        async with context.session() as session:
            current = await session.get(ResearchAnalysis, identifier)
            inputs = dict(current.input_json)
        if not inputs["spec"].get("confirmed"):
            raise ValueError("analysis specification needs confirmation")
        elapsed_before = float(inputs.get("elapsed_seconds", 0))
        if inputs.get("execution_started_at"):
            elapsed_before += max(
                0,
                (
                    datetime.now(UTC) - datetime.fromisoformat(inputs["execution_started_at"])
                ).total_seconds(),
            )
        started = time.monotonic()
        await save(
            status="running",
            update_inputs={
                **inputs,
                "elapsed_seconds": elapsed_before,
                "execution_started_at": datetime.now(UTC).isoformat(),
            },
        )
        remaining = 600 - elapsed_before
        if remaining <= 0:
            raise ValueError("分析累计执行时间已达到 10 分钟")
        try:
            async with asyncio.timeout(remaining):
                if inputs["engine"] == "pi":
                    from paperforge_worker.orchestration.pi_analysis import run_pi

                    await run_pi(context, inputs["goal"], tool, reserve_turn)
                    if "propose_patch" not in completed_tools:
                        raise ValueError("Agent 未完成计算与提案；可保留结果或重新发起任务")
                else:
                    await tool("compute_descriptive", {})
                    await tool("propose_patch", {})
                await persist_proposal()
        finally:
            async with context.session() as session:
                row = await session.get(ResearchAnalysis, identifier)
                row.input_json = {
                    **row.input_json,
                    "elapsed_seconds": elapsed_before + time.monotonic() - started,
                    "execution_started_at": None,
                }
                await fence_commit(session)
        return {"ready": True}

    async def persist_proposal():
        table_id = uuid.uuid5(identifier, "table")
        figure_id = uuid.uuid5(identifier, "figure")
        parsed = {
            **derived_table(computed),
            "provenance": {
                "analysis_id": analysis_id,
                "source_asset_id": inputs["asset_id"],
                "source_hash": inputs["source_hash"],
                "spec": inputs["spec"],
                "result_hash": digest(computed),
            },
        }
        async with context.session() as session:
            for asset_id, kind, title, payload, key in (
                (table_id, "result_table", "描述统计结果", parsed, None),
                (
                    figure_id,
                    "figure",
                    "描述统计均值图.png",
                    {
                        "type": "figure",
                        "figure_path": f"figures/{figure_id}.png",
                        "provenance": parsed["provenance"],
                    },
                    computed["chart_key"],
                ),
            ):
                if await session.get(UserAsset, asset_id) is None:
                    session.add(
                        UserAsset(
                            id=asset_id,
                            project_id=context.project_id,
                            kind=kind,
                            title=title,
                            parsed_json=payload,
                            object_key=key,
                        )
                    )
            row = await session.get(ResearchAnalysis, identifier)
            proposal = None
            if inputs.get("section_key"):
                document = await session.get(PaperDocument, uuid.UUID(inputs["document_id"]))
                sections = await list_sections(session, document.id) if document else []
                if document_snapshot_hash(sections) != inputs["snapshot_hash"]:
                    raise ValueError("文稿已变化；结果已保存，请重新发起修改")
                target = next(s for s in sections if s.section_key == inputs["section_key"])
                from db import grounded_asset_payloads
                from ingest.numlint import lint_sections

                from paperforge_worker.pipelines.quality import _body_text

                await session.flush()
                check = lint_sections(
                    [
                        {
                            "section_key": target.section_key,
                            "text": _body_text(target.body_ir_json or {}),
                        }
                    ],
                    parsed_assets=await grounded_asset_payloads(session, context.project_id),
                )
                computed["number_check"] = check.to_payload()
                row.result_json = dict(computed)

                body = copy.deepcopy(
                    target.body_ir_json
                    or {"key": target.section_key, "title": target.title, "blocks": []}
                )
                refs = [str(table_id), str(figure_id), inputs["asset_id"]]
                body.setdefault("blocks", []).extend(
                    [
                        {
                            "type": "paragraph",
                            "runs": [
                                {
                                    "t": "text",
                                    "v": (
                                        "以下为上传数据的描述统计。缺失值按字段排除并单独报告，"
                                        "样本标准差使用 n−1 分母；结果不代表统计显著性或因果关系。"
                                    ),
                                },
                                {"t": "grounding", "source_refs": refs},
                            ],
                        },
                        {
                            "type": "table",
                            "source": {"kind": "user_asset", "ref": str(table_id)},
                            "caption": "描述统计（不可计算的标准差明确标注）",
                        },
                        {
                            "type": "figure",
                            "asset_ref": str(figure_id),
                            "caption": "各分组的均值（描述性展示）",
                            "alt_text": "上传数据的均值图",
                        },
                    ]
                )
                from paper_ir.schema import Section

                Section.model_validate(body)
                proposal = {
                    "section_key": target.section_key,
                    "body": body,
                    "body_hash": digest(body),
                    "asset_refs": refs,
                    "summary": "在目标章节追加来源可追溯的描述统计表与均值图；现有文字和数字保留。",
                }
            row.proposal_json = proposal
            row.status = "proposed" if proposal else "completed"
            await fence_commit(session)
        await context.emit(
            "research.result_ready",
            {"analysis_id": analysis_id, "needs_confirmation": bool(proposal)},
        )

    graph = StateGraph(State)
    graph.add_node("inspect", inspect)
    graph.add_node("clarify", clarify)
    graph.add_node("execute", execute)
    graph.add_edge(START, "inspect")
    graph.add_conditional_edges("inspect", lambda s: "execute" if s["ready"] else "clarify")
    graph.add_edge("clarify", "inspect")
    graph.add_edge("execute", END)
    compiled = graph.compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": f"research-v1:{analysis_id}"}, "recursion_limit": 16}
    existing = await compiled.aget_state(config)
    argument = (
        Command(resume=True)
        if inputs.get("response") and existing.tasks and any(t.interrupts for t in existing.tasks)
        else None
        if existing.values
        else {}
    )
    result = await compiled.ainvoke(argument, config)
    if result.get("__interrupt__"):
        raise JobStopped("pause", reason="research_needs_input")
