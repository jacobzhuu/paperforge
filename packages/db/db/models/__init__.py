"""PaperForge ORM 模型（方案 §4.3）：文献域 + 项目/论文结构域。

29 张综述表 → 保留 scholarly_work 系 5 张 + 新增 14 张，共 19 张；删除的 24 张
（筛选裁决/守恒账本/质量评估/效应量）不迁移（方案 §3.3）。
"""

from db.models.auth import AppUser, AuthActionToken, UserSession
from db.models.library import (
    DocumentFile,
    LibraryEntry,
    LiteratureCard,
    ScholarlyHttpCache,
    ScholarlyWork,
    WorkAuthor,
    WorkIdentifier,
    WorkUrl,
)
from db.models.paper import (
    CitationUsage,
    ClaimEvidenceAnchor,
    ExportArtifact,
    GenerationJob,
    JobEvent,
    LlmCallLog,
    Outline,
    PaperDocument,
    PaperProject,
    PaperSection,
    QualityReportRecord,
    SearchRun,
    UserAsset,
    VisualAsset,
    VisualGenerationAttempt,
    VisualSourceAsset,
)

__all__ = [
    "AppUser",
    "AuthActionToken",
    "CitationUsage",
    "ClaimEvidenceAnchor",
    "DocumentFile",
    "ExportArtifact",
    "GenerationJob",
    "JobEvent",
    "LibraryEntry",
    "LiteratureCard",
    "LlmCallLog",
    "Outline",
    "PaperDocument",
    "PaperProject",
    "PaperSection",
    "QualityReportRecord",
    "ScholarlyHttpCache",
    "ScholarlyWork",
    "SearchRun",
    "UserAsset",
    "UserSession",
    "VisualAsset",
    "VisualGenerationAttempt",
    "VisualSourceAsset",
    "WorkAuthor",
    "WorkIdentifier",
    "WorkUrl",
]
