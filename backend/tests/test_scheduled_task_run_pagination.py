"""
@Time       : 2026/08/01 21:45
@Author     : zhanglp8181
@File       : test_scheduled_task_run_pagination.py
@CallChain  : pytest → scheduled_tasks.page_enterprise_scheduled_task_runs → SQLModel
@Description: 验证定时任务定义/概览及运行记录的状态分页、稳定顺序和用户访问隔离。
"""

from datetime import datetime, timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.scheduled_tasks import (
    overview_enterprise_scheduled_tasks,
    page_enterprise_scheduled_task_runs,
    page_enterprise_scheduled_tasks,
)
from app.db.models import ScheduledTask, ScheduledTaskRun, Tenant, User


def test_scheduled_task_page_returns_status_counts_and_stable_pages() -> None:
    """验证任务状态统计基于完整访问范围，而列表按过滤状态稳定分页。"""

    with _test_session() as db:
        admin = _seed_users_and_task(db)
        base_time = datetime(2026, 8, 1, 10, 0)
        db.add_all(
            [
                _task("task_completed", "owner_user", "completed", base_time + timedelta(minutes=2)),
                _task("task_paused", "other_user", "paused", base_time + timedelta(minutes=1)),
                ScheduledTask(
                    id="task_archived",
                    tenant_id="tenant_demo",
                    agent_id="agent_demo",
                    created_by_user_id="owner_user",
                    title="归档任务",
                    prompt="不进入分页",
                    status="archived",
                    updated_at=base_time + timedelta(minutes=4),
                ),
                ScheduledTask(
                    id="task_other_agent",
                    tenant_id="tenant_demo",
                    agent_id="agent_other",
                    created_by_user_id="owner_user",
                    title="其他员工任务",
                    prompt="不进入当前员工",
                    status="active",
                    updated_at=base_time + timedelta(minutes=3),
                ),
            ]
        )
        db.commit()

        result = page_enterprise_scheduled_tasks(
            "tenant_demo", "agent_demo", "all", 1, 1, admin, db
        )
        completed = page_enterprise_scheduled_tasks(
            "tenant_demo", "agent_demo", "completed", 1, 10, admin, db
        )
        overview = overview_enterprise_scheduled_tasks(
            "tenant_demo", "agent_demo", admin, db
        )

    assert result.total == 3
    assert result.status_counts == {"active": 1, "completed": 1, "paused": 1}
    assert [item.id for item in result.items] == ["task_completed"]
    assert [item.id for item in completed.items] == ["task_completed"]
    assert overview.active_count == 1
    assert [item.id for item in overview.active_items] == ["task_demo"]


def test_scheduled_task_page_and_overview_scope_non_admin_to_created_tasks() -> None:
    """验证普通用户的分页统计与概览都不会包含其他创建者的任务。"""

    with _test_session() as db:
        _seed_users_and_task(db)
        owner = db.get(User, "owner_user")
        assert owner is not None
        db.add(_task("task_other", "other_user", "paused", datetime(2026, 8, 1, 11, 0)))
        db.commit()

        result = page_enterprise_scheduled_tasks(
            "tenant_demo", "agent_demo", "all", 1, 10, owner, db
        )
        overview = overview_enterprise_scheduled_tasks(
            "tenant_demo", "agent_demo", owner, db
        )

    assert result.total == 1
    assert result.status_counts == {"active": 1}
    assert [item.id for item in result.items] == ["task_demo"]
    assert overview.active_count == 1


def test_scheduled_run_page_filters_before_count_and_returns_unfiltered_total() -> None:
    """验证状态组在分页前生效，同时 run_total 保留当前员工范围的全部运行量。"""

    with _test_session() as db:
        admin = _seed_users_and_task(db)
        base_time = datetime(2026, 8, 1, 10, 0)
        db.add_all(
            [
                _run("run_1", "owner_user", "succeeded", base_time + timedelta(minutes=3)),
                _run("run_2", "owner_user", "running", base_time + timedelta(minutes=2)),
                _run("run_3", "other_user", "queued", base_time + timedelta(minutes=1)),
                _run("run_waiting", "owner_user", "waiting", base_time),
                ScheduledTaskRun(
                    id="run_other_agent",
                    tenant_id="tenant_demo",
                    scheduled_task_id="task_demo",
                    agent_id="agent_other",
                    user_id="owner_user",
                    source_kind="legacy",
                    source_ref="legacy:run_other_agent",
                    source_snapshot_json={},
                    source_checksum="legacy-run-other-agent",
                    scheduled_for=base_time + timedelta(minutes=4),
                    status="running",
                ),
            ]
        )
        db.commit()

        result = page_enterprise_scheduled_task_runs(
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            task_id=None,
            status_filter="pending",
            page=1,
            page_size=1,
            current_user=admin,
            db=db,
        )
        second_page = page_enterprise_scheduled_task_runs(
            "tenant_demo", "agent_demo", None, "pending", 2, 1, admin, db
        )

    assert result.run_total == 4
    assert result.total == 3
    assert [item.id for item in result.items] == ["run_2"]
    assert [item.id for item in second_page.items] == ["run_3"]
    assert result.items[0].task_title == "日报任务"


def test_scheduled_run_page_scopes_non_admin_and_task_access() -> None:
    """验证普通用户只能读取自己的运行记录，且不能借 task_id 访问他人任务。"""

    with _test_session() as db:
        _seed_users_and_task(db)
        owner = db.get(User, "owner_user")
        other = db.get(User, "other_user")
        assert owner is not None and other is not None
        base_time = datetime(2026, 8, 1, 10, 0)
        db.add_all(
            [
                _run("run_owner", owner.id, "succeeded", base_time),
                _run("run_other", other.id, "failed", base_time + timedelta(minutes=1)),
            ]
        )
        db.commit()

        result = page_enterprise_scheduled_task_runs(
            "tenant_demo", "agent_demo", None, "all", 1, 10, owner, db
        )

        from fastapi import HTTPException

        try:
            page_enterprise_scheduled_task_runs(
                "tenant_demo", "agent_demo", "task_demo", "all", 1, 10, other, db
            )
        except HTTPException as error:
            forbidden_status = error.status_code
        else:
            forbidden_status = None

    assert result.run_total == 1
    assert [item.id for item in result.items] == ["run_owner"]
    assert forbidden_status == 403


def _seed_users_and_task(db: Session) -> User:
    """创建测试租户、管理员、普通用户及归属于 owner 的定时任务。"""

    admin = User(
        id="admin_user",
        tenant_id="tenant_demo",
        username="admin",
        password_hash="hash",
        role="admin",
    )
    owner = User(
        id="owner_user",
        tenant_id="tenant_demo",
        username="owner",
        password_hash="hash",
    )
    other = User(
        id="other_user",
        tenant_id="tenant_demo",
        username="other",
        password_hash="hash",
    )
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add_all([admin, owner, other])
    db.add(
        ScheduledTask(
            id="task_demo",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            created_by_user_id=owner.id,
            title="日报任务",
            prompt="生成日报",
            updated_at=datetime(2026, 8, 1, 9, 0),
        )
    )
    db.commit()
    return admin


def _run(run_id: str, user_id: str, status: str, scheduled_for: datetime) -> ScheduledTaskRun:
    """构造指定用户、状态和计划时间的运行记录。"""

    return ScheduledTaskRun(
        id=run_id,
        tenant_id="tenant_demo",
        scheduled_task_id="task_demo",
        agent_id="agent_demo",
        user_id=user_id,
        source_kind="legacy",
        source_ref=f"legacy:{run_id}",
        source_snapshot_json={},
        source_checksum=f"legacy-{run_id}",
        scheduled_for=scheduled_for,
        status=status,
    )


def _task(
    task_id: str,
    creator_id: str,
    status: str,
    updated_at: datetime,
) -> ScheduledTask:
    """构造当前测试员工下指定创建者和状态的任务定义。"""

    return ScheduledTask(
        id=task_id,
        tenant_id="tenant_demo",
        agent_id="agent_demo",
        created_by_user_id=creator_id,
        title=task_id,
        prompt="测试任务",
        status=status,
        updated_at=updated_at,
    )


def _test_session() -> Session:
    """创建带完整项目元数据的内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
