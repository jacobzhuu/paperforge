"""快照测试的共用件：录制回放与整篇产物比对。

住在普通模块里而不是 `conftest.py`：两个 conftest（仓库根一个、这里一个）同时
在 sys.path 上时，`from conftest import ...` 取到哪一个取决于插入顺序。共用件
按模块名导入就没有这种歧义，fixture 仍然留在 conftest 里。

仓库里此前只有根 `conftest.py`（Postgres 会话 + 环境隔离），没有任何 LLM
fixture，于是九份假 provider 各自复制粘贴在各测试文件里
（`test_pipelines.py`、`test_writing_pipeline.py`、`test_write_recovery.py` 两份、
`test_numeric_claim_repair.py`、`test_semantic_review.py`、`test_quality_pipeline.py`
两份、`test_publication_metadata.py`）。它们的调度策略各不相同，其中一份在脚本
用尽时**合成**一个看起来合理的回复——那正好把「提示词漂移」这件事掩盖掉。

这里提供两样东西：按 prompt 摘要回放录制响应的 `RecordedProvider`，以及整篇
产物的 golden 比对 `assert_matches_snapshot`。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from llm_runtime import LLMRequest, LLMResponse

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
#: 置为 `record` 时写入期望产物，否则只比对。CI 不设这个变量。
SNAPSHOT_ENV = "PAPERFORGE_SNAPSHOT"


def prompt_key(role: str, system_prompt: str, user_prompt: str) -> str:
    """回放用的键：角色 + prompt 摘要。

    与 `LLMRunner._record` 写进 `llm_call_log.prompt_sha256` 的摘要口径一致，
    所以一次真实运行的台账可以直接对上一份录制。
    """
    joined = f"{system_prompt}\n{user_prompt}"
    return f"{role}:{hashlib.sha256(joined.encode('utf-8')).hexdigest()}"


class MissingRecording(AssertionError):
    """录制里没有这次调用。"""


class RecordedProvider:
    """按 (role, prompt 摘要) 回放录制好的响应。

    命中不了就**大声失败**。合成一个「看起来对」的回复会让提示词漂移悄悄通过：
    模板改了、证据块少了一段、角色路由换了——回放照样绿，而生产已经在拿不同的
    prompt 问模型了。这正是 `_PerSectionProvider` 从 prompt 里回抄 EVIDENCE_ID
    自造回复时掩盖掉的那一类回归。
    """

    name = "recorded"

    def __init__(self, recordings: dict[str, dict[str, Any]]) -> None:
        self._recordings = recordings
        self.seen: list[str] = []

    @classmethod
    def from_file(cls, path: Path) -> RecordedProvider:
        recordings: dict[str, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            recordings[row["key"]] = row
        return cls(recordings)

    def generate(self, request: LLMRequest) -> LLMResponse:
        role = str(request.metadata.get("role") or "")
        key = prompt_key(role, request.system_prompt, request.user_prompt)
        self.seen.append(key)
        row = self._recordings.get(key)
        if row is None:
            raise MissingRecording(
                f"no recorded response for {key}\n"
                f"  role   : {role}\n"
                f"  system : {request.system_prompt[:160]}…\n"
                f"  user   : {request.user_prompt[:160]}…\n"
                f"录制里有 {len(self._recordings)} 条。prompt 变了就要重新录制"
                f"（scripts/record_writing_snapshot.py），而不是让替身编一个回复。"
            )
        return LLMResponse(
            text=row["text"],
            model=row.get("model") or request.model,
            provider=self.name,
            usage=row.get("usage"),
            finish_reason=row.get("finish_reason"),
        )


def assert_matches_snapshot(actual: str, name: str) -> None:
    """把整篇产物与签入的期望文件逐字比对。

    单元测试断言的是「这一条规则做了它该做的」；快照断言的是「这些规则**合起来**
    产出的东西没变」。句级证据规则静默删掉整段 prose 这一类问题，只有后者看得见。

    :param actual: 本次跑出来的产物。
    :param name: ``snapshots/`` 下的相对路径。
    """
    path = SNAPSHOT_DIR / name
    if os.environ.get(SNAPSHOT_ENV) == "record":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        pytest.skip(f"recorded {name}; rerun without {SNAPSHOT_ENV}=record to compare")
    if not path.exists():
        raise AssertionError(
            f"missing snapshot {path}. 用 {SNAPSHOT_ENV}=record 跑一遍生成它，"
            "并逐行审阅生成的内容再签入。"
        )
    expected = path.read_text(encoding="utf-8")
    if actual == expected:
        return
    diff = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile=f"{name} (expected)",
            tofile=f"{name} (actual)",
            lineterm="",
        )
    )
    raise AssertionError(f"snapshot {name} changed:\n{diff}")
