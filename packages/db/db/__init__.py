"""PaperForge 数据层：ORM 基类、会话、模型、仓储（约定迁移自 DeepSearch packages/db）。"""

from db.base import Base, TimestampMixin
from db.repositories import *  # noqa: F403 - 仓储是数据层的公开面
from db.repositories import __all__ as _repository_exports
from db.session import make_engine, make_session_factory, session_scope

__all__ = [
    "Base",
    "TimestampMixin",
    "make_engine",
    "make_session_factory",
    "session_scope",
    *_repository_exports,
]
