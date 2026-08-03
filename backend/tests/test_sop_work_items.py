"""
@Time       : 2026/07/22 10:09
@Author     : zhanglp8181
@File       : test_sop_work_items.py
@CallChain  : pytest → SopWorkItemService → 候选快照/工作项状态机/命令回执
@Description: 验证多角色候选去重、防自批、认领和结构化决定的幂等闭环。
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    BusinessRole,
    EmployeeProfile,
    EmployeeRoleAssignment,
    MemberOrgAssignment,
    SopInstance,
    SopNodeExecution,
    SopWorkItemCommandReceipt,
    SopWorkItemDecision,
    Tenant,
    User,
)
from app.organization.assignments import (
    assign_member_to_organization,
    assign_member_to_position,
    create_position,
    ensure_assignment_foundation,
)
from app.organization.query import current_assignment_predicates
from app.organization.roles import bind_position_business_role, role_source_codes
from app.organization.units import create_organization_unit, ensure_organization_foundation
from app.sop_runtime.contracts import CompletionMode, WorkItemCompletionPolicy
from app.sop_runtime.definition import (
    HumanTaskConfig,
    HumanTaskKind,
    ParticipantScopeResolver,
    WorkItemOutcomeOption,
)
from app.sop_runtime.work_items import SopWorkItemService, WorkItemError


BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_offer_deduplicates_user_reached_through_multiple_business_roles() -> None:
    """验证同一员工经两个业务角色命中时只成为一个候选并保留两条来源。"""

    with _test_session() as db:
        instance, execution = _seed_runtime(db, initiator_user_id="applicant")
        _seed_employee_roles(
            db,
            user_id="approver",
            employee_id="E200",
            role_codes=("department.manager", "seal.approver"),
        )
        service = SopWorkItemService(db)

        work_item, created = service.offer(
            instance,
            execution,
            _work_item_config(
                role_codes=("department.manager", "seal.approver"),
                mode=CompletionMode.ANY,
            ),
            initiator_user_id="applicant",
        )

        candidates = service.candidates(work_item)
        assert created is True
        assert len(candidates) == 1
        assert candidates[0].user_id == "approver"
        assert candidates[0].source_role_codes_json == [
            "department.manager",
            "seal.approver",
        ]


def test_position_default_role_enters_same_candidate_snapshot_with_source() -> None:
    """验证岗位带入角色进入唯一候选解析，并明确标记来源而不产生第二套任务。"""

    with _test_session() as db:
        instance, execution = _seed_runtime(db, initiator_user_id="applicant")
        _seed_employee_roles(
            db,
            user_id="approver",
            employee_id="E200",
            role_codes=(),
        )
        root = ensure_organization_foundation(db, "tenant_demo")
        department = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=root.id,
            code="FINANCE",
            name="财务部",
            unit_type_code="department",
        )
        ensure_assignment_foundation(db, "tenant_demo")
        position = create_position(
            db,
            tenant_id="tenant_demo",
            org_unit_id=department.id,
            code="FIN_APPROVER",
            name="财务审批岗",
            position_type_code="professional",
        )
        role = BusinessRole(
            id="role_finance_approver",
            tenant_id="tenant_demo",
            role_code="finance.approver",
            name="财务审批人",
        )
        db.add(role)
        db.flush()
        assign_member_to_organization(
            db,
            tenant_id="tenant_demo",
            employee_profile_id="profile_approver",
            org_unit_id=department.id,
        )
        assign_member_to_position(
            db,
            tenant_id="tenant_demo",
            employee_profile_id="profile_approver",
            position_id=position.id,
        )
        bind_position_business_role(
            db,
            tenant_id="tenant_demo",
            position_id=position.id,
            business_role_id=role.id,
        )
        db.commit()

        work_item, created = SopWorkItemService(db).offer(
            instance,
            execution,
            _work_item_config(role_codes=("finance.approver",)),
            initiator_user_id="applicant",
        )
        candidates = SopWorkItemService(db).candidates(work_item)

        assert created is True
        assert len(candidates) == 1
        assert candidates[0].user_id == "approver"
        assert candidates[0].source_role_codes_json == ["finance.approver"]
        assert candidates[0].source_types_json == ["position_role"]


def test_direct_candidate_requires_active_member_and_employee_profile() -> None:
    """验证直接指定用户也必须同时是活动成员并拥有活动员工档案。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        for user_id, member_status, profile_status in (
            ("eligible", "active", "active"),
            ("suspended_member", "suspended", "active"),
            ("inactive_profile", "active", "suspended"),
        ):
            db.add(
                User(
                    id=user_id,
                    tenant_id="tenant_demo",
                    username=user_id,
                    membership_status=member_status,
                    password_hash="hash",
                )
            )
            db.add(
                EmployeeProfile(
                    id=f"profile_{user_id}",
                    tenant_id="tenant_demo",
                    user_id=user_id,
                    employee_id=f"E-{user_id}",
                    status=profile_status,
                )
            )
        db.commit()

        sources = SopWorkItemService(db)._resolve_candidate_sources(
            tenant_id="tenant_demo",
            role_codes=(),
            user_ids=("eligible", "suspended_member", "inactive_profile"),
        )

        assert list(sources) == ["eligible"]
        assert sources["eligible"]["employee_profile_id"] == "profile_eligible"


def test_offer_excludes_initiator_even_when_initiator_has_approval_role() -> None:
    """验证申请人拥有审批角色时仍被排除，且无其他候选时确定性失败。"""

    with _test_session() as db:
        instance, execution = _seed_runtime(db, initiator_user_id="applicant")
        _seed_employee_roles(
            db,
            user_id="applicant",
            employee_id="E100",
            role_codes=("seal.approver",),
        )

        try:
            SopWorkItemService(db).offer(
                instance,
                execution,
                _work_item_config(role_codes=("seal.approver",)),
                initiator_user_id="applicant",
            )
        except WorkItemError as error:
            assert error.code == "WORK_ITEM_NO_ELIGIBLE_CANDIDATE"
        else:
            raise AssertionError("initiator must not approve own work item")


def test_initiator_subtree_freezes_only_in_scope_candidate_and_rechecks_membership() -> None:
    """验证发起人主组织子树只冻结范围内候选，调出范围后不能再认领。"""

    with _test_session() as db:
        instance, execution = _seed_runtime(db, initiator_user_id="applicant")
        for user_id, employee_id, roles in (
            ("applicant", "E100", ()),
            ("inside", "E201", ("seal.approver",)),
            ("sibling", "E202", ("seal.approver",)),
        ):
            _seed_employee_roles(
                db,
                user_id=user_id,
                employee_id=employee_id,
                role_codes=roles,
            )
        root = ensure_organization_foundation(db, "tenant_demo")
        division = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=root.id,
            code="DIVISION_A",
            name="事业部甲",
            unit_type_code="division",
        )
        child = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=division.id,
            code="DEPARTMENT_A",
            name="部门甲",
            unit_type_code="department",
        )
        sibling = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=root.id,
            code="DIVISION_B",
            name="事业部乙",
            unit_type_code="division",
        )
        applicant_assignment = assign_member_to_organization(
            db,
            tenant_id="tenant_demo",
            employee_profile_id="profile_applicant",
            org_unit_id=division.id,
        )
        inside_assignment = assign_member_to_organization(
            db,
            tenant_id="tenant_demo",
            employee_profile_id="profile_inside",
            org_unit_id=child.id,
        )
        assign_member_to_organization(
            db,
            tenant_id="tenant_demo",
            employee_profile_id="profile_sibling",
            org_unit_id=sibling.id,
        )
        db.commit()

        service = SopWorkItemService(db)
        work_item, _ = service.offer(
            instance,
            execution,
            HumanTaskConfig(
                kind=HumanTaskKind.STRUCTURED_WORK_ITEM,
                capability="human.structured_work_item",
                candidate_role_codes=("seal.approver",),
                participant_scope_resolver=(ParticipantScopeResolver.INITIATOR_PRIMARY_ORG_SUBTREE),
                completion_policy=WorkItemCompletionPolicy(
                    mode=CompletionMode.ANY,
                    claim_required=True,
                ),
            ),
            initiator_user_id="applicant",
        )

        assert [candidate.user_id for candidate in service.candidates(work_item)] == ["inside"]
        assert work_item.participant_scope_snapshot_json == {
            "schema_version": 1,
            "resolver": "initiator_primary_org_subtree",
            "root_org_unit_id": division.id,
            "organization_unit_ids": [division.id, child.id],
        }
        assert applicant_assignment.status == "active"

        inside_assignment.status = "inactive"
        db.add(inside_assignment)
        db.commit()
        try:
            service.claim(work_item, actor_user_id="inside", command_id="claim-after-transfer")
        except WorkItemError as error:
            assert error.code == "WORK_ITEM_CANDIDATE_NO_LONGER_ELIGIBLE"
        else:
            raise AssertionError("candidate outside the frozen scope must not claim")


def test_initiator_primary_org_scope_excludes_child_and_sibling_candidates() -> None:
    """验证主组织精确范围不自动扩大到子部门或兄弟部门。"""

    with _test_session() as db:
        instance, execution = _seed_runtime(db, initiator_user_id="applicant")
        for user_id, employee_id in (
            ("applicant", "E100"),
            ("same_org", "E201"),
            ("child_org", "E202"),
            ("sibling_org", "E203"),
        ):
            _seed_employee_roles(
                db,
                user_id=user_id,
                employee_id=employee_id,
                role_codes=() if user_id == "applicant" else ("seal.approver",),
            )
        root = ensure_organization_foundation(db, "tenant_demo")
        division = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=root.id,
            code="DIVISION_EXACT",
            name="精确范围事业部",
            unit_type_code="division",
        )
        child = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=division.id,
            code="DIVISION_EXACT_CHILD",
            name="精确范围子部门",
            unit_type_code="department",
        )
        sibling = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=root.id,
            code="DIVISION_EXACT_SIBLING",
            name="精确范围兄弟部门",
            unit_type_code="division",
        )
        for profile_id, org_unit_id in (
            ("profile_applicant", division.id),
            ("profile_same_org", division.id),
            ("profile_child_org", child.id),
            ("profile_sibling_org", sibling.id),
        ):
            assign_member_to_organization(
                db,
                tenant_id="tenant_demo",
                employee_profile_id=profile_id,
                org_unit_id=org_unit_id,
            )
        db.commit()

        work_item, _ = SopWorkItemService(db).offer(
            instance,
            execution,
            HumanTaskConfig(
                kind=HumanTaskKind.STRUCTURED_WORK_ITEM,
                capability="human.structured_work_item",
                candidate_role_codes=("seal.approver",),
                participant_scope_resolver=ParticipantScopeResolver.INITIATOR_PRIMARY_ORG,
                completion_policy=WorkItemCompletionPolicy(mode=CompletionMode.ANY),
            ),
            initiator_user_id="applicant",
        )

        assert [
            candidate.user_id for candidate in SopWorkItemService(db).candidates(work_item)
        ] == ["same_org"]
        assert work_item.participant_scope_snapshot_json["organization_unit_ids"] == [division.id]


def test_org_scoped_direct_role_matches_only_intersecting_participant_scope() -> None:
    """组织级真人直接角色只在授权子树内成为候选，不能泄漏到兄弟组织。"""

    with _test_session() as db:
        _seed_runtime(db, initiator_user_id="applicant")
        for user_id, employee_id in (("inside", "E201"), ("sibling", "E202")):
            _seed_employee_roles(
                db,
                user_id=user_id,
                employee_id=employee_id,
                role_codes=("seal.approver",),
            )
        root = ensure_organization_foundation(db, "tenant_demo")
        division = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=root.id,
            code="SCOPED_DIVISION",
            name="作用域事业部",
            unit_type_code="division",
        )
        department = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=division.id,
            code="SCOPED_DEPARTMENT",
            name="作用域部门",
            unit_type_code="department",
        )
        sibling = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=root.id,
            code="SCOPED_SIBLING",
            name="兄弟事业部",
            unit_type_code="division",
        )
        for profile_id, org_unit_id in (
            ("profile_inside", department.id),
            ("profile_sibling", sibling.id),
        ):
            assign_member_to_organization(
                db,
                tenant_id="tenant_demo",
                employee_profile_id=profile_id,
                org_unit_id=org_unit_id,
            )
        assignments = db.exec(select(EmployeeRoleAssignment)).all()
        for assignment in assignments:
            assignment.scope_type = "org_unit"
            assignment.scope_id = (
                division.id
                if assignment.employee_profile_id == "profile_inside"
                else sibling.id
            )
            assignment.include_descendants = True
            db.add(assignment)
        db.commit()

        role = db.exec(
            select(BusinessRole).where(BusinessRole.role_code == "seal.approver")
        ).one()
        service = SopWorkItemService(db)
        scoped_sources = service.preview_candidate_sources(
            tenant_id="tenant_demo",
            role_codes=(role.role_code,),
            user_ids=(),
            organization_unit_ids={division.id, department.id},
        )

        assert set(scoped_sources) == {"inside"}
        assert role_source_codes(
            db,
            tenant_id="tenant_demo",
            employee_profile_id="profile_inside",
            role_ids={role.id},
            organization_unit_ids={division.id, department.id},
        ) == {role.id: {"business_role"}}
        assert role_source_codes(
            db,
            tenant_id="tenant_demo",
            employee_profile_id="profile_sibling",
            role_ids={role.id},
            organization_unit_ids={division.id, department.id},
        ) == {}


def test_scoped_work_item_requires_initiator_primary_organization() -> None:
    """验证依赖发起人组织的 resolver 在缺少有效主归属时给出稳定错误码。"""

    with _test_session() as db:
        instance, execution = _seed_runtime(db, initiator_user_id="applicant")
        _seed_employee_roles(
            db,
            user_id="approver",
            employee_id="E200",
            role_codes=("seal.approver",),
        )

        try:
            SopWorkItemService(db).offer(
                instance,
                execution,
                HumanTaskConfig(
                    kind=HumanTaskKind.STRUCTURED_WORK_ITEM,
                    capability="human.structured_work_item",
                    candidate_role_codes=("seal.approver",),
                    participant_scope_resolver=(
                        ParticipantScopeResolver.INITIATOR_PRIMARY_ORG_SUBTREE
                    ),
                    completion_policy=WorkItemCompletionPolicy(mode=CompletionMode.ANY),
                ),
                initiator_user_id="applicant",
            )
        except WorkItemError as error:
            assert error.code == "WORK_ITEM_INITIATOR_ORG_REQUIRED"
        else:
            raise AssertionError("scoped work item must require an initiator primary org")


def test_new_scope_rechecks_role_but_empty_legacy_snapshot_keeps_frozen_candidate() -> None:
    """验证新工作项实时复核角色，而迁移前空快照继续遵循冻结候选兼容语义。"""

    with _test_session() as db:
        instance, execution = _seed_runtime(db, initiator_user_id="applicant")
        _seed_employee_roles(
            db,
            user_id="approver",
            employee_id="E200",
            role_codes=("seal.approver",),
        )
        service = SopWorkItemService(db)
        work_item, _ = service.offer(
            instance,
            execution,
            _work_item_config(
                role_codes=("seal.approver",),
                mode=CompletionMode.ANY,
                claim_required=True,
            ),
            initiator_user_id="applicant",
        )
        assignment = db.exec(
            select(EmployeeRoleAssignment).where(
                EmployeeRoleAssignment.tenant_id == "tenant_demo",
                EmployeeRoleAssignment.employee_profile_id == "profile_approver",
            )
        ).one()
        assignment.status = "inactive"
        db.add(assignment)
        db.commit()

        try:
            service.claim(work_item, actor_user_id="approver", command_id="revoked-role")
        except WorkItemError as error:
            assert error.code == "WORK_ITEM_CANDIDATE_NO_LONGER_ELIGIBLE"
        else:
            raise AssertionError("revoked role must disable a new work item candidate")

        work_item.participant_scope_snapshot_json = {}
        db.add(work_item)
        db.commit()
        claimed = service.claim(
            work_item,
            actor_user_id="approver",
            command_id="legacy-frozen-candidate",
        )
        assert claimed.assignee_user_id == "approver"


@pytest.mark.mysql
def test_explicit_subtree_candidate_matrix_runs_after_mysql_head_migration(
    mysql_database_url: str,
) -> None:
    """在隔离 MySQL head 上验证显式组织子树冻结和兄弟组织拒绝矩阵。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = mysql_database_url
    command.upgrade(config, "head")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    try:
        with Session(engine) as db:
            instance, execution = _seed_runtime(db, initiator_user_id="applicant")
            for user_id, employee_id in (("inside", "E201"), ("sibling", "E202")):
                _seed_employee_roles(
                    db,
                    user_id=user_id,
                    employee_id=employee_id,
                    role_codes=("seal.approver",),
                )
            root = ensure_organization_foundation(db, "tenant_demo")
            division = create_organization_unit(
                db,
                tenant_id="tenant_demo",
                parent_id=root.id,
                code="MYSQL_DIVISION",
                name="MySQL 事业部",
                unit_type_code="division",
            )
            child = create_organization_unit(
                db,
                tenant_id="tenant_demo",
                parent_id=division.id,
                code="MYSQL_CHILD",
                name="MySQL 子部门",
                unit_type_code="department",
            )
            sibling = create_organization_unit(
                db,
                tenant_id="tenant_demo",
                parent_id=root.id,
                code="MYSQL_SIBLING",
                name="MySQL 兄弟部门",
                unit_type_code="division",
            )
            assign_member_to_organization(
                db,
                tenant_id="tenant_demo",
                employee_profile_id="profile_inside",
                org_unit_id=child.id,
            )
            assign_member_to_organization(
                db,
                tenant_id="tenant_demo",
                employee_profile_id="profile_sibling",
                org_unit_id=sibling.id,
            )
            db.commit()

            service = SopWorkItemService(db)
            scope_snapshot = service._resolve_participant_scope(
                tenant_id="tenant_demo",
                config=HumanTaskConfig(
                    kind=HumanTaskKind.STRUCTURED_WORK_ITEM,
                    capability="human.structured_work_item",
                    candidate_role_codes=("seal.approver",),
                    participant_scope_resolver=ParticipantScopeResolver.EXPLICIT_ORG,
                    participant_scope_org_unit_id=division.id,
                    completion_policy=WorkItemCompletionPolicy(mode=CompletionMode.ANY),
                ),
                initiator_user_id="applicant",
            )
            assert scope_snapshot["organization_unit_ids"] == [division.id, child.id]
            scoped_profiles = set(
                db.exec(
                    select(MemberOrgAssignment.employee_profile_id).where(
                        MemberOrgAssignment.tenant_id == "tenant_demo",
                        MemberOrgAssignment.org_unit_id.in_({division.id, child.id}),
                        *current_assignment_predicates(),
                    )
                ).all()
            )
            assert scoped_profiles == {"profile_inside"}
            role_row = db.exec(
                select(BusinessRole).where(
                    BusinessRole.tenant_id == "tenant_demo",
                    BusinessRole.role_code == "seal.approver",
                )
            ).one()
            assert role_source_codes(
                db,
                tenant_id="tenant_demo",
                employee_profile_id="profile_inside",
                role_ids={role_row.id},
                organization_unit_ids={division.id, child.id},
            ) == {role_row.id: {"business_role"}}
            assert set(
                service._resolve_candidate_sources(
                    tenant_id="tenant_demo",
                    role_codes=("seal.approver",),
                    user_ids=(),
                    organization_unit_ids={division.id, child.id},
                )
            ) == {"inside"}
            work_item, _ = service.offer(
                instance,
                execution,
                HumanTaskConfig(
                    kind=HumanTaskKind.STRUCTURED_WORK_ITEM,
                    capability="human.structured_work_item",
                    candidate_role_codes=("seal.approver",),
                    participant_scope_resolver=ParticipantScopeResolver.EXPLICIT_ORG,
                    participant_scope_org_unit_id=division.id,
                    completion_policy=WorkItemCompletionPolicy(mode=CompletionMode.ANY),
                ),
                initiator_user_id="applicant",
            )
            db.commit()

            assert [candidate.user_id for candidate in service.candidates(work_item)] == ["inside"]
            assert work_item.participant_scope_snapshot_json["organization_unit_ids"] == [
                division.id,
                child.id,
            ]
    finally:
        engine.dispose()


def test_claim_unclaim_and_complete_are_candidate_scoped_and_idempotent() -> None:
    """验证候选认领、释放和完成均受状态约束，相同命令不会重复产生决定。"""

    with _test_session() as db:
        instance, execution = _seed_runtime(db, initiator_user_id="applicant")
        _seed_employee_roles(
            db,
            user_id="approver",
            employee_id="E200",
            role_codes=("seal.approver",),
        )
        db.add(
            User(
                id="stranger",
                tenant_id="tenant_demo",
                username="stranger",
                password_hash="hash",
            )
        )
        db.commit()
        service = SopWorkItemService(db)
        work_item, _ = service.offer(
            instance,
            execution,
            _work_item_config(
                role_codes=("seal.approver",),
                mode=CompletionMode.ANY,
                claim_required=True,
            ),
            initiator_user_id="applicant",
        )

        try:
            service.claim(
                work_item,
                actor_user_id="stranger",
                command_id="claim-stranger",
            )
        except WorkItemError as error:
            assert error.code == "WORK_ITEM_NOT_CANDIDATE"
        else:
            raise AssertionError("non-candidate must not claim work item")

        service.claim(work_item, actor_user_id="approver", command_id="claim-approver")
        claimed_revision = work_item.revision
        service.claim(work_item, actor_user_id="approver", command_id="claim-approver")
        assert work_item.revision == claimed_revision

        service.unclaim(work_item, actor_user_id="approver", command_id="unclaim-approver")
        assert work_item.status == "offered"
        service.claim(work_item, actor_user_id="approver", command_id="reclaim-approver")
        completed, did_complete = service.complete(
            work_item,
            actor_user_id="approver",
            command_id="approve-once",
            outcome="approved",
            comment="材料完整",
        )
        completed_revision = completed.revision
        replayed, replay_completed = service.complete(
            work_item,
            actor_user_id="approver",
            command_id="approve-once",
            outcome="approved",
            comment="材料完整",
        )

        assert did_complete is True
        assert replay_completed is True
        assert replayed.status == "completed"
        assert replayed.outcome == "approved"
        assert replayed.revision == completed_revision
        assert len(db.exec(select(SopWorkItemDecision)).all()) == 1
        assert len(db.exec(select(SopWorkItemCommandReceipt)).all()) == 4


def test_all_mode_waits_for_distinct_candidates_and_rejection_finishes_early() -> None:
    """验证会签按不同候选人计数，并在任一明确拒绝时确定性结束。"""

    with _test_session() as db:
        instance, execution = _seed_runtime(db, initiator_user_id="applicant")
        for user_id, employee_id in (("approver_a", "E201"), ("approver_b", "E202")):
            _seed_employee_roles(
                db,
                user_id=user_id,
                employee_id=employee_id,
                role_codes=("seal.approver",),
            )
        service = SopWorkItemService(db)
        work_item, _ = service.offer(
            instance,
            execution,
            _work_item_config(
                role_codes=("seal.approver",),
                mode=CompletionMode.ALL,
            ),
            initiator_user_id="applicant",
        )

        _, first_completed = service.complete(
            work_item,
            actor_user_id="approver_a",
            command_id="approve-a",
            outcome="approved",
        )
        completed, second_completed = service.complete(
            work_item,
            actor_user_id="approver_b",
            command_id="reject-b",
            outcome="rejected",
        )

        assert first_completed is False
        assert second_completed is True
        assert completed.status == "completed"
        assert completed.outcome == "rejected"
        assert len(service.decisions(work_item)) == 2


def test_non_approval_outcome_requires_comment_and_keeps_presentation_snapshot() -> None:
    """验证维修类结果沿用统一工作项，并按冻结选项强制保存解决说明。"""

    with _test_session() as db:
        instance, execution = _seed_runtime(db, initiator_user_id="applicant")
        _seed_employee_roles(
            db,
            user_id="engineer",
            employee_id="E204",
            role_codes=("it_support_engineer",),
        )
        service = SopWorkItemService(db)
        config = HumanTaskConfig(
            kind=HumanTaskKind.STRUCTURED_WORK_ITEM,
            capability="human.structured_work_item",
            candidate_role_codes=("it_support_engineer",),
            completion_policy=WorkItemCompletionPolicy(
                mode=CompletionMode.ANY,
                claim_required=True,
            ),
            allowed_outcomes=("resolved",),
            outcome_options=(
                WorkItemOutcomeOption(
                    value="resolved",
                    label="标记已解决",
                    tone="success",
                    comment_required=True,
                    completion_message="工程师处理说明：{comment}",
                ),
            ),
        )
        work_item, _ = service.offer(
            instance,
            execution,
            config,
            initiator_user_id="applicant",
        )
        service.claim(work_item, actor_user_id="engineer", command_id="claim-ticket")

        try:
            service.complete(
                work_item,
                actor_user_id="engineer",
                command_id="resolve-without-comment",
                outcome="resolved",
            )
        except WorkItemError as error:
            assert error.code == "WORK_ITEM_COMMENT_REQUIRED"
        else:
            raise AssertionError("resolved work item must require a resolution comment")

        completed, did_complete = service.complete(
            work_item,
            actor_user_id="engineer",
            command_id="resolve-with-comment",
            outcome="resolved",
            comment="重置 VPN 配置后连接恢复",
        )

        assert did_complete is True
        assert completed.outcome == "resolved"
        assert completed.comment == "重置 VPN 配置后连接恢复"
        assert completed.outcome_options_json[0]["label"] == "标记已解决"


def test_action_permission_is_frozen_but_rechecked_against_current_employee_roles() -> None:
    """验证候选快照不等于永久授权，撤权后新办理命令必须被服务端拒绝。"""

    with _test_session() as db:
        instance, execution = _seed_runtime(db, initiator_user_id="applicant")
        _seed_employee_roles(
            db,
            user_id="engineer",
            employee_id="E210",
            role_codes=("it_support_engineer",),
        )
        role = db.get(BusinessRole, "role_it_support_engineer")
        assert role is not None
        role.permissions_json = ["it.ticket.claim", "it.ticket.resolve"]
        db.add(role)
        db.commit()
        service = SopWorkItemService(db)
        work_item, _ = service.offer(
            instance,
            execution,
            _work_item_config(
                role_codes=("it_support_engineer",),
                claim_required=True,
                action_permissions={
                    "claim": "it.ticket.claim",
                    "outcome:approved": "it.ticket.resolve",
                },
            ),
            initiator_user_id="applicant",
        )

        service.claim(work_item, actor_user_id="engineer", command_id="claim-with-permission")
        role.permissions_json = []
        db.add(role)
        db.commit()

        try:
            service.complete(
                work_item,
                actor_user_id="engineer",
                command_id="complete-after-revoke",
                outcome="approved",
            )
        except WorkItemError as error:
            assert error.code == "WORK_ITEM_PERMISSION_REQUIRED"
            assert "it.ticket.resolve" in str(error)
        else:
            raise AssertionError("revoked action permission must stop a fresh command")
        assert work_item.action_permissions_json["claim"] == "it.ticket.claim"


def _work_item_config(
    *,
    role_codes: tuple[str, ...],
    mode: CompletionMode = CompletionMode.ANY,
    claim_required: bool = False,
    action_permissions: dict[str, str] | None = None,
) -> HumanTaskConfig:
    """构造测试用结构化人工任务参与者配置。"""

    return HumanTaskConfig(
        kind=HumanTaskKind.STRUCTURED_WORK_ITEM,
        capability="human.structured_work_item",
        candidate_role_codes=role_codes,
        completion_policy=WorkItemCompletionPolicy(
            mode=mode,
            claim_required=claim_required,
        ),
        action_permissions=action_permissions or {},
    )


def _seed_runtime(
    db: Session,
    *,
    initiator_user_id: str,
) -> tuple[SopInstance, SopNodeExecution]:
    """创建测试租户、申请人以及运行中的 SOP 实例和人工节点执行。"""

    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(
        User(
            id=initiator_user_id,
            tenant_id="tenant_demo",
            username=initiator_user_id,
            password_hash="hash",
        )
    )
    instance = SopInstance(
        id="instance_test",
        tenant_id="tenant_demo",
        session_id="session_test",
        skill_id="approval_test",
        skill_version_id="skill_version_test",
        skill_version="1.0.0",
        definition_checksum="a" * 64,
        status="running",
        active_slot_key="foreground:session_test",
        current_node_id="human_review",
    )
    execution = SopNodeExecution(
        id="execution_test",
        tenant_id="tenant_demo",
        instance_id=instance.id,
        node_id="human_review",
        step_key="human_review",
        status="running",
    )
    db.add(instance)
    db.add(execution)
    db.commit()
    return instance, execution


def _seed_employee_roles(
    db: Session,
    *,
    user_id: str,
    employee_id: str,
    role_codes: tuple[str, ...],
) -> None:
    """为一个测试员工创建多个公司业务角色任职。"""

    user = db.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            tenant_id="tenant_demo",
            username=user_id,
            password_hash="hash",
        )
        db.add(user)
    profile = EmployeeProfile(
        id=f"profile_{user_id}",
        tenant_id="tenant_demo",
        user_id=user_id,
        employee_id=employee_id,
    )
    db.add(profile)
    for role_code in role_codes:
        role = db.exec(
            select(BusinessRole).where(
                BusinessRole.tenant_id == "tenant_demo",
                BusinessRole.role_code == role_code,
            )
        ).first()
        if role is None:
            role = BusinessRole(
                id=f"role_{role_code}",
                tenant_id="tenant_demo",
                role_code=role_code,
                name=role_code,
            )
            db.add(role)
            db.flush()
        db.add(
            EmployeeRoleAssignment(
                tenant_id="tenant_demo",
                employee_profile_id=profile.id,
                business_role_id=role.id,
            )
        )
    db.commit()


def _test_session() -> Session:
    """创建加载全部表结构的隔离内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
