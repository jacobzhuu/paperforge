"""Worker 测试的 pytest fixture。

共用件（录制回放、快照比对）在 `snapshot_support.py`——见那里关于为什么不放在
这个文件里的说明。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest
from snapshot_support import SNAPSHOT_ENV, assert_matches_snapshot


@pytest.fixture
def recording_mode() -> bool:
    return os.environ.get(SNAPSHOT_ENV) == "record"


@pytest.fixture
def snapshot() -> Iterator[Callable[[str, str], None]]:
    yield assert_matches_snapshot
