"""章节写入的乐观并发协议。

两条路径会同时改写同一份章节 IR：

  - 写作台保存人工编辑（PUT /sections/{key}）；
  - 视觉批准把 FigureBlock 插进目标章节（POST /visuals/{id}/approve）。

此前两者都是无条件覆盖。典型后果是：批准插图成功 → 编辑器里那份**不含
FigureBlock** 的草稿被保存 → 刚插入的图被静默删掉，且没有任何冲突提示。

协议：客户端把读到章节时的 `updated_at` 原样回传，服务端发现已变化就返回 409
`section_changed`，由客户端合并后重试。字段可空，旧客户端行为不变。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException

#: 时间戳往返（ISO 序列化 / 解析、数据库精度）带来的抖动容差。
_TOLERANCE_SECONDS = 1.0

SECTION_CHANGED = "section_changed"


def require_section_unchanged(section: Any, expected: datetime | None) -> None:
    if expected is None:
        # 旧客户端不带这个字段。不能因为后端升级就让它们全部保存失败。
        return
    actual = getattr(section, "updated_at", None)
    if actual is None:
        return
    if expected.tzinfo is None and actual.tzinfo is not None:
        expected = expected.replace(tzinfo=actual.tzinfo)
    elif actual.tzinfo is None and expected.tzinfo is not None:
        actual = actual.replace(tzinfo=expected.tzinfo)
    if abs((actual - expected).total_seconds()) < _TOLERANCE_SECONDS:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": SECTION_CHANGED,
            "message": "章节已在其他位置更新，请合并后重试。",
        },
    )
