"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : candidates.py
@CallChain  : 当前编辑页反馈候选 → 原分享/权限/SkillCard → 共享原发布职责
@Description: 未发布候选与活动版隔离，来源/基础/授权贯穿人工编辑与原子确认。
"""
from copy import deepcopy
import secrets

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.agents.branching import get_overall_agent, visible_skill, visible_tool_rows
from app.db.models import AgentProfile, FeedbackMaterial, Skill, SkillEditCandidate, SkillVersion, User, utc_now
from app.feedback.materials import current_material, digest, lock_feedback, material_read, valid_material
from app.security.permissions import ensure_agent_scope_manager, ensure_open_gallery_admin
from app.skills.candidate_schema import (
    CandidateActionRequest, CandidateCreateRequest, CandidatePublishReceipt, CandidatePublishRequest,
    CandidateResetRead, CandidateResetRequest, CandidateSaveRequest, CandidateSourceRef,
    OrdinaryEditContext, SkillCandidateRead, SkillEditorContextRead,
)
from app.skills.edit_context import current_editor, editor_slot, lock_scope_agents, lock_skill_actor, skill_write_barrier
from app.skills.publication import compile_skill_for_publication, publish_skill_head
from app.skills.skill_schema import SkillCard
from app.skills.step_ids import skill_card_with_unique_step_ids
from app.sop_runtime.versioning import next_derived_skill_version, skill_content_checksum


def _start(db: Session, tenant_id: str, actor: User) -> User:
    """候选当前账号与租户屏障先于所有业务锁，外呼准备后必须结束该事务。"""
    actor = lock_skill_actor(db, tenant_id, actor)
    skill_write_barrier(db, tenant_id)
    return actor


def _scope(db: Session, tenant_id: str, agent_id: str | None, actor: User) -> AgentProfile | None:
    """实际范围沿原manager/整体admin规则授权，不信客户端scope或角色。"""
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, actor)
    if agent is None or agent.is_overall:
        ensure_open_gallery_admin(tenant_id, actor)
    if agent is None:
        overall = get_overall_agent(db, tenant_id, current=True)
        if overall:
            agent = ensure_agent_scope_manager(db, tenant_id, overall.id, actor)
    return agent


def _scope_id(agent: AgentProfile | None) -> str:
    """将整体规范为空串，员工用真实服务端档案ID。"""
    return agent.id if agent and not agent.is_overall else ''


def _head(db: Session, tenant_id: str, skill_id: str,
          agent: AgentProfile | None) -> tuple[Skill, SkillVersion]:
    """读取真实当前可见活动头及精确scope不可变发布，不从同名整体补猜身份。"""
    head = visible_skill(db, tenant_id, skill_id, agent.id if agent else None, current=True)
    if head is None or head.status != 'published':
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_BASE_UNAVAILABLE')
    version = db.exec(select(SkillVersion).where(SkillVersion.tenant_id == tenant_id,
        SkillVersion.skill_id == skill_id, SkillVersion.scope_agent_id == _scope_id(agent),
        SkillVersion.version == head.version, SkillVersion.status == 'published')
        .with_for_update().execution_options(populate_existing=True)).first()
    if (version is None or not version.compiled_definition_checksum
            or skill_content_checksum(head.content_json) != skill_content_checksum(version.content_json)
            or compile_skill_for_publication(head.content_json).checksum != version.compiled_definition_checksum):
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_BASE_UNAVAILABLE')
    return head, version


def _locator(db: Session, candidate_id: str, tenant_id: str, actor: User) -> SkillEditCandidate:
    """普通定位只用来选择锁ID，所有状态及身份在排序业务锁后当前读取。"""
    row = db.get(SkillEditCandidate, candidate_id)
    if row is None or row.tenant_id != tenant_id or row.actor_user_id != actor.id:
        raise HTTPException(404, 'Feedback candidate not found')
    return row


def _locked_candidate(db: Session, candidate_id: str, tenant_id: str,
                      actor: User) -> tuple[SkillEditCandidate, AgentProfile | None]:
    """排序锁原范围与材料目标，再当前锁候选；不在Feedback之后取得Agent锁。"""
    locator = _locator(db, candidate_id, tenant_id, actor)
    identifiers = [ref['target_agent_id'] for ref in locator.source_refs_json]
    if locator.scope_profile_id:
        identifiers.append(locator.scope_profile_id)
    lock_scope_agents(db, tenant_id, identifiers)
    row = db.get(SkillEditCandidate, candidate_id, populate_existing=True, with_for_update=True)
    if row is None or row.tenant_id != tenant_id or row.actor_user_id != actor.id:
        raise HTTPException(404, 'Feedback candidate not found')
    agent = _scope(db, tenant_id, row.scope_profile_id or row.scope_agent_id or None, actor)
    if _scope_id(agent) != row.scope_agent_id:
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_SCOPE_INVALID')
    return row, agent


def _materials(db: Session, tenant_id: str, material_ids: list[str], actor: User,
               agent: AgentProfile | None) -> tuple[list[FeedbackMaterial], list[CandidateSourceRef]]:
    """只承接已分享且可证明SOP来源；DTA/缺失来源继续F1分享，不伪造SOP候选。"""
    rows = []
    references = []
    for material_id in sorted(set(material_ids)):
        locator = db.get(FeedbackMaterial, material_id)
        if locator is None or locator.tenant_id != tenant_id:
            raise HTTPException(404, 'Material not found')
        target = ensure_agent_scope_manager(db, tenant_id, locator.target_agent_id, actor)
        if target is None or (agent and not agent.is_overall and target.id != agent.id):
            raise HTTPException(403, 'FEEDBACK_CANDIDATE_TARGET_UNAVAILABLE')
        feedback = lock_feedback(db, tenant_id, locator.feedback_id)
        row = current_material(db, material_id)
        if row.status != 'shared':
            raise HTTPException(409, 'FEEDBACK_MATERIAL_INVALID')
        valid_material(db, row, feedback)
        source = row.material_json.get('source') or {}
        if source.get('status') != 'verified' or source.get('branch') != 'sop':
            raise HTTPException(409, 'FEEDBACK_CANDIDATE_SOP_SOURCE_REQUIRED')
        rows.append(row)
        references.append(CandidateSourceRef(id=row.id, feedback_revision=row.feedback_revision,
            checksum=row.material_checksum, target_agent_id=target.id,
            target_profile_revision=target.profile_revision))
    return rows, references


def _validate_open(db: Session, row: SkillEditCandidate, agent: AgentProfile | None,
                   actor: User, expected_revision: int | None = None) -> tuple[Skill, SkillVersion, list[FeedbackMaterial]]:
    """模型/编辑/读取/发布共享当前slot、授权代数、材料及真实base校验。"""
    if (row.status not in {'draft', 'ready'} or row.editor_slot_key != editor_slot(actor.id)
            or row.ordinary_context_json or row.actor_auth_revision != actor.auth_revision
            or (expected_revision is not None and expected_revision != row.revision)
            or (agent.profile_revision if agent else None) != row.scope_profile_revision):
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_STALE')
    rows, refs = _materials(db, row.tenant_id, [ref['id'] for ref in row.source_refs_json], actor, agent)
    if [ref.model_dump() for ref in refs] != row.source_refs_json:
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_STALE')
    head, base = _head(db, row.tenant_id, row.skill_id, agent)
    if base.id != row.base_skill_version_id or base.compiled_definition_checksum != row.base_checksum:
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_BASE_CHANGED')
    return head, base, rows


def _changed_paths(before: object, after: object, path: str = '') -> list[str]:
    """采用确定性JSON pointer差异思想，只列路径，不另造执行或补丁语义。"""
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            escaped = str(key).replace('~', '~0').replace('/', '~1')
            pointer = path + '/' + escaped
            if key not in before or key not in after:
                result.append(pointer)
            else:
                result.extend(_changed_paths(before[key], after[key], pointer))
        return result
    if isinstance(before, list) and isinstance(after, list):
        result = []
        for index in range(max(len(before), len(after))):
            pointer = path + '/' + str(index)
            if index >= len(before) or index >= len(after):
                result.append(pointer)
            else:
                result.extend(_changed_paths(before[index], after[index], pointer))
        return result
    return [path or '/']


def _read(row: SkillEditCandidate, base: SkillVersion | None = None,
          materials: list[FeedbackMaterial] | None = None) -> SkillCandidateRead:
    """活动候选才返回已校验正文；关闭/发布历史仅保留身份和合法最小回执。"""
    full = base is not None and row.status in {'draft', 'ready'}
    sources = materials or []
    difference = full and any((item.material_json.get('source') or {}).get('skill_version_id') != base.id
                              for item in sources)
    return SkillCandidateRead(id=row.id, tenant_id=row.tenant_id, skill_id=row.skill_id,
        scope_agent_id=row.scope_agent_id, revision=row.revision, status=row.status,
        base_skill_version_id=row.base_skill_version_id, base_checksum=row.base_checksum,
        base_version=base.version if base else None, base_difference=bool(difference),
        base_difference_confirmed=row.base_difference_confirmed,
        base_content=SkillCard.model_validate(base.content_json) if full else None,
        content=SkillCard.model_validate(row.candidate_json) if full else None,
        materials=[material_read(item) for item in sources] if full else [],
        changed_paths=_changed_paths(base.content_json, row.candidate_json) if full else [],
        published=CandidatePublishReceipt.model_validate(row.publish_receipt_json)
                  if row.status == 'published' else None)


def create_candidate(db: Session, request: CandidateCreateRequest, actor: User) -> SkillCandidateRead:
    """服务器读取实际分享和基础，隔离保存单SkillCard，替换slot但不删除旧历史。"""
    actor = _start(db, request.tenant_id, actor)
    locators = [db.get(FeedbackMaterial, identifier) for identifier in request.material_ids]
    if len(set(request.material_ids)) != len(request.material_ids):
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_DUPLICATE_SOURCE')
    if any(row is None or row.tenant_id != request.tenant_id for row in locators):
        raise HTTPException(404, 'Material not found')
    identifiers = [row.target_agent_id for row in locators]
    if request.agent_id:
        identifiers.append(request.agent_id)
    lock_scope_agents(db, request.tenant_id, identifiers)
    agent = _scope(db, request.tenant_id, request.agent_id, actor)
    rows, refs = _materials(db, request.tenant_id, request.material_ids, actor, agent)
    skill_ids = {(row.material_json['source']).get('skill_id') for row in rows}
    if len(skill_ids) != 1 or not next(iter(skill_ids)):
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_SOURCE_MISMATCH')
    skill_id = next(iter(skill_ids))
    identity = digest({'actor': actor.id, 'request_key': request.request_key})
    request_hash = digest(request.model_dump())
    existing = db.exec(select(SkillEditCandidate).where(
        SkillEditCandidate.tenant_id == request.tenant_id,
        SkillEditCandidate.create_request_identity == identity)
        .with_for_update().execution_options(populate_existing=True)).first()
    if existing:
        if existing.create_request_hash != request_hash:
            raise HTTPException(409, 'FEEDBACK_CANDIDATE_REQUEST_CONFLICT')
        head, base, sources = _validate_open(db, existing, agent, actor)
        return _read(existing, base, sources)
    _head_row, base = _head(db, request.tenant_id, skill_id, agent)
    previous = current_editor(db, request.tenant_id, actor.id)
    if previous:
        previous.editor_slot_key = None
        previous.generation += 1
        previous.revision += 1
        if previous.status in {'draft', 'ready'}:
            previous.status = 'closed'
        previous.ordinary_context_json = {}
        previous.updated_at = utc_now()
        db.add(previous)
        db.flush()
    row = SkillEditCandidate(tenant_id=request.tenant_id, actor_user_id=actor.id,
        skill_id=skill_id, scope_agent_id=_scope_id(agent), editor_slot_key=editor_slot(actor.id),
        create_request_identity=identity, create_request_hash=request_hash,
        actor_auth_revision=actor.auth_revision, scope_profile_id=agent.id if agent else None,
        scope_profile_revision=agent.profile_revision if agent else None,
        base_skill_version_id=base.id, base_checksum=base.compiled_definition_checksum,
        source_refs_json=[ref.model_dump() for ref in refs], candidate_json=deepcopy(base.content_json))
    db.add(row)
    db.flush()
    return _read(row, base, rows)


def get_candidate(db: Session, candidate_id: str, tenant_id: str, actor: User) -> SkillCandidateRead:
    """每次读取重新授权；来源失效不泄漏未发布正文，合法发布历史只返回回执。"""
    actor = _start(db, tenant_id, actor)
    row, agent = _locked_candidate(db, candidate_id, tenant_id, actor)
    if row.status in {'closed', 'rejected', 'published'}:
        return _read(row)
    _head_row, base, rows = _validate_open(db, row, agent, actor)
    return _read(row, base, rows)


def save_candidate(db: Session, candidate_id: str, request: CandidateSaveRequest,
                   actor: User) -> SkillCandidateRead:
    """人工保存只写候选修订，忽略客户端版本号；活动头和Runtime快照不变。"""
    actor = _start(db, request.tenant_id, actor)
    row, agent = _locked_candidate(db, candidate_id, request.tenant_id, actor)
    _head_row, base, rows = _validate_open(db, row, agent, actor, request.expected_revision)
    if request.content.skill_id != row.skill_id:
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_SKILL_IMMUTABLE')
    content, _warnings = skill_card_with_unique_step_ids(request.content)
    row.candidate_json = content.model_copy(update={'version': base.version}).model_dump(mode='json')
    row.base_difference_confirmed = request.confirm_base_difference
    row.status = 'ready'
    row.revision += 1
    row.generation += 1
    row.updated_at = utc_now()
    db.add(row)
    return _read(row, base, rows)


def reject_candidate(db: Session, candidate_id: str, request: CandidateActionRequest,
                     actor: User) -> SkillCandidateRead:
    """拒绝不依赖旧来源仍有效；终止晚到结果但保留slot阻旧普通请求。"""
    actor = _start(db, request.tenant_id, actor)
    row, _agent = _locked_candidate(db, candidate_id, request.tenant_id, actor)
    if (row.revision != request.expected_revision or row.status not in {'draft', 'ready'}
            or row.editor_slot_key != editor_slot(actor.id)):
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_STALE')
    row.status = 'rejected'
    row.revision += 1
    row.generation += 1
    row.updated_at = utc_now()
    db.add(row)
    return _read(row)


def reset_candidate(db: Session, candidate_id: str, request: CandidateResetRequest,
                    actor: User) -> CandidateResetRead:
    """明确丢弃并当前加载所选范围原版；轮换source-free nonce，原source/base不改。"""
    actor = _start(db, request.tenant_id, actor)
    lock_scope_agents(db, request.tenant_id, [request.agent_id] if request.agent_id else [])
    agent = _scope(db, request.tenant_id, request.agent_id, actor)
    row = _locator(db, candidate_id, request.tenant_id, actor)
    row = db.get(SkillEditCandidate, row.id, populate_existing=True, with_for_update=True)
    if row.editor_slot_key != editor_slot(actor.id):
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_STALE')
    request_hash = digest(request.model_dump())
    context = row.ordinary_context_json or {}
    if context.get('request_key') == request.request_key:
        if context.get('request_hash') != request_hash:
            raise HTTPException(409, 'FEEDBACK_CANDIDATE_REQUEST_CONFLICT')
    else:
        if row.revision != request.expected_revision:
            raise HTTPException(409, 'FEEDBACK_CANDIDATE_STALE')
        row.revision += 1
        row.generation += 1
        if row.status != 'published':
            row.status = 'closed'
        context = OrdinaryEditContext(identity=secrets.token_urlsafe(32),
            scope_agent_id=_scope_id(agent), actor_auth_revision=actor.auth_revision,
            scope_profile_id=agent.id if agent else None,
            scope_profile_revision=agent.profile_revision if agent else None,
            request_key=request.request_key, request_hash=request_hash).model_dump()
        row.ordinary_context_json = context
        row.updated_at = utc_now()
        db.add(row)
    head = visible_skill(db, request.tenant_id, request.skill_id,
                         agent.id if agent else None, current=True) if request.skill_id else None
    if request.skill_id and head is None:
        raise HTTPException(404, 'Skill not found')
    from app.skills.read import skill_read

    return CandidateResetRead(candidate_id=row.id, revision=row.revision,
        editor_context=context['identity'], current_skill=skill_read(head, {}) if head else None)


def editor_context(db: Session, tenant_id: str, actor: User) -> SkillEditorContextRead:
    """刷新只取当前slot状态，绝不从浏览器草稿恢复材料权威。"""
    actor = _start(db, tenant_id, actor)
    row = current_editor(db, tenant_id, actor.id)
    if row is None:
        return SkillEditorContextRead()
    context = row.ordinary_context_json or {}
    return SkillEditorContextRead(candidate_id=row.id, candidate_revision=row.revision, status=row.status,
        scope_agent_id=context.get('scope_agent_id', row.scope_agent_id),
        scope_profile_id=context.get('scope_profile_id', row.scope_profile_id),
        ordinary=bool(context), editor_context=context.get('identity'))


def _validate_tools(db: Session, row: SkillEditCandidate, agent: AgentProfile | None,
                    content: SkillCard) -> None:
    """原可见工具目录当前锁读；不存在/停用/跨范围动作不能被发布自动绑定。"""
    names = {action.split(':', 1)[1] for key in ('nodes', 'steps')
             for node in content.model_dump(mode='json').get(key, [])
             for action in node.get('allowed_actions', []) if action.startswith('call_tool:')}
    available = {tool.name for tool in visible_tool_rows(db, row.tenant_id,
        agent.id if agent else None, include_inactive=False, current=True)}
    if names - available:
        raise HTTPException(422, 'FEEDBACK_CANDIDATE_TOOL_UNAVAILABLE')


def publish_candidate(db: Session, candidate_id: str, request: CandidatePublishRequest,
                      actor: User) -> CandidatePublishReceipt:
    """最终短事务校验并共享原发布；回执/头/绑定/不可变版本一起提交，失败全回滚。"""
    try:
        actor = _start(db, request.tenant_id, actor)
        row, agent = _locked_candidate(db, candidate_id, request.tenant_id, actor)
        request_hash = digest(request.model_dump())
        if row.status == 'published':
            if row.publish_request_hash != request_hash:
                raise HTTPException(409, 'FEEDBACK_CANDIDATE_REQUEST_CONFLICT')
            receipt = CandidatePublishReceipt.model_validate(row.publish_receipt_json)
            db.commit()
            return receipt
        head, base, materials = _validate_open(db, row, agent, actor, request.expected_revision)
        if request.expected_base_version_id != base.id or request.expected_base_checksum != base.compiled_definition_checksum:
            raise HTTPException(409, 'FEEDBACK_CANDIDATE_BASE_CHANGED')
        if any(item.material_json['source']['skill_version_id'] != base.id for item in materials):
            if not (row.base_difference_confirmed and request.confirm_base_difference):
                raise HTTPException(409, 'FEEDBACK_CANDIDATE_BASE_DIFFERENCE_UNCONFIRMED')
        if not _changed_paths(base.content_json, row.candidate_json):
            raise HTTPException(422, 'FEEDBACK_CANDIDATE_NO_CHANGE')
        content = SkillCard.model_validate(row.candidate_json).model_copy(update={
            'version': next_derived_skill_version(db, head, scope_agent_id=row.scope_agent_id)})
        _validate_tools(db, row, agent, content)
        skill = db.exec(select(Skill).where(Skill.tenant_id == row.tenant_id,
            Skill.skill_id == row.skill_id).with_for_update()
            .execution_options(populate_existing=True)).one()
        published = publish_skill_head(db, skill, agent, content=content,
                                       derived_from_version_id=base.id)
        version = db.exec(select(SkillVersion).where(SkillVersion.tenant_id == row.tenant_id,
            SkillVersion.skill_id == row.skill_id, SkillVersion.scope_agent_id == row.scope_agent_id,
            SkillVersion.version == published.version, SkillVersion.status == 'published')
            .with_for_update().execution_options(populate_existing=True)).one()
        receipt = CandidatePublishReceipt(candidate_id=row.id, skill_id=row.skill_id,
            scope_agent_id=row.scope_agent_id, skill_version_id=version.id, version=version.version,
            definition_checksum=version.compiled_definition_checksum)
        row.status = 'published'
        row.publish_request_hash = request_hash
        row.publish_receipt_json = receipt.model_dump()
        row.revision += 1
        row.generation += 1
        row.updated_at = utc_now()
        db.add(row)
        db.commit()
        return receipt
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(409, 'FEEDBACK_CANDIDATE_PUBLICATION_CONFLICT') from error
