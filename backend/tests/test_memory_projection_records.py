"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_memory_projection_records.py
@CallChain  : pytest SQLite/MySQL → MemoryService → 冻结快照 → 最终SDK
@Description: 验证真实范围代数、独立会话恢复、撤权ABA和清空后新来源消费。
"""

from sqlmodel import Session, SQLModel, create_engine

from app.db.models import AgentProfile, MemoryCaptureScope, MemoryKeyState, MemoryRecord, Tenant, User
from app.memory.capture import advance_memory_agent_permission_generation, advance_memory_clear_generation
from app.memory.request_projection import MemoryRequestEnvelope, MemoryRequestPayload
from app.memory.service import MemoryService
from tests.test_memory_projection_consumers import SDK, llm


def check_generation_contract(engine) -> None:
    """相同断言在真实双方言执行；恢复只用旧快照，新事实不能偷偷替换计划。"""
    with Session(engine, expire_on_commit=False) as db:
        db.add(Tenant(id="projection-tenant", name="隔离测试"))
        db.add(User(id="projection-user", tenant_id="projection-tenant", username="测试",
                    password_hash="synthetic-test-only"))
        db.add(AgentProfile(id="projection-agent", tenant_id="projection-tenant", name="员工",
                            owner_user_id="projection-user"))
        db.commit()
        db.add(MemoryRecord(id="old-fact", tenant_id="projection-tenant", user_id="projection-user",
                            agent_id="projection-agent", kind="profile", content="青禾\n规则 a  b",
                            metadata_json={"key": "name"}, source_sequence=1))
        db.commit()
        service = MemoryService(db)
        selected = service.projection_candidates("projection-tenant", "projection-user",
                                                  agent_id="projection-agent")
        assert len(selected) == 1
        validate = service.projection_validator("projection-tenant", "projection-user",
                                                 agent_id="projection-agent")
        envelope = MemoryRequestEnvelope(selected, "任务", validate=validate)
        payload = MemoryRequestPayload({"goal": "任务", "memory_context": []}, envelope)
        sdk = SDK(['{"ok":true}'])
        llm(sdk).generate_json("系统", payload)
        assert envelope.selected == selected
        snapshot = envelope.snapshot()
        db.add(MemoryRecord(id="later-fact", tenant_id="projection-tenant", user_id="projection-user",
                            agent_id="projection-agent", kind="fact", content="稍后新增事实",
                            metadata_json={"key": "later"}, source_sequence=2))
        db.commit()
        # 新进程/Session的权限查询不从旧ORM identity map获取。
        with Session(engine) as fresh:
            current_service = MemoryService(fresh)
            frozen = MemoryRequestEnvelope.restore(snapshot, validate=current_service.projection_validator(
                "projection-tenant", "projection-user", agent_id="projection-agent"))
            sdk = SDK(['{"ok":true}'])
            llm(sdk).generate_json("系统", MemoryRequestPayload(dict(payload), frozen))
            assert frozen.selected == selected
            assert "稍后新增事实" not in str(sdk.calls)
        # 原键被合法删除后，不以冻结正文存在继续披露；新版本更新则不替换旧计划。
        deletion = MemoryKeyState(tenant_id="projection-tenant", user_id="projection-user",
            agent_scope_key="projection-agent", kind="profile", normalized_key="name",
            last_source_sequence=3, tombstoned=True)
        db.add(deletion)
        db.commit()
        sdk = SDK(['{"ok":true}'])
        frozen = MemoryRequestEnvelope.restore(snapshot, validate=validate)
        llm(sdk).generate_json("系统", MemoryRequestPayload(dict(payload), frozen))
        assert frozen.selected == ()
        assert "青禾" not in str(sdk.calls)
        advance_memory_clear_generation(db, tenant_id="projection-tenant", user_id="projection-user",
                                         agent_id="projection-agent")
        # 正式 clear 同时清理本范围键状态；保留清空代数阻止旧选择复活。
        db.delete(deletion)
        db.commit()
        sdk = SDK(['{"ok":true}'])
        frozen = MemoryRequestEnvelope.restore(snapshot, validate=validate)
        llm(sdk).generate_json("系统", MemoryRequestPayload(dict(payload), frozen))
        assert frozen.selected == ()
        assert "青禾" not in str(sdk.calls)
        # 清空后新来源携带最新代数，正常进入新的任务选择。
        db.add(MemoryRecord(id="new-fact", tenant_id="projection-tenant", user_id="projection-user",
                            agent_id="projection-agent", kind="profile", content="清空后新事实",
                            metadata_json={"key": "name"}, source_sequence=3, agent_generation=1))
        db.commit()
        current = service.projection_candidates("projection-tenant", "projection-user",
                                                 agent_id="projection-agent")
        assert [item.id for item in current] == ["new-fact"]
        assert validate(current[0])
        # 撤权后重新授权也不倒退代数，旧来源永久失去披露许可。
        advance_memory_agent_permission_generation(db, tenant_id="projection-tenant",
                                                    user_id="projection-user", agent_id="projection-agent")
        db.commit()
        assert not validate(current[0])
        assert not MemoryService(db).projection_validator("other-tenant", "projection-user",
                                                           agent_id="projection-agent")(current[0])
        assert not MemoryService(db).projection_validator("projection-tenant", "projection-user",
                                                           agent_id="other-agent")(current[0])
        # 不绑定Agent的合法新来源也使用__user__ scope，不能混同为空字符串。
        db.add(MemoryCaptureScope(tenant_id="projection-tenant", user_id="projection-user",
                                 scope_kind="agent", agent_scope_key="__user__", clear_generation=1))
        db.add(MemoryRecord(id="global-new-fact", tenant_id="projection-tenant",
                            user_id="projection-user", kind="profile", content="全用户清空后的新事实",
                            metadata_json={"key": "global-name"}, source_sequence=4,
                            agent_generation=1))
        db.commit()
        global_facts = service.projection_candidates("projection-tenant", "projection-user")
        assert [item.id for item in global_facts] == ["global-new-fact"]
        assert service.projection_validator("projection-tenant", "projection-user")(global_facts[0])


def test_sqlite_frozen_disclosure_clear_revoke_new_source(tmp_path) -> None:
    """真实独立SQLite连接验证冻结、清空、撤权和新事实消费。"""
    engine = create_engine("sqlite:///" + str(tmp_path / "projection.sqlite"))
    SQLModel.metadata.create_all(engine)
    try:
        check_generation_contract(engine)
    finally:
        engine.dispose()
