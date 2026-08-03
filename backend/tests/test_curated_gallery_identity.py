from app.api import agents
from app.db import curated_gallery_seed
from app.db.models import AgentProfile


def test_curated_gallery_uses_new_seed_source() -> None:
    assert curated_gallery_seed.SEED_SOURCE == "gongge_curated_gallery_seed"


def test_seed_metadata_accepts_only_current_sources() -> None:
    assert curated_gallery_seed._is_managed_seed_metadata(
        {"seed_source": "gongge_curated_gallery_seed"}
    )
    foreign_source = "".join(("staff", "deck", "_admin_gallery_seed"))
    assert not curated_gallery_seed._is_managed_seed_metadata({"seed_source": foreign_source})
    assert curated_gallery_seed._is_managed_seed_metadata({"managed_by_seed": True})


def test_hidden_metadata_accepts_only_current_key() -> None:
    foreign_key = "".join(("hidden_from_staff", "deck"))
    foreign = AgentProfile(
        id="foreign",
        tenant_id="tenant_demo",
        name="foreign",
        metadata_json={foreign_key: True},
    )
    current = AgentProfile(
        id="current",
        tenant_id="tenant_demo",
        name="current",
        metadata_json={"hidden_from_product": True},
    )

    assert agents._agent_hidden_from_product(foreign) is False
    assert agents._agent_hidden_from_product(current) is True
