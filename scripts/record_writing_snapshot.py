#!/usr/bin/env python3
"""录制写作快照场景要用到的模型响应。

快照测试回放的是**真实模型对固定输入的回答**：输入冻结、回答冻结，之后任何
diff 都只可能来自我们自己的代码。回放键与 ``llm_call_log.prompt_sha256`` 同口径
（角色 + system/user 摘要），所以一次生产运行的台账可以直接对上一份录制。

用法：

    LLM_DEFAULT_PROVIDER=openai-compatible \\
    LLM_OPENAI_BASE_URL=... LLM_OPENAI_API_KEY=... \\
    LLM_ROLE_MODELS='{"writer":"glm-5.3-flash"}' \\
    python scripts/record_writing_snapshot.py section_zh

录完之后**逐行审阅**再签入：这份录制此后就是「模型会怎么回答」的定义，一段糊掉的
回答会把一个糊掉的期望产物一起固化下来。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for package in (ROOT / "services" / "worker", *(ROOT / "packages").iterdir()):
    if package.is_dir():
        sys.path.insert(0, str(package))
sys.path.insert(0, str(ROOT / "services" / "worker" / "tests"))

from llm_runtime import LLMConfig, LLMRequest, LLMResponse, LLMRunner  # noqa: E402
from llm_runtime.client import create_llm_provider  # noqa: E402
from snapshot_support import prompt_key  # noqa: E402


class _RecordingProvider:
    """透传到真实 provider，并把每次问答按回放键记下来。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.rows: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return str(getattr(self._inner, "name", "recorded"))

    def generate(self, request: LLMRequest) -> LLMResponse:
        response = self._inner.generate(request)
        role = str(request.metadata.get("role") or "")
        self.rows.append(
            {
                "key": prompt_key(role, request.system_prompt, request.user_prompt),
                "role": role,
                "model": response.model,
                "text": response.text,
                "usage": response.usage,
                "finish_reason": response.finish_reason,
            }
        )
        return response


def _config() -> LLMConfig:
    provider = os.environ.get("LLM_DEFAULT_PROVIDER", "").strip()
    if not provider or provider == "noop":
        raise SystemExit(
            "LLM_DEFAULT_PROVIDER 未配置（或为 noop）。录制需要真实 provider——"
            "静默产出一份空录制，会让快照测试从此比对一个空产物。"
        )
    return LLMConfig(
        provider=provider,
        base_url=os.environ.get("LLM_OPENAI_BASE_URL", ""),
        api_key=os.environ.get("LLM_OPENAI_API_KEY", ""),
        role_models=json.loads(os.environ.get("LLM_ROLE_MODELS", "{}")),
    )


async def _record_section_zh(out: Path) -> list[dict[str, Any]]:
    import test_writing_snapshot as scenario
    from paperforge_worker.pipelines.writing import WritingContext, write_section

    provider = _RecordingProvider(create_llm_provider(_config()))
    runner = LLMRunner(_config(), provider=provider)
    draft = await write_section(
        section=scenario.SECTION,
        cards=scenario.CARDS,
        whitelist=set(scenario.WHITELIST),
        context=WritingContext(outline=scenario.OUTLINE, language="zh", paper_type="review"),
        runner=runner,
    )
    print(f"  drafted {draft.word_count} words in {len(draft.paragraphs)} paragraph(s)")
    return provider.rows


async def _record_document_en(out: Path) -> list[dict[str, Any]]:
    """整篇手稿：需要真实 Postgres，因为 `write_document` 全程读写数据库。

    连接串取自 PAPERFORGE_TEST_DATABASE_URL，与测试同一个入口，绝不碰生产库。
    """
    import test_document_snapshot as scenario
    from db import make_engine, make_session_factory
    from paperforge_worker.context import JobContext
    from paperforge_worker.pipelines.document import write_document

    url = os.environ.get("PAPERFORGE_TEST_DATABASE_URL", "")
    if not url:
        raise SystemExit("PAPERFORGE_TEST_DATABASE_URL 未配置：整篇手稿场景需要真实 Postgres。")
    session_factory = make_session_factory(make_engine(url, application_name="pf-record"))

    provider = _RecordingProvider(create_llm_provider(_config()))
    runner = LLMRunner(_config(), provider=provider)

    project_id = await scenario._seed(session_factory)
    context = scenario._context(project_id, session_factory)
    JobContext.llm_runner = lambda self: runner  # type: ignore[method-assign]
    outcome = await write_document(
        context,
        language="en",
        title="Poisoning attacks on sequential recommenders",
        paper_type="review",
        coherence=False,
    )
    print(f"  wrote {outcome.section_count} section(s)")
    return provider.rows


SCENARIOS = {"section_zh": _record_section_zh, "document_en": _record_document_en}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in SCENARIOS:
        print(f"usage: {argv[0]} <{'|'.join(SCENARIOS)}>", file=sys.stderr)
        return 2
    name = argv[1]
    out = ROOT / "services" / "worker" / "tests" / "snapshots" / name
    out.mkdir(parents=True, exist_ok=True)
    rows = asyncio.run(SCENARIOS[name](out))
    if not rows:
        raise SystemExit("录制为空：没有任何模型调用发生，不写文件。")
    target = out / "calls.jsonl"
    target.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    print(f"recorded {len(rows)} call(s) -> {target.relative_to(ROOT)}")
    print("下一步：不带 PAPERFORGE_SNAPSHOT 跑测试生成期望产物，逐行审阅后签入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
