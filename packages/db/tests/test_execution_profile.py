import uuid
from types import SimpleNamespace

from db.execution_profile import source_execution_profile
from db.repositories.jobs import resume_checkpoint


def test_resume_keeps_pinned_profile_and_recovers_legacy_fast_job():
    pinned = SimpleNamespace(
        id=uuid.uuid4(),
        checkpoint_json={
            "execution_profile": "standard",
            "resume": {"function": "run_full_pipeline", "kwargs": {"delivery_mode": "fast_draft"}},
            "control": "pause",
        },
    )
    assert source_execution_profile(pinned) == "standard"
    assert resume_checkpoint(pinned)["execution_profile"] == "standard"

    legacy = SimpleNamespace(
        id=uuid.uuid4(),
        checkpoint_json={
            "resume": {"function": "run_full_pipeline", "kwargs": {"delivery_mode": "fast_draft"}}
        },
    )
    assert source_execution_profile(legacy) == "fast_draft"
    assert resume_checkpoint(legacy)["execution_profile"] == "fast_draft"
