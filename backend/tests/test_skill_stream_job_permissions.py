from fastapi import HTTPException
import pytest

from app.api import skills as skills_api
from app.db.models import User
from app.skills.stream_jobs import SkillStreamJobStore


def _user(user_id: str, username: str, tenant_id: str = "tenant_demo") -> User:
    return User(
        id=user_id,
        tenant_id=tenant_id,
        username=username,
        password_hash="test",
    )


def test_skill_stream_jobs_are_private_to_the_creating_user(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SkillStreamJobStore()
    owner = _user("user_owner", "owner")
    other = _user("user_other", "other")
    other_tenant = _user("user_external", "external", "tenant_external")
    job = store.create("skill.distill", owner.tenant_id, owner.id)
    monkeypatch.setattr(skills_api, "stream_jobs", store)

    assert skills_api._owned_stream_job(job.id, owner).id == job.id
    for user in (other, other_tenant):
        with pytest.raises(HTTPException) as error:
            skills_api._owned_stream_job(job.id, user)
        assert error.value.status_code == 404
