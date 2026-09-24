"""Project default and immutable per-job execution policy."""

from __future__ import annotations

from typing import Any

EXECUTION_PROFILE_KEY = "execution_profile"
EXECUTION_PROFILES = frozenset({"standard", "fast_draft"})


def source_execution_profile(job: Any) -> str:
    """Recover the policy of jobs created before the dedicated checkpoint existed."""
    checkpoint = getattr(job, "checkpoint_json", None) or {}
    profile = checkpoint.get(EXECUTION_PROFILE_KEY)
    if profile in EXECUTION_PROFILES:
        return profile
    resume = checkpoint.get("resume") or {}
    kwargs = resume.get("kwargs") if isinstance(resume, dict) else None
    if not isinstance(kwargs, dict):
        kwargs = {}
    return "fast_draft" if kwargs.get("delivery_mode") == "fast_draft" else "standard"
