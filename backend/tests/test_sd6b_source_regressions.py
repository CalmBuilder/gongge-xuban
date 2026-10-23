"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_sd6b_source_regressions.py
@CallChain  : pytest → 真实输入/模型变更API/完成登记 → 结算
@Description: 将独立复核 R1 原生反例纳入回归，测试夹具显式设置原选模为默认模型。
"""

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from app.core.agent_loop import AgentLoop
from app.db.models import (
    AgentProfile,
    ChatSession,
    MemoryCaptureJob,
    MemoryRecord,
    Message,
    ModelConfig,
    Tenant,
    User,
)
from app.api.model_configs import update_model_config, preflight_dynamic_model_config
from app.llm.schemas import ModelConfigUpdateRequest
from app.memory.capture import register_memory_capture_job, memory_model_contract_hash
from app.memory.worker import claim_memory_capture_job, settle_memory_capture_job
from app.security.encryption import encrypt_secret
from app.session.session_schema import ChatTurnRequest

OBSERVATIONS = []


def _seed(db):
    """由实际 AgentLoop T0 建立合成来源，并保留轮开始时选中的模型。"""
    tenant = Tenant(id="tenant_probe", name="隔离审核")
    user = User(
        id="user_probe",
        tenant_id=tenant.id,
        username="probe-user",
        password_hash="unused",
        role="admin",
    )
    agent = AgentProfile(
        id="agent_probe",
        tenant_id=tenant.id,
        name="审核员工",
        owner_user_id=user.id,
        status="active",
    )
    session = ChatSession(
        id="session_probe", tenant_id=tenant.id, user_id=user.id, agent_id=agent.id
    )
    model = ModelConfig(
        id="model_probe",
        tenant_id=tenant.id,
        name="审核模型",
        is_default=True,
        model="probe-model",
        api_key_encrypted=encrypt_secret("synthetic-probe-not-a-credential"),
    )
    db.add_all([tenant, user, agent, session, model])
    db.commit()
    request = ChatTurnRequest(
        tenant_id=tenant.id,
        session_id=session.id,
        user_id=user.id,
        agent_id=agent.id,
        message="我的偏好是合成事实。",
    )
    source, _ = AgentLoop(db)._append_user_input_message(request, session.id)
    db.commit()
    return request, user, session, model, source


def _assistant(db, session, source, suffix="one"):
    """创建明确绑定原用户消息的合成助手完成事实。"""
    assistant = Message(
        id=f"assistant_{suffix}",
        tenant_id=session.tenant_id,
        session_id=session.id,
        role="assistant",
        content="已完成合成回复。",
        metadata_json={"turn_id": source.id, "user_message_id": source.id},
    )
    db.add(assistant)
    db.flush()
    return assistant


def test_selected_model_aba_before_t1_must_not_rebind_old_turn():
    """输入和模型已选后真实禁用/恢复入口不能把旧轮改绑为新模型代数。"""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        request, user, session, model, source = _seed(db)
        initial_generation = model.memory_capture_generation
        initial_contract = memory_model_contract_hash(model)
        update_model_config(
            model.id, ModelConfigUpdateRequest(tenant_id=user.tenant_id, enabled=False), db, user
        )
        update_model_config(
            model.id, ModelConfigUpdateRequest(tenant_id=user.tenant_id, enabled=True), db, user
        )
        assistant = _assistant(db, session, source)
        registration = register_memory_capture_job(
            db,
            request,
            session,
            model,
            user_message_id=source.id,
            assistant_message_id=assistant.id,
        )
        assert registration is not None
        db.commit()
        job = claim_memory_capture_job(db, worker_id="probe-worker", job_id=registration.job.id)
        result = settle_memory_capture_job(
            db,
            job_id=job.id,
            worker_id="probe-worker",
            fencing_token=job.fencing_token,
            delta=[
                {
                    "operation": "upsert",
                    "kind": "preference",
                    "key": "drink",
                    "content": "合成旧轮事实",
                }
            ],
        )
        OBSERVATIONS.append(
            {
                "case": "selected_model_aba_before_t1",
                "initial_generation": initial_generation,
                "current_generation": model.memory_capture_generation,
                "job_generation": job.model_capture_generation,
                "initial_contract_restored": initial_contract == memory_model_contract_hash(model),
                "result": result,
                "memory_count": len(db.exec(select(MemoryRecord)).all()),
            }
        )
        assert result["status"] == "discarded", (
            "模型在选择后禁用/恢复，旧轮应失效，不能在T1改绑新代数后成功保存"
        )


def test_explicit_source_and_wrong_turn_assistant_must_not_register():
    """显式传入正确来源身份时，另一轮 assistant cutoff 也必须拒绝。"""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        request, user, session, model, source = _seed(db)
        second, _ = AgentLoop(db)._append_user_input_message(request, session.id)
        db.flush()
        assistant = _assistant(db, session, second, suffix="wrong-turn")
        registration = register_memory_capture_job(
            db,
            request,
            session,
            model,
            user_message_id=source.id,
            assistant_message_id=assistant.id,
        )
        db.commit()
        OBSERVATIONS.append(
            {
                "case": "explicit_source_wrong_turn_assistant",
                "registration_created": registration is not None,
                "job_count": len(db.exec(select(MemoryCaptureJob)).all()),
                "source_matches_assistant_turn": source.id == assistant.metadata_json["turn_id"],
            }
        )
        assert registration is None, "正确来源与另一轮assistant不可共同登记为一个成功任务"


@pytest.mark.parametrize("failure", [False, True])
def test_stale_preflight_must_not_publish_old_capability_after_model_change(monkeypatch, failure):
    """真实预检入口的外呼期间改变模型，旧预检不能覆写新配置能力合同。"""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        request, user, session, model, source = _seed(db)
        model_id = model.id
        tenant_id = user.tenant_id

        class Provider:
            """只替代外部预检请求，并由另一Session执行真实模型更新API。"""

            def __init__(self, row):
                """快照预检起始模型，外呼时不持有原 Session 事务。"""
                self.started_model = row.model

            def preflight_dynamic_capabilities(self):
                """通过真实更新入口变更模型后返回旧成功或旧失败。"""
                with Session(engine) as other:
                    owner = other.get(User, user.id)
                    update_model_config(
                        model_id,
                        ModelConfigUpdateRequest(tenant_id=tenant_id, model="changed-probe-model"),
                        other,
                        owner,
                    )
                if failure:
                    from app.llm import LLMError

                    raise LLMError("synthetic failure")
                return {
                    "probe_started_model": self.started_model,
                    "supports_structured_output": True,
                }

        monkeypatch.setattr("app.api.model_configs.LLMClient", Provider)
        response = preflight_dynamic_model_config(model_id, tenant_id, db)
        with Session(engine) as verify:
            current = verify.get(ModelConfig, model_id)
            OBSERVATIONS.append(
                {
                    "case": "stale_preflight_after_model_change",
                    "response_success": response.success,
                    "current_model": current.model,
                    "current_generation": current.memory_capture_generation,
                    "preflight_status": current.preflight_status,
                    "snapshot_probe_model": (current.capability_snapshot_json or {}).get(
                        "probe_started_model"
                    ),
                    "capability_present": bool(current.capability_checksum),
                }
            )
            current_status = current.preflight_status
            assert current_status != "ready", "模型已更换，旧模型预检不应发布ready能力快照"
