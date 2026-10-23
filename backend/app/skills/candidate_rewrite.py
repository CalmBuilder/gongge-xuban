"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : candidate_rewrite.py
@CallChain  : 原Skill rewrite同步/流式/后台 → 短事务冻结 → SkillEditor/SDK守卫 → 候选结算
@Description: 共用候选authority；外呼不持事务，每次SDK与晚到结果均重新验证来源。
"""
from collections.abc import Callable
from dataclasses import dataclass, replace

from fastapi import HTTPException
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.agents.branching import visible_tool_rows
from app.db.models import ModelConfig, User, utc_now
from app.feedback.materials import digest, material_read
from app.skills.candidates import _locked_candidate, _start, _validate_open
from app.skills.generation_safety import tool_generation_payload
from app.skills.skill_editor import SkillEditor
from app.skills.skill_schema import SkillCard, SkillRewriteRequest, SkillRewriteResponse
from app.skills.step_ids import skill_card_with_unique_step_ids

MODEL_FAILURE = 'FEEDBACK_MODEL_FAILED'


@dataclass(frozen=True)
class CandidateRewriteFence:
    """进程内原job只带冻结身份与摘要，不另建作者/执行任务或复制来源正文。"""

    candidate_id: str
    tenant_id: str
    actor_id: str
    revision: int
    generation: int
    model_id: str
    model_checksum: str
    tools_checksum: str


@dataclass(frozen=True)
class PreparedCandidateRewrite:
    """准备事务结束后的现有编辑器输入、脱离ORM会话的模型与冻结身份。"""

    request: SkillRewriteRequest
    model: ModelConfig
    fence: CandidateRewriteFence


def _actor(fence: CandidateRewriteFence) -> User:
    """只构造定位账号的身份，真实授权始终由原当前User锁读决定。"""
    return User(id=fence.actor_id, tenant_id=fence.tenant_id,
                username='candidate-actor-locator', password_hash='unavailable')


def _model_checksum(model: ModelConfig) -> str:
    """在内存摘要绑定模型参数/凭据修订；不返回或记录原值。"""
    return digest({key: getattr(model, key) for key in (
        'id', 'tenant_id', 'model', 'base_url', 'api_key_encrypted', 'enabled',
        'temperature', 'max_output_tokens', 'extra_body_json', 'memory_capture_generation',
    )})


def _tools(db: Session, tenant_id: str, agent_id: str | None) -> list[dict[str, object]]:
    """复用原安全工具目录构造器，并以当前可见/启用绑定拒绝旧RR快照。"""
    return [tool_generation_payload(tool) for tool in visible_tool_rows(
        db, tenant_id, agent_id, include_inactive=False, current=True)]


def prepare_candidate_rewrite(db: Session, request: SkillRewriteRequest,
                              actor: User, skill_id: str) -> PreparedCandidateRewrite:
    """冻结有效分享/当前候选/实际工具与模型，提交释放锁后才允许外呼。"""
    try:
        if not request.feedback_candidate_id or request.expected_candidate_revision is None:
            raise HTTPException(422, 'FEEDBACK_CANDIDATE_REVISION_REQUIRED')
        actor = _start(db, request.tenant_id, actor)
        row, agent = _locked_candidate(db, request.feedback_candidate_id, request.tenant_id, actor)
        if skill_id != row.skill_id or request.agent_id not in {None, row.scope_profile_id}:
            raise HTTPException(409, 'FEEDBACK_CANDIDATE_SCOPE_INVALID')
        if row.scope_agent_id and request.agent_id != row.scope_agent_id:
            raise HTTPException(409, 'FEEDBACK_CANDIDATE_SCOPE_INVALID')
        _head, _base, sources = _validate_open(db, row, agent, actor,
                                             request.expected_candidate_revision)
        query = select(ModelConfig).where(ModelConfig.tenant_id == row.tenant_id,
                                           ModelConfig.enabled == True)  # noqa: E712
        query = query.where(ModelConfig.id == request.model_config_id) if request.model_config_id else query.where(
            ModelConfig.is_default == True)  # noqa: E712
        model = db.exec(query.order_by(ModelConfig.id).with_for_update()
            .execution_options(populate_existing=True)).first()
        if model is None:
            raise HTTPException(404, 'Model config not found')
        tools = _tools(db, row.tenant_id, agent.id if agent else None)
        row.generation += 1
        row.updated_at = utc_now()
        db.add(row)
        enriched = request.model_copy(update={
            'agent_id': agent.id if agent else None,
            'current_skill': SkillCard.model_validate(row.candidate_json),
            'available_tools': tools,
            'feedback_materials': [material_read(item).model_dump(mode='json') for item in sources],
            'conversation': [],
        })
        fence = CandidateRewriteFence(candidate_id=row.id, tenant_id=row.tenant_id,
            actor_id=actor.id, revision=row.revision, generation=row.generation,
            model_id=model.id, model_checksum=_model_checksum(model), tools_checksum=digest(tools))
        db.expunge(model)
        db.commit()
        return PreparedCandidateRewrite(request=enriched, model=model, fence=fence)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_WRITE_CONFLICT') from error


def check_candidate_fence(db: Session, fence: CandidateRewriteFence,
                          *, allow_own_settlement: bool = False) -> User:
    """发送和写回要求精确修订；缓存可跨同代数本次结算的唯一修订递增窗口。"""
    actor = _start(db, fence.tenant_id, _actor(fence))
    row, agent = _locked_candidate(db, fence.candidate_id, fence.tenant_id, actor)
    if row.generation != fence.generation:
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_GENERATION_STALE')
    expected = fence.revision
    if allow_own_settlement and row.status == 'ready' and row.revision == expected + 1:
        # 人工保存、取消和另一次生成均推进generation，不能借此读取旧缓存。
        expected = row.revision
    _validate_open(db, row, agent, actor, expected)
    model = db.get(ModelConfig, fence.model_id, populate_existing=True, with_for_update=True)
    if (model is None or model.tenant_id != row.tenant_id or not model.enabled
            or _model_checksum(model) != fence.model_checksum
            or digest(_tools(db, row.tenant_id, agent.id if agent else None)) != fence.tools_checksum):
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_GENERATION_STALE')
    return actor


def candidate_guard(engine: Engine, fence: CandidateRewriteFence,
                    is_cancelled: Callable[[], bool] | None = None,
                    *, allow_own_settlement: bool = False) -> Callable[[], None]:
    """独立短会话校验；仅缓存读允许同代数结算窗口，SDK和提交仍精确CAS。"""
    def guard() -> None:
        """取消/撤权/撤回/新编辑/新生成均先拒绝，再放行下一次实际SDK或事件。"""
        if is_cancelled and is_cancelled():
            raise HTTPException(409, 'FEEDBACK_CANDIDATE_GENERATION_CANCELLED')
        try:
            with Session(engine) as db:
                check_candidate_fence(db, fence, allow_own_settlement=allow_own_settlement)
                db.commit()
        except SQLAlchemyError as error:
            raise HTTPException(409, 'FEEDBACK_CANDIDATE_WRITE_CONFLICT') from error
    return guard


def settle_candidate_rewrite(engine: Engine, fence: CandidateRewriteFence,
                             response: SkillRewriteResponse) -> tuple[SkillRewriteResponse, CandidateRewriteFence]:
    """晚到结果只在同一冻结代数仍有效时写候选；不改活动头或发布快照。"""
    try:
        with Session(engine) as db:
            actor = check_candidate_fence(db, fence)
            row, _agent = _locked_candidate(db, fence.candidate_id, fence.tenant_id, actor)
            if response.draft_skill.skill_id != row.skill_id:
                raise HTTPException(409, 'FEEDBACK_CANDIDATE_SKILL_IMMUTABLE')
            card, _warnings = skill_card_with_unique_step_ids(response.draft_skill)
            original = SkillCard.model_validate(row.candidate_json)
            card = card.model_copy(update={'version': original.version})
            row.candidate_json = card.model_dump(mode='json')
            row.revision += 1
            row.status = 'ready'
            row.updated_at = utc_now()
            db.add(row)
            new_fence = replace(fence, revision=row.revision)
            result = response.model_copy(update={'draft_skill': card,
                'feedback_candidate_id': row.id, 'candidate_revision': row.revision})
            db.commit()
            return result, new_fence
    except SQLAlchemyError as error:
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_WRITE_CONFLICT') from error


def rewrite_candidate(engine: Engine, prepared: PreparedCandidateRewrite) -> SkillRewriteResponse:
    """同步沿原SkillEditor生成及反思；错误只用稳定码，不外发原provider异常。"""
    guard = candidate_guard(engine, prepared.fence)
    try:
        response = SkillEditor().rewrite(prepared.request, prepared.model, request_guard=guard)
        guard()
        settled, _fence = settle_candidate_rewrite(engine, prepared.fence, response)
        return settled
    except HTTPException:
        raise
    except Exception as error:
        guard()
        raise HTTPException(502, MODEL_FAILURE) from error


def cancel_candidate_rewrite(engine: Engine, fence: CandidateRewriteFence) -> None:
    """取消先提交同一候选的生成屏障；取消先于结算时，晚到结果不可写回。"""
    try:
        with Session(engine) as db:
            actor = check_candidate_fence(db, fence)
            row, _agent = _locked_candidate(db, fence.candidate_id, fence.tenant_id, actor)
            row.generation += 1
            row.updated_at = utc_now()
            db.add(row)
            db.commit()
    except SQLAlchemyError as error:
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_WRITE_CONFLICT') from error
