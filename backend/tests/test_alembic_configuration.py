"""
@Time       : 2026/08/10 17:15
@Author     : zhanglp8181
@File       : test_alembic_configuration.py
@CallChain  : pytest → Alembic ScriptDirectory → 迁移链和配置安全断言
@Description: 验证数据库迁移单头顺序以及迁移文件不包含运行凭据。
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_alembic_has_single_expected_head_and_baseline() -> None:
    """验证迁移链只有一个头，并保持基线到身份角色迁移的连续顺序。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["20260814_0063"]
    member_lifecycle = script.get_revision("20260728_0016")
    assert member_lifecycle.down_revision == "20260727_0015"
    organization_units = script.get_revision("20260728_0017")
    assert organization_units.down_revision == "20260728_0016"
    organization_assignments = script.get_revision("20260728_0018")
    assert organization_assignments.down_revision == "20260728_0017"
    position_role_bindings = script.get_revision("20260728_0019")
    assert position_role_bindings.down_revision == "20260728_0018"
    organization_leaders = script.get_revision("20260728_0020")
    assert organization_leaders.down_revision == "20260728_0019"
    organization_query_indexes = script.get_revision("20260728_0021")
    assert organization_query_indexes.down_revision == "20260728_0020"
    governance_role_scopes = script.get_revision("20260728_0022")
    assert governance_role_scopes.down_revision == "20260728_0021"
    sop_participant_scope = script.get_revision("20260728_0023")
    assert sop_participant_scope.down_revision == "20260728_0022"
    agent_identity = script.get_revision("20260728_0024")
    assert agent_identity.down_revision == "20260728_0023"
    knowledge_governance = script.get_revision("20260728_0025")
    assert knowledge_governance.down_revision == "20260728_0024"
    management_audit = script.get_revision("20260729_0026")
    assert management_audit.down_revision == "20260728_0025"
    legacy_published_snapshots = script.get_revision("20260729_0027")
    assert legacy_published_snapshots.down_revision == "20260729_0026"
    agent_responsibility = script.get_revision("20260729_0028")
    assert agent_responsibility.down_revision == "20260729_0027"
    effective_interval_precision = script.get_revision("20260729_0029")
    assert effective_interval_precision.down_revision == "20260729_0028"
    acceptance_asset_retirement = script.get_revision("20260729_0030")
    assert acceptance_asset_retirement.down_revision == "20260729_0029"
    memory_agent_pagination = script.get_revision("20260801_0031")
    assert memory_agent_pagination.down_revision == "20260729_0030"
    scheduled_run_pagination = script.get_revision("20260801_0032")
    assert scheduled_run_pagination.down_revision == "20260801_0031"
    scheduled_task_pagination = script.get_revision("20260801_0033")
    assert scheduled_task_pagination.down_revision == "20260801_0032"
    agent_gallery_pagination = script.get_revision("20260801_0034")
    assert agent_gallery_pagination.down_revision == "20260801_0033"
    role_binding_intervals = script.get_revision("20260802_0035")
    assert role_binding_intervals.down_revision == "20260801_0034"
    execution_ownership = script.get_revision("20260803_0036")
    assert execution_ownership.down_revision == "20260802_0035"
    operation_reliability = script.get_revision("20260803_0037")
    assert operation_reliability.down_revision == "20260803_0036"
    capability_catalog = script.get_revision("20260803_0038")
    assert capability_catalog.down_revision == "20260803_0037"
    execution_plan_resources = script.get_revision("20260803_0039")
    assert execution_plan_resources.down_revision == "20260803_0038"
    execution_control = script.get_revision("20260803_0040")
    assert execution_control.down_revision == "20260803_0039"
    execution_artifacts = script.get_revision("20260804_0041")
    assert execution_artifacts.down_revision == "20260803_0040"
    connection_profiles = script.get_revision("20260810_0042")
    assert connection_profiles.down_revision == "20260804_0041"
    connector_inbound = script.get_revision("20260810_0043")
    assert connector_inbound.down_revision == "20260810_0042"
    connector_delivery = script.get_revision("20260810_0044")
    assert connector_delivery.down_revision == "20260810_0043"
    controlled_writes = script.get_revision("20260810_0045")
    assert controlled_writes.down_revision == "20260810_0044"
    scheduled_dynamic_runs = script.get_revision("20260811_0046")
    assert scheduled_dynamic_runs.down_revision == "20260810_0045"
    standing_approvals = script.get_revision("20260811_0047")
    assert standing_approvals is not None
    assert standing_approvals.down_revision == "20260811_0046"
    quota_leases = script.get_revision("20260811_0048")
    assert quota_leases is not None
    assert quota_leases.down_revision == "20260811_0047"
    skill_revision_foundation = script.get_revision("20260811_0049")
    assert skill_revision_foundation.down_revision == "20260811_0048"
    skill_dependencies = script.get_revision("20260812_0050")
    assert skill_dependencies.down_revision == "20260811_0049"
    skill_import_quotas = script.get_revision("20260812_0051")
    assert skill_import_quotas.down_revision == "20260812_0050"
    skill_retry_chain = script.get_revision("20260812_0052")
    assert skill_retry_chain.down_revision == "20260812_0051"
    skill_worker_lease = script.get_revision("20260812_0053")
    assert skill_worker_lease.down_revision == "20260812_0052"
    skill_source_credentials = script.get_revision("20260812_0054")
    assert skill_source_credentials.down_revision == "20260812_0053"
    skill_authorization_state = script.get_revision("20260812_0055")
    assert skill_authorization_state.down_revision == "20260812_0054"
    skill_upgrade_target = script.get_revision("20260813_0056")
    assert skill_upgrade_target.down_revision == "20260812_0055"
    skill_runtime = script.get_revision("20260813_0057")
    assert skill_runtime.down_revision == "20260813_0056"
    approval_requests = script.get_revision("20260727_0015")
    assert approval_requests.down_revision == "20260727_0014"
    unique_default_model = script.get_revision("20260727_0014")
    assert unique_default_model.down_revision == "20260722_0013"
    tool_authorization_mode = script.get_revision("20260722_0013")
    assert tool_authorization_mode.down_revision == "20260722_0012"
    tool_execution_permission = script.get_revision("20260722_0012")
    assert tool_execution_permission.down_revision == "20260722_0011"
    action_permissions = script.get_revision("20260722_0011")
    assert action_permissions.down_revision == "20260722_0010"
    permission_catalog = script.get_revision("20260722_0010")
    assert permission_catalog.down_revision == "20260722_0009"
    outcome_options = script.get_revision("20260722_0009")
    assert outcome_options.down_revision == "20260722_0008"
    work_item_timeout = script.get_revision("20260722_0008")
    assert work_item_timeout.down_revision == "20260722_0007"
    work_items = script.get_revision("20260722_0007")
    business_roles = script.get_revision("20260722_0006")
    employee_profiles = script.get_revision("20260722_0005")
    runtime_execution = script.get_revision("20260722_0004")
    immutable_versions = script.get_revision("20260722_0003")
    head = script.get_revision("20260718_0002")
    baseline = script.get_revision("20260718_0001")
    assert work_items is not None and work_items.down_revision == "20260722_0006"
    assert business_roles is not None and business_roles.down_revision == "20260722_0005"
    assert employee_profiles is not None and employee_profiles.down_revision == "20260722_0004"
    assert runtime_execution is not None and runtime_execution.down_revision == "20260722_0003"
    assert immutable_versions is not None and immutable_versions.down_revision == "20260718_0002"
    assert head is not None and head.down_revision == "20260718_0001"
    assert baseline is not None and baseline.down_revision is None


def test_alembic_files_do_not_embed_runtime_credentials() -> None:
    """验证 Alembic 配置和迁移脚本没有写入真实数据库凭据。"""

    paths = [BACKEND_DIR / "alembic.ini", *sorted((BACKEND_DIR / "alembic").rglob("*.py"))]
    contents = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert "MYSQL_ROOT_PASSWORD" not in contents
    assert "mysql+pymysql://root:" not in contents
    assert "mysql+pymysql://gongge_xuban:" not in contents
