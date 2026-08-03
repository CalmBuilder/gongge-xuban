import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  BriefcaseBusiness,
  CircleAlert,
  GitBranch,
  Plus,
  RefreshCw,
  ShieldCheck,
  UserRoundPlus,
  Workflow,
} from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { OrganizationTreeNavigator } from '@/components/OrganizationTreeNavigator';
import { Paginator } from '@/components/Paginator';
import { RemoteMemberSelect } from '@/components/RemoteMemberSelect';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
} from '@/components/ui';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';
import { formatDateTime } from '@/lib/enterprise-ui';

import { api } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import { useEnterpriseContext } from '../enterprise-context';
import type {
  BusinessRoleOption,
  CodeOption,
  OrganizationAssignment,
  OrganizationMember,
  OrganizationUnit,
  Position,
  PositionAssignment,
  PositionRoleBinding,
  OrganizationLeaderAssignment,
  OrganizationLeaderType,
  OrganizationSummary,
  MemberPage,
  OrganizationSearchResult,
} from '../types/organization';
import type {
  PositionRoleSopImpact,
  SopDependencyCoverageReport,
} from '../types/sop-coverage';

type DialogMode = 'unit' | 'move' | 'position' | 'member' | 'role' | null;
type StopTarget = {
  kind: 'unit' | 'position' | 'org-assignment' | 'position-assignment' | 'binding' | 'leader';
  id: string;
  name: string;
  impactSummary?: string;
} | null;

const EMPTY_UNIT = { code: '', name: '', type: 'department' };
const EMPTY_POSITION = {
  code: '',
  name: '',
  type: 'professional',
  responsibility: '',
};
const ORGANIZATION_MEMBER_PAGE_SIZE = 50;

export default function OrganizationPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const { tenant } = useEnterpriseContext();
  const [units, setUnits] = useState<OrganizationUnit[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [orgAssignments, setOrgAssignments] = useState<OrganizationAssignment[]>([]);
  const [positionAssignments, setPositionAssignments] = useState<PositionAssignment[]>([]);
  const [bindings, setBindings] = useState<PositionRoleBinding[]>([]);
  const [members, setMembers] = useState<OrganizationMember[]>([]);
  const [memberPage, setMemberPage] = useState(1);
  const [memberTotal, setMemberTotal] = useState(0);
  const [roles, setRoles] = useState<BusinessRoleOption[]>([]);
  const [unitTypes, setUnitTypes] = useState<CodeOption[]>([]);
  const [positionTypes, setPositionTypes] = useState<CodeOption[]>([]);
  const [leaderTypes, setLeaderTypes] = useState<OrganizationLeaderType[]>([]);
  const [leaders, setLeaders] = useState<OrganizationLeaderAssignment[]>([]);
  const [summary, setSummary] = useState<OrganizationSummary | null>(null);
  const [coverage, setCoverage] = useState<SopDependencyCoverageReport | null>(null);
  const [selectedUnitId, setSelectedUnitId] = useState('');
  const [selectedPositionId, setSelectedPositionId] = useState('');
  const [dialogMode, setDialogMode] = useState<DialogMode>(null);
  const [unitDraft, setUnitDraft] = useState(EMPTY_UNIT);
  const [positionDraft, setPositionDraft] = useState(EMPTY_POSITION);
  const [selectedProfileId, setSelectedProfileId] = useState('');
  const [selectedRoleId, setSelectedRoleId] = useState('');
  const [positionRoleEffectiveUntil, setPositionRoleEffectiveUntil] = useState('');
  const [selectedParentId, setSelectedParentId] = useState('');
  const [stopTarget, setStopTarget] = useState<StopTarget>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [treeRefreshToken, setTreeRefreshToken] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    const tenantId = encodeURIComponent(tenant.id);
    try {
      const [
        rolesResult,
        unitTypesResult,
        positionTypesResult,
        leaderTypesResult,
        coverageResult,
      ] = await Promise.allSettled([
        api.get<BusinessRoleOption[]>(
          `/api/organization/business-roles?tenant_id=${tenantId}`,
        ),
        api.get<CodeOption[]>(`/api/organization/unit-types?tenant_id=${tenantId}`),
        api.get<CodeOption[]>(`/api/organization/position-types?tenant_id=${tenantId}`),
        api.get<OrganizationLeaderType[]>(`/api/organization/leader-types?tenant_id=${tenantId}`),
        api.get<SopDependencyCoverageReport>(
          `/api/sop-migrations/coverage?tenant_id=${tenantId}`,
        ),
      ]);
      const failedResources: string[] = [];
      const applyResult = <T,>(
        result: PromiseSettledResult<T>,
        resourceName: string,
        setter: (value: T) => void,
      ) => {
        if (result.status === 'fulfilled') {
          setter(result.value);
          return result.value;
        }
        failedResources.push(resourceName);
        return null;
      };

      applyResult(rolesResult, '业务角色', setRoles);
      applyResult(unitTypesResult, '组织类型', setUnitTypes);
      applyResult(positionTypesResult, '岗位类型', setPositionTypes);
      applyResult(leaderTypesResult, '负责人类型', setLeaderTypes);
      applyResult(coverageResult, 'SOP 责任覆盖', setCoverage);
      if (failedResources.length > 0) {
        notify.error(`部分组织数据加载失败：${failedResources.join('、')}。请检查服务版本。`);
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '组织数据加载失败');
    } finally {
      setLoading(false);
    }
  }, [tenant.id]);

  useEffect(() => {
    void load();
  }, [load]);

  const selectOrganization = useCallback((unit: OrganizationUnit) => {
    setUnits((current) => (
      current.some((item) => item.id === unit.id) ? current : [...current, unit]
    ));
    setSelectedUnitId(unit.id);
    setSelectedPositionId('');
    setMemberPage(1);
  }, []);

  const loadOrganizationContext = useCallback(async () => {
    if (!selectedUnitId) return;
    const tenantId = encodeURIComponent(tenant.id);
    const orgId = encodeURIComponent(selectedUnitId);
    const [
      summaryResult,
      positionsResult,
      orgAssignmentsResult,
      positionAssignmentsResult,
    ] = await Promise.allSettled([
      api.get<OrganizationSummary>(
        `/api/organization/unit-summary?tenant_id=${tenantId}&org_unit_id=${orgId}`,
      ),
      api.get<Position[]>(
        `/api/organization/positions?tenant_id=${tenantId}&org_unit_id=${orgId}`,
      ),
      api.get<MemberPage<OrganizationAssignment>>(
        `/api/organization/member-org-assignments/page?tenant_id=${tenantId}`
        + `&org_unit_id=${orgId}&page=${memberPage}&page_size=${ORGANIZATION_MEMBER_PAGE_SIZE}`,
      ),
      api.get<PositionAssignment[]>(
        `/api/organization/position-assignments?tenant_id=${tenantId}&org_unit_id=${orgId}`,
      ),
    ]);
    const failures: string[] = [];
    if (summaryResult.status === 'fulfilled') setSummary(summaryResult.value);
    else failures.push('组织摘要');
    if (positionsResult.status === 'fulfilled') setPositions(positionsResult.value);
    else failures.push('岗位');
    if (orgAssignmentsResult.status === 'fulfilled') {
      setOrgAssignments(orgAssignmentsResult.value.items);
      setMemberTotal(orgAssignmentsResult.value.total);
      setMembers(orgAssignmentsResult.value.items.map((assignment) => ({
        id: assignment.user_id || assignment.employee_profile_id,
        username: assignment.username || '',
        display_name: assignment.display_name,
        employee_profile_id: assignment.employee_profile_id,
        employee_id: assignment.employee_id,
        employee_name: assignment.employee_name,
        membership_status: 'active',
      })));
    }
    else failures.push('组织归属');
    if (positionAssignmentsResult.status === 'fulfilled') setPositionAssignments(positionAssignmentsResult.value);
    else failures.push('岗位任职');
    if (failures.length) notify.error(`当前组织部分数据加载失败：${failures.join('、')}。`);
  }, [memberPage, selectedUnitId, tenant.id]);

  useEffect(() => {
    void loadOrganizationContext();
  }, [loadOrganizationContext]);

  const loadPositionBindings = useCallback(async () => {
    if (!selectedPositionId) {
      setBindings([]);
      return;
    }
    try {
      setBindings(await api.get<PositionRoleBinding[]>(
        `/api/organization/position-role-bindings?tenant_id=${encodeURIComponent(tenant.id)}`
        + `&position_id=${encodeURIComponent(selectedPositionId)}`,
      ));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '岗位角色加载失败');
    }
  }, [selectedPositionId, tenant.id]);

  useEffect(() => {
    void loadPositionBindings();
  }, [loadPositionBindings]);

  const loadLeaders = useCallback(async () => {
    if (!selectedUnitId) {
      setLeaders([]);
      return;
    }
    try {
      setLeaders(await api.get<OrganizationLeaderAssignment[]>(
        `/api/organization/leader-assignments?tenant_id=${encodeURIComponent(tenant.id)}`
        + `&org_unit_id=${encodeURIComponent(selectedUnitId)}&include_history=true`,
      ));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '负责人记录加载失败');
    }
  }, [selectedUnitId, tenant.id]);

  useEffect(() => {
    void loadLeaders();
  }, [loadLeaders]);

  const selectedUnit = units.find((unit) => unit.id === selectedUnitId);
  const unitPositions = useMemo(
    () => positions.filter((position) => position.org_unit_id === selectedUnitId),
    [positions, selectedUnitId],
  );
  const selectedPosition = positions.find((position) => position.id === selectedPositionId);
  const memberByProfile = useMemo(
    () => new Map(
      members
        .filter((member) => member.employee_profile_id)
        .map((member) => [member.employee_profile_id as string, member]),
    ),
    [members],
  );
  const currentOrgAssignments = useMemo(
    () => orgAssignments.filter(
      (assignment) => assignment.org_unit_id === selectedUnitId && assignment.status === 'active',
    ),
    [orgAssignments, selectedUnitId],
  );
  const selectedPositionAssignments = useMemo(
    () => positionAssignments.filter(
      (assignment) => assignment.position_id === selectedPositionId,
    ),
    [positionAssignments, selectedPositionId],
  );
  const selectedBindings = useMemo(
    () => bindings.filter(
      (binding) => binding.position_id === selectedPositionId && binding.status === 'active',
    ),
    [bindings, selectedPositionId],
  );
  const roleImpactsByCode = useMemo(() => {
    const result = new Map<string, PositionRoleSopImpact[]>();
    for (const entry of coverage?.entries || []) {
      for (const participant of entry.dependency_assessment.human_participants) {
        for (const roleCode of participant.role_codes) {
          const impacts = result.get(roleCode) || [];
          impacts.push({
            skillId: entry.skill_id,
            skillName: entry.name,
            version: entry.current_version,
            readiness: entry.dependency_assessment.readiness,
            participant,
          });
          result.set(roleCode, impacts);
        }
      }
    }
    return result;
  }, [coverage]);
  const selectedPositionImpacts = useMemo(
    () => selectedBindings.flatMap(
      (binding) => roleImpactsByCode.get(binding.business_role_code) || [],
    ),
    [roleImpactsByCode, selectedBindings],
  );

  function openDialog(mode: Exclude<DialogMode, null>) {
    setUnitDraft(EMPTY_UNIT);
    setPositionDraft(EMPTY_POSITION);
    setSelectedProfileId('');
    setSelectedRoleId('');
    setPositionRoleEffectiveUntil('');
    setSelectedParentId('');
    setDialogMode(mode);
  }

  async function saveDialog() {
    if (!selectedUnit) return;
    setSaving(true);
    try {
      if (dialogMode === 'unit') {
        await api.post('/api/organization/units', {
          tenant_id: tenant.id,
          parent_id: selectedUnit.id,
          code: unitDraft.code.trim(),
          name: unitDraft.name.trim(),
          unit_type_code: unitDraft.type,
        });
        notify.success(`已在“${selectedUnit.name}”下创建组织`);
      } else if (dialogMode === 'move') {
        await api.put(`/api/organization/units/${selectedUnit.id}`, {
          tenant_id: tenant.id,
          parent_id: selectedParentId,
        });
        notify.success('组织已移动，子树路径已同步更新');
      } else if (dialogMode === 'position') {
        await api.post('/api/organization/positions', {
          tenant_id: tenant.id,
          org_unit_id: selectedUnit.id,
          code: positionDraft.code.trim(),
          name: positionDraft.name.trim(),
          position_type_code: positionDraft.type,
          responsibility: positionDraft.responsibility.trim() || undefined,
        });
        notify.success('岗位已创建');
      } else if (dialogMode === 'member') {
        await api.post('/api/organization/member-org-assignments', {
          tenant_id: tenant.id,
          employee_profile_id: selectedProfileId,
          org_unit_id: selectedUnit.id,
          assignment_type: 'primary',
        });
        notify.success('成员主组织已调整，原任期已归档');
      } else if (dialogMode === 'role' && selectedPosition) {
        await api.post('/api/organization/position-role-bindings', {
          tenant_id: tenant.id,
          position_id: selectedPosition.id,
          business_role_id: selectedRoleId,
          effective_until: positionRoleEffectiveUntil
            ? new Date(positionRoleEffectiveUntil).toISOString()
            : undefined,
        });
        notify.success('岗位默认角色已绑定');
      }
      setDialogMode(null);
      if (dialogMode === 'unit' || dialogMode === 'move') {
        setTreeRefreshToken((current) => current + 1);
      }
      await Promise.all([
        load(),
        loadOrganizationContext(),
        ...(dialogMode === 'role' ? [loadPositionBindings()] : []),
      ]);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setSaving(false);
    }
  }

  async function confirmStop() {
    if (!stopTarget) return;
    setSaving(true);
    try {
      if (stopTarget.kind === 'unit') {
        await api.delete(
          `/api/organization/units/${stopTarget.id}?tenant_id=${encodeURIComponent(tenant.id)}`,
        );
      } else if (stopTarget.kind === 'position') {
        await api.delete(
          `/api/organization/positions/${stopTarget.id}?tenant_id=${encodeURIComponent(tenant.id)}`,
        );
      } else if (stopTarget.kind === 'org-assignment') {
        await api.post(`/api/organization/member-org-assignments/${stopTarget.id}/end`, {
          tenant_id: tenant.id,
        });
      } else if (stopTarget.kind === 'position-assignment') {
        await api.post(`/api/organization/position-assignments/${stopTarget.id}/end`, {
          tenant_id: tenant.id,
        });
      } else if (stopTarget.kind === 'leader') {
        await api.post(`/api/organization/leader-assignments/${stopTarget.id}/end`, {
          tenant_id: tenant.id,
        });
      } else {
        await api.delete(
          `/api/organization/position-role-bindings/${stopTarget.id}?tenant_id=${encodeURIComponent(tenant.id)}`,
        );
      }
      notify.success(`${stopTarget.name}已结束`);
      setStopTarget(null);
      setTreeRefreshToken((current) => current + 1);
      await Promise.all([load(), loadOrganizationContext(), loadLeaders()]);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '操作失败');
    } finally {
      setSaving(false);
    }
  }

  async function assignPosition(profileId: string) {
    if (!selectedPosition) return;
    setSaving(true);
    try {
      await api.post('/api/organization/position-assignments', {
        tenant_id: tenant.id,
        employee_profile_id: profileId,
        position_id: selectedPosition.id,
        assignment_type: 'primary',
      });
      notify.success('主岗位已调整，原岗位任期已归档');
      await loadOrganizationContext();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '岗位任职失败');
    } finally {
      setSaving(false);
    }
  }

  async function assignLeader(
    profileId: string,
    leaderTypeCode: string,
    positionAssignmentId: string,
    effectiveUntil: string,
  ) {
    if (!selectedUnit) return;
    setSaving(true);
    try {
      await api.post('/api/organization/leader-assignments', {
        tenant_id: tenant.id,
        org_unit_id: selectedUnit.id,
        employee_profile_id: profileId,
        leader_type_code: leaderTypeCode,
        position_assignment_id: positionAssignmentId || undefined,
        effective_until: effectiveUntil ? new Date(effectiveUntil).toISOString() : undefined,
      });
      notify.success('负责人关系已创建；该关系不会自动产生角色或流程权限');
      await loadLeaders();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '负责人配置失败');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="min-h-full box-border px-[32px] pt-[28px] pb-[40px] max-[900px]:px-[14px]">
      <AppHeader onLogout={onLogout} userName={currentUser?.username} title="组织与岗位" />

      <div className="mt-[18px] overflow-hidden rounded-[24px] border border-[#e5e9f2] bg-white shadow-[0_22px_60px_rgba(32,48,86,0.08)]">
        <header className="flex items-center justify-between gap-[18px] border-b border-[#edf0f6] bg-[linear-gradient(110deg,#f4f7ff_0%,#ffffff_58%,#fff9ed_100%)] px-[24px] py-[20px]">
          <div>
            <div className="flex items-center gap-[8px] text-[11px] font-semibold tracking-[0.16em] text-[#68769e]">
              <GitBranch className="size-[14px]" />
              企业责任链
            </div>
            <h1 className="mt-[7px] text-[20px] font-semibold tracking-[-0.02em] text-[#1d2433]">
              谁在什么组织、以什么岗位承担工作
            </h1>
            <p className="mt-[5px] text-[12px] text-[#778095]">
              调岗追加任期历史；岗位默认角色决定流程候选资格，不改变平台管理员身份。
            </p>
          </div>
          <Button variant="outline" disabled={loading} onClick={() => void load()}>
            <RefreshCw className={cn('size-[15px]', loading && 'animate-spin')} />
            刷新
          </Button>
        </header>

        <div className="grid min-h-[610px] grid-cols-[280px_minmax(0,1fr)] max-[980px]:grid-cols-1">
          <aside className="border-r border-[#edf0f6] bg-[#fbfcff] p-[16px] max-[980px]:border-r-0 max-[980px]:border-b">
            <div className="flex items-center justify-between px-[6px] pb-[12px]">
              <span className="text-[12px] font-semibold text-[#424b61]">企业组织树</span>
              <Button
                size="sm"
                variant="ghost"
                disabled={!selectedUnit || selectedUnit.status !== 'active'}
                onClick={() => openDialog('unit')}
              >
                <Plus className="size-[14px]" />
                下级
              </Button>
            </div>
            <OrganizationTreeNavigator
              onSelect={selectOrganization}
              refreshToken={treeRefreshToken}
              selectedId={selectedUnitId}
              tenantId={tenant.id}
            />
          </aside>

          <main className="min-w-0 p-[22px]">
            {selectedUnit ? (
              <>
                <div className="flex flex-wrap items-start justify-between gap-[12px] border-b border-[#edf0f6] pb-[17px]">
                  <div>
                    <div className="flex items-center gap-[8px]">
                      <h2 className="text-[18px] font-semibold text-[#202637]">{selectedUnit.name}</h2>
                      <span className="rounded-full bg-[#f1f4fa] px-[8px] py-[3px] font-mono text-[10px] text-[#727b90]">
                        {selectedUnit.code}
                      </span>
                    </div>
                    <p className="mt-[5px] text-[11px] text-[#8a92a4]">
                      直属 {summary?.direct_member_count ?? '—'} 人 · 子树 {summary?.subtree_member_count ?? '—'} 人
                      {' · '}{unitPositions.length} 个岗位 · {summary?.current_leader_count ?? '—'} 名当前负责人
                    </p>
                  </div>
                  <div className="flex gap-[8px]">
                    {!selectedUnit.is_root ? (
                      <>
                        <Button variant="outline" onClick={() => openDialog('move')}>
                          移动组织
                        </Button>
                        <Button
                          variant="outline"
                          onClick={() => setStopTarget({
                            kind: 'unit',
                            id: selectedUnit.id,
                            name: selectedUnit.name,
                          })}
                        >
                          停用组织
                        </Button>
                      </>
                    ) : null}
                    <Button variant="outline" onClick={() => openDialog('member')}>
                      <UserRoundPlus className="size-[14px]" />
                      调入成员
                    </Button>
                    <Button onClick={() => openDialog('position')}>
                      <BriefcaseBusiness className="size-[14px]" />
                      新建岗位
                    </Button>
                  </div>
                </div>

                <section className="mt-[18px]">
                  <DetailPanel
                    title="组织负责人（当前与历史）"
                    icon={<UserRoundPlus className="size-[15px]" />}
                    action={<span className="text-[9px] text-[#9299aa]">责任关系，不自动授予角色或权限</span>}
                  >
                    {leaders.length ? leaders.map((leader) => {
                      const member = memberByProfile.get(leader.employee_profile_id);
                      const type = leaderTypes.find((item) => item.code === leader.leader_type_code);
                      return (
                        <div key={leader.id} className="flex items-center gap-[10px] border-b border-[#eef1f5] py-[9px] last:border-0">
                          <span className={cn(
                            'size-[7px] rounded-full',
                            leader.status === 'active' ? 'bg-[#2eaf72]' : 'bg-[#c7ccd6]',
                          )} />
                          <div className="min-w-0 flex-1">
                            <p className="truncate text-[12px] font-medium text-[#3d4558]">
                              {member?.employee_name || member?.display_name || '成员信息未加载'}
                              <span className="ml-[7px] text-[10px] font-normal text-[#6677a8]">
                                {type?.name || leader.leader_type_code}
                              </span>
                            </p>
                            <p className="mt-[2px] text-[9px] text-[#949bad]">
                              {formatDateTime(leader.effective_from)}
                              {leader.effective_until ? ` — ${formatDateTime(leader.effective_until)}` : ' — 至今'}
                            </p>
                          </div>
                          {leader.status === 'active' ? (
                            <button
                              type="button"
                              className="text-[10px] text-[#9a6370] hover:text-[#bd2948]"
                              onClick={() => setStopTarget({
                                kind: 'leader',
                                id: leader.id,
                                name: `${member?.employee_name || '成员'}的负责人任期`,
                              })}
                            >
                              结束
                            </button>
                          ) : null}
                        </div>
                      );
                    }) : (
                      <p className="py-[14px] text-[11px] text-[#8b93a5]">
                        尚未明确负责人。平台不会根据岗位名称或组织层级自动推断。
                      </p>
                    )}
                    <LeaderPicker
                      key={`leader-${selectedUnit.id}-${currentOrgAssignments.length}`}
                      tenantId={tenant.id}
                      orgUnitId={selectedUnit.id}
                      leaderTypes={leaderTypes}
                      positionAssignments={positionAssignments}
                      positions={unitPositions}
                      hasPrimary={leaders.some(
                        (leader) => leader.status === 'active' && leader.leader_type_code === 'primary',
                      )}
                      disabled={saving}
                      onAssign={assignLeader}
                    />
                  </DetailPanel>
                </section>

                <section className="mt-[18px]">
                  <div className="mb-[10px] flex items-center justify-between">
                    <h3 className="text-[12px] font-semibold text-[#4e566b]">岗位目录</h3>
                    <span className="text-[10px] text-[#9299aa]">选择岗位查看任职与默认角色</span>
                  </div>
                  {unitPositions.length ? (
                    <div className="grid grid-cols-2 gap-[10px] max-[760px]:grid-cols-1">
                      {unitPositions.map((position) => (
                        <button
                          key={position.id}
                          type="button"
                          aria-pressed={position.id === selectedPositionId}
                          className={cn(
                            'rounded-[14px] border p-[14px] text-left transition-all',
                            position.id === selectedPositionId
                              ? 'border-[#3157e8] bg-[#f5f7ff] shadow-[0_8px_20px_rgba(49,87,232,0.10)]'
                              : 'border-[#e7eaf1] hover:border-[#cbd4e8]',
                          )}
                          onClick={() => setSelectedPositionId(position.id)}
                        >
                          <div className="flex items-center justify-between gap-[10px]">
                            <span className="text-[13px] font-semibold text-[#303749]">{position.name}</span>
                            <span className="font-mono text-[9px] text-[#8c94a6]">{position.code}</span>
                          </div>
                          <p className="mt-[7px] line-clamp-2 text-[11px] leading-[17px] text-[#7b8395]">
                            {position.responsibility || '尚未填写岗位职责'}
                          </p>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <EmptyState text="这个组织还没有岗位。先创建岗位，再配置任职与默认角色。" />
                  )}
                </section>

                <section className="mt-[18px]">
                  <h3 className="mb-[9px] text-[12px] font-semibold text-[#4e566b]">当前组织成员</h3>
                  <div className="flex flex-wrap gap-[7px]">
                    {currentOrgAssignments.length ? currentOrgAssignments.map((assignment) => {
                      const member = memberByProfile.get(assignment.employee_profile_id);
                      const label = member?.employee_name || member?.display_name || '成员信息未加载';
                      return (
                        <span key={assignment.id} className="flex items-center gap-[7px] rounded-full border border-[#e1e6f0] bg-[#fafbfe] px-[10px] py-[5px] text-[10px] text-[#596174]">
                          {label}
                          <button
                            type="button"
                            className="text-[#9a6370] hover:text-[#bd2948]"
                            onClick={() => setStopTarget({
                              kind: 'org-assignment',
                              id: assignment.id,
                              name: `${label}的组织归属`,
                            })}
                          >
                            结束
                          </button>
                        </span>
                      );
                    }) : <span className="text-[11px] text-[#8b93a5]">暂无当前成员。</span>}
                  </div>
                  {memberTotal > ORGANIZATION_MEMBER_PAGE_SIZE ? (
                    <Paginator
                      aria-label="组织成员分页"
                      className="mt-[12px]"
                      page={memberPage}
                      pageCount={Math.max(1, Math.ceil(memberTotal / ORGANIZATION_MEMBER_PAGE_SIZE))}
                      onChange={setMemberPage}
                    />
                  ) : null}
                </section>

                {selectedPosition ? (
                  <section className="mt-[18px] grid grid-cols-2 gap-[14px] max-[760px]:grid-cols-1">
                    <DetailPanel
                      title="当前与历史任职"
                      icon={<BriefcaseBusiness className="size-[15px]" />}
                      action={(
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => setStopTarget({
                            kind: 'position',
                            id: selectedPosition.id,
                            name: selectedPosition.name,
                          })}
                        >
                          停用岗位
                        </Button>
                      )}
                    >
                      {selectedPositionAssignments.length ? (
                        selectedPositionAssignments.map((assignment) => {
                          const member = memberByProfile.get(assignment.employee_profile_id);
                          return (
                            <div key={assignment.id} className="flex items-center gap-[10px] border-b border-[#eef1f5] py-[9px] last:border-0">
                              <span className={cn(
                                'size-[7px] rounded-full',
                                assignment.status === 'active' ? 'bg-[#2eaf72]' : 'bg-[#c7ccd6]',
                              )} />
                              <div className="min-w-0 flex-1">
                                <p className="truncate text-[12px] font-medium text-[#3d4558]">
                                  {member?.employee_name || member?.display_name || '成员信息未加载'}
                                </p>
                                <p className="mt-[2px] text-[9px] text-[#949bad]">
                                  {formatDateTime(assignment.effective_from)}
                                  {assignment.effective_until ? ` — ${formatDateTime(assignment.effective_until)}` : ' — 至今'}
                                </p>
                              </div>
                              {assignment.status === 'active' ? (
                                <button
                                  type="button"
                                  className="text-[10px] text-[#9a6370] hover:text-[#bd2948]"
                                  onClick={() => setStopTarget({
                                    kind: 'position-assignment',
                                    id: assignment.id,
                                    name: `${member?.employee_name || '成员'}的岗位任职`,
                                  })}
                                >
                                  结束
                                </button>
                              ) : null}
                            </div>
                          );
                        })
                      ) : (
                        <p className="py-[14px] text-[11px] text-[#8b93a5]">暂无任职记录。</p>
                      )}
                      <PositionMemberPicker
                        key={`position-member-${selectedUnit.id}-${currentOrgAssignments.length}`}
                        tenantId={tenant.id}
                        orgUnitId={selectedUnit.id}
                        disabled={saving}
                        onAssign={(profileId) => void assignPosition(profileId)}
                      />
                    </DetailPanel>

                    <DetailPanel
                      title="岗位默认角色"
                      icon={<ShieldCheck className="size-[15px]" />}
                      action={(
                        <Button size="sm" variant="ghost" onClick={() => openDialog('role')}>
                          <Plus className="size-[13px]" />
                          绑定
                        </Button>
                      )}
                    >
                      {selectedBindings.length ? selectedBindings.map((binding) => (
                        <div key={binding.id} className="flex items-center justify-between gap-[10px] border-b border-[#eef1f5] py-[9px] last:border-0">
                          <div>
                            <p className="text-[12px] font-medium text-[#3d4558]">{binding.business_role_name}</p>
                            <p className="mt-[2px] font-mono text-[9px] text-[#949bad]">{binding.business_role_code}</p>
                          </div>
                          <span className="rounded-full bg-[#fff4da] px-[8px] py-[3px] text-[9px] text-[#8c6209]">
                            岗位带入
                          </span>
                          <button
                            type="button"
                            className="text-[10px] text-[#9a6370] hover:text-[#bd2948]"
                            onClick={() => setStopTarget({
                              kind: 'binding',
                              id: binding.id,
                              name: binding.business_role_name,
                              impactSummary: formatRoleImpactSummary(
                                roleImpactsByCode.get(binding.business_role_code) || [],
                              ),
                            })}
                          >
                            解除
                          </button>
                        </div>
                      )) : (
                        <p className="py-[14px] text-[11px] text-[#8b93a5]">未配置默认角色，不会由此岗位进入流程候选。</p>
                      )}
                    </DetailPanel>
                  </section>
                ) : null}
                {selectedPosition ? (
                  <ResponsibilityImpactRail
                    position={selectedPosition}
                    bindings={selectedBindings}
                    impacts={selectedPositionImpacts}
                  />
                ) : null}
              </>
            ) : (
              <EmptyState text="组织树尚未初始化。" />
            )}
          </main>
        </div>
      </div>

      <EditorDialog
        mode={dialogMode}
        unitName={selectedUnit?.name || ''}
        positionName={selectedPosition?.name || ''}
        saving={saving}
        unitDraft={unitDraft}
        positionDraft={positionDraft}
        unitTypes={unitTypes}
        positionTypes={positionTypes}
        roles={roles.filter((role) => role.status === 'active')}
        units={units.filter(
          (unit) => unit.status === 'active'
            && unit.id !== selectedUnitId
            && !unit.tree_path.startsWith(`${selectedUnit?.tree_path || ''}/`),
        )}
        tenantId={tenant.id}
        excludeUserId={currentUser?.id}
        selectedProfileId={selectedProfileId}
        selectedRoleId={selectedRoleId}
        roleEffectiveUntil={positionRoleEffectiveUntil}
        selectedParentId={selectedParentId}
        roleImpacts={roleImpactsByCode.get(
          roles.find((role) => role.id === selectedRoleId)?.role_code || '',
        ) || []}
        onUnitDraft={setUnitDraft}
        onPositionDraft={setPositionDraft}
        onProfile={setSelectedProfileId}
        onRole={setSelectedRoleId}
        onRoleEffectiveUntil={setPositionRoleEffectiveUntil}
        onParent={setSelectedParentId}
        onClose={() => setDialogMode(null)}
        onSave={() => void saveDialog()}
      />
      <ConfirmDialog
        open={stopTarget !== null}
        onOpenChange={(open) => { if (!open) setStopTarget(null); }}
        title={`确认结束“${stopTarget?.name || ''}”？`}
        description={stopTarget?.impactSummary
          ? `${stopTarget.impactSummary}。解除后系统会保留历史，覆盖状态将按真实剩余来源重新计算。`
          : '系统会保留历史记录；仍有活动引用时，服务端会拒绝本次操作。'}
        confirmText="确认结束"
        loading={saving}
        onConfirm={() => void confirmStop()}
      />
    </div>
  );
}

function ResponsibilityImpactRail({
  position,
  bindings,
  impacts,
}: {
  position: Position;
  bindings: PositionRoleBinding[];
  impacts: PositionRoleSopImpact[];
}) {
  const uniqueImpacts = [...new Map(
    impacts.map((impact) => [`${impact.skillId}:${impact.participant.node_id}`, impact]),
  ).values()];
  return (
    <section
      aria-label="岗位流程责任影响"
      className="mt-[14px] rounded-[18px] border border-[#dce4f4] bg-[linear-gradient(110deg,#f7f9ff_0%,#ffffff_52%,#f6fbf9_100%)] p-[16px]"
    >
      <div className="flex flex-wrap items-center gap-[8px]">
        <Workflow className="size-[17px] text-[#3157e8]" />
        <h3 className="text-[13px] font-semibold text-[#30384c]">责任闭环轨道</h3>
        <span className="text-[11px] text-[#69738a]">
          这里展示真实绑定会进入哪些 SOP 人工节点，不根据岗位名称推断职责。
        </span>
      </div>
      <div className="mt-[13px] grid items-stretch gap-[10px] lg:grid-cols-[minmax(180px,0.8fr)_28px_minmax(220px,1fr)_28px_minmax(320px,1.7fr)]">
        <ImpactNode eyebrow="组织岗位" title={position.name} detail={position.code} tone="blue" />
        <ImpactArrow label="带入" />
        <div className="rounded-[14px] border border-[#eadfca] bg-[#fffaf0] p-[13px]">
          <p className="text-[11px] font-medium text-[#9a6a18]">默认业务角色</p>
          <div className="mt-[8px] flex flex-wrap gap-[6px]">
            {bindings.length ? bindings.map((binding) => (
              <span key={binding.id} className="rounded-full border border-[#ead9b8] bg-white px-[9px] py-[4px] text-[11px] font-medium text-[#6e5528]">
                {binding.business_role_name}
              </span>
            )) : <span className="text-[12px] text-[#8d7a58]">尚未绑定</span>}
          </div>
        </div>
        <ImpactArrow label="参与" />
        <div className="rounded-[14px] border border-[#d9e9e3] bg-[#f5fbf8] p-[13px]">
          <p className="text-[11px] font-medium text-[#2c8065]">SOP 人工责任</p>
          {uniqueImpacts.length ? (
            <div className="mt-[7px] grid gap-[6px]">
              {uniqueImpacts.map((impact) => (
                <div key={`${impact.skillId}:${impact.participant.node_id}`} className="flex flex-wrap items-center gap-x-[8px] gap-y-[3px] text-[12px] text-[#344a43]">
                  <span className="font-semibold">{impact.skillName}</span>
                  <span className="font-mono text-[11px] text-[#6d817a]">{impact.participant.node_id}</span>
                  {impact.participant.context_count ? (
                    <span className={cn(
                      'rounded-full px-[7px] py-[2px] text-[11px]',
                      impact.participant.covered_context_count === impact.participant.context_count
                        ? 'bg-[#dcf5e9] text-[#237657]'
                        : 'bg-[#fff0e1] text-[#a15a16]',
                    )}>
                      覆盖 {impact.participant.covered_context_count}/{impact.participant.context_count} 个组织
                    </span>
                  ) : null}
                </div>
              ))}
            </div>
          ) : (
            <div className="mt-[8px] flex items-center gap-[7px] text-[12px] text-[#71817b]">
              <CircleAlert className="size-[15px]" />
              当前角色尚未被已发布 SOP 的人工节点引用。
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function ImpactNode({
  eyebrow,
  title,
  detail,
  tone,
}: {
  eyebrow: string;
  title: string;
  detail: string;
  tone: 'blue';
}) {
  return (
    <div className={cn(
      'rounded-[14px] border p-[13px]',
      tone === 'blue' && 'border-[#d8e0f5] bg-[#f5f7ff]',
    )}>
      <p className="text-[11px] font-medium text-[#5570c4]">{eyebrow}</p>
      <p className="mt-[5px] text-[13px] font-semibold text-[#30384c]">{title}</p>
      <p className="mt-[3px] font-mono text-[11px] text-[#78839a]">{detail}</p>
    </div>
  );
}

function ImpactArrow({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center text-[11px] font-medium text-[#8190b2] lg:flex-col">
      <span>{label}</span>
      <span aria-hidden="true">→</span>
    </div>
  );
}

function formatRoleImpactSummary(impacts: PositionRoleSopImpact[]) {
  if (!impacts.length) return '当前没有已发布 SOP 节点引用该角色';
  const nodeCount = new Set(
    impacts.map((impact) => `${impact.skillId}:${impact.participant.node_id}`),
  ).size;
  const skillCount = new Set(impacts.map((impact) => impact.skillId)).size;
  return `该角色关联 ${skillCount} 个 SOP、${nodeCount} 个人工节点`;
}

function DetailPanel({
  title,
  icon,
  action,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  action: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-[16px] border border-[#e5e9f1] bg-[#fcfdff] p-[14px]">
      <div className="flex min-h-[30px] items-center gap-[7px] border-b border-[#edf0f5] pb-[9px] text-[#59647d]">
        {icon}
        <h3 className="text-[12px] font-semibold">{title}</h3>
        <div className="ml-auto">{action}</div>
      </div>
      {children}
    </div>
  );
}

function EmptyState({ text }: { text: string }) {
  return (
    <div className="rounded-[14px] border border-dashed border-[#d8deea] bg-[#fafbfe] px-[18px] py-[28px] text-center text-[11px] text-[#8b93a5]">
      {text}
    </div>
  );
}

function PositionMemberPicker({
  tenantId,
  orgUnitId,
  disabled,
  onAssign,
}: {
  tenantId: string;
  orgUnitId: string;
  disabled: boolean;
  onAssign: (profileId: string) => void;
}) {
  const [profileId, setProfileId] = useState('');
  return (
    <div className="mt-[10px] flex gap-[8px] border-t border-[#edf0f5] pt-[12px]">
      <div className="flex-1">
        <RemoteMemberSelect
          tenantId={tenantId}
          orgUnitId={orgUnitId}
          value={profileId}
          onValueChange={setProfileId}
          ariaLabel="选择岗位任职成员"
          placeholder="选择本组织成员"
        />
      </div>
      <Button
        size="sm"
        disabled={disabled || !profileId}
        onClick={() => {
          onAssign(profileId);
          setProfileId('');
        }}
      >
        任职
      </Button>
    </div>
  );
}

function LeaderPicker({
  tenantId,
  orgUnitId,
  leaderTypes,
  positionAssignments,
  positions,
  hasPrimary,
  disabled,
  onAssign,
}: {
  tenantId: string;
  orgUnitId: string;
  leaderTypes: OrganizationLeaderType[];
  positionAssignments: PositionAssignment[];
  positions: Position[];
  hasPrimary: boolean;
  disabled: boolean;
  onAssign: (
    profileId: string,
    leaderTypeCode: string,
    positionAssignmentId: string,
    effectiveUntil: string,
  ) => void;
}) {
  const [profileId, setProfileId] = useState('');
  const [leaderTypeCode, setLeaderTypeCode] = useState('primary');
  const [positionAssignmentId, setPositionAssignmentId] = useState('');
  const [effectiveUntil, setEffectiveUntil] = useState('');
  const availableLeaderTypes = useMemo(
    () => leaderTypes.map((item) => (
      item.code === 'primary' && hasPrimary ? { ...item, status: 'inactive' as const } : item
    )),
    [hasPrimary, leaderTypes],
  );
  useEffect(() => {
    if (hasPrimary && leaderTypeCode === 'primary') {
      setLeaderTypeCode(availableLeaderTypes.find((item) => item.status === 'active')?.code || '');
    }
  }, [availableLeaderTypes, hasPrimary, leaderTypeCode]);
  const memberPositions = positionAssignments.filter(
    (assignment) => assignment.employee_profile_id === profileId
      && assignment.status === 'active'
      && positions.some((position) => position.id === assignment.position_id),
  );
  const positionName = new Map(positions.map((position) => [position.id, position.name]));
  return (
    <div className="mt-[10px] grid gap-[8px] border-t border-[#edf0f5] pt-[12px] md:grid-cols-2 xl:grid-cols-[1fr_1fr_1fr_1fr_auto]">
      <Field label="负责人">
        <RemoteMemberSelect
          tenantId={tenantId}
          orgUnitId={orgUnitId}
          value={profileId}
          onValueChange={(value) => {
            setProfileId(value);
            setPositionAssignmentId('');
          }}
          ariaLabel="负责人"
          placeholder="搜索本组织负责人"
        />
      </Field>
      <CodeSelect
        label="负责人类型"
        value={leaderTypeCode}
        options={availableLeaderTypes}
        onChange={setLeaderTypeCode}
      />
      <CodeSelect
        label="关联任职（可选）"
        value={positionAssignmentId}
        options={memberPositions.map((assignment) => ({
          code: assignment.id,
          name: positionName.get(assignment.position_id) || assignment.position_id,
          status: assignment.status,
        }))}
        onChange={setPositionAssignmentId}
      />
      <Field label={leaderTypeCode === 'acting' ? '代理结束时间（必填）' : '结束时间（可选）'}>
        <Input
          type="datetime-local"
          value={effectiveUntil}
          onChange={(event) => setEffectiveUntil(event.target.value)}
        />
      </Field>
      <Button
        className="self-end"
        disabled={disabled || !profileId || !leaderTypeCode || (leaderTypeCode === 'acting' && !effectiveUntil)}
        onClick={() => {
          onAssign(profileId, leaderTypeCode, positionAssignmentId, effectiveUntil);
          setProfileId('');
          setPositionAssignmentId('');
          setEffectiveUntil('');
        }}
      >
        设为负责人
      </Button>
    </div>
  );
}

function EditorDialog({
  mode,
  unitName,
  positionName,
  saving,
  unitDraft,
  positionDraft,
  unitTypes,
  positionTypes,
  roles,
  units,
  tenantId,
  excludeUserId,
  selectedProfileId,
  selectedRoleId,
  roleEffectiveUntil,
  selectedParentId,
  roleImpacts,
  onUnitDraft,
  onPositionDraft,
  onProfile,
  onRole,
  onRoleEffectiveUntil,
  onParent,
  onClose,
  onSave,
}: {
  mode: DialogMode;
  unitName: string;
  positionName: string;
  saving: boolean;
  unitDraft: typeof EMPTY_UNIT;
  positionDraft: typeof EMPTY_POSITION;
  unitTypes: CodeOption[];
  positionTypes: CodeOption[];
  roles: BusinessRoleOption[];
  units: OrganizationUnit[];
  tenantId: string;
  excludeUserId?: string;
  selectedProfileId: string;
  selectedRoleId: string;
  roleEffectiveUntil: string;
  selectedParentId: string;
  roleImpacts: PositionRoleSopImpact[];
  onUnitDraft: (draft: typeof EMPTY_UNIT) => void;
  onPositionDraft: (draft: typeof EMPTY_POSITION) => void;
  onProfile: (value: string) => void;
  onRole: (value: string) => void;
  onRoleEffectiveUntil: (value: string) => void;
  onParent: (value: string) => void;
  onClose: () => void;
  onSave: () => void;
}) {
  const [moveSearch, setMoveSearch] = useState('');
  const [moveOptions, setMoveOptions] = useState<OrganizationUnit[]>(units);

  useEffect(() => {
    if (mode !== 'move') return;
    const keyword = moveSearch.trim();
    if (!keyword) {
      setMoveOptions(units);
      return;
    }
    const timeout = window.setTimeout(async () => {
      try {
        setMoveOptions(await api.get<OrganizationSearchResult[]>(
          `/api/organization/unit-search?tenant_id=${encodeURIComponent(tenantId)}`
          + `&keyword=${encodeURIComponent(keyword)}&limit=50`,
        ));
      } catch (error) {
        notify.error(error instanceof Error ? error.message : '移动目标搜索失败');
      }
    }, 250);
    return () => window.clearTimeout(timeout);
  }, [mode, moveSearch, tenantId, units]);

  const title = mode === 'unit'
    ? `在“${unitName}”下创建组织`
    : mode === 'move'
      ? `移动“${unitName}”`
      : mode === 'position'
      ? `为“${unitName}”创建岗位`
      : mode === 'member'
        ? `调入“${unitName}”`
        : `为“${positionName}”绑定默认角色`;
  const canSave = mode === 'unit'
    ? Boolean(unitDraft.code.trim() && unitDraft.name.trim())
    : mode === 'move'
      ? Boolean(selectedParentId)
      : mode === 'position'
      ? Boolean(positionDraft.code.trim() && positionDraft.name.trim())
      : mode === 'member'
        ? Boolean(selectedProfileId)
        : Boolean(selectedRoleId);
  return (
    <Dialog open={mode !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-[480px]">
        <DialogTitle>{title}</DialogTitle>
        {mode === 'unit' ? (
          <div className="grid gap-[13px]">
            <Field label="稳定组织编码">
              <Input value={unitDraft.code} onChange={(event) => onUnitDraft({ ...unitDraft, code: event.target.value })} />
            </Field>
            <Field label="组织名称">
              <Input value={unitDraft.name} onChange={(event) => onUnitDraft({ ...unitDraft, name: event.target.value })} />
            </Field>
            <CodeSelect label="组织类型" value={unitDraft.type} options={unitTypes} onChange={(type) => onUnitDraft({ ...unitDraft, type })} />
          </div>
        ) : null}
        {mode === 'move' ? (
          <div className="grid gap-[10px]">
            <Field label="搜索新的上级组织">
              <Input
                aria-label="搜索新的上级组织"
                value={moveSearch}
                placeholder="输入名称或编码定位，不下载整棵组织树"
                onChange={(event) => setMoveSearch(event.target.value)}
              />
            </Field>
            <CodeSelect
              label="新的上级组织"
              value={selectedParentId}
              options={moveOptions.map((unit) => ({
                code: unit.id,
                name: unit.name,
                status: unit.status,
              }))}
              onChange={onParent}
            />
          </div>
        ) : null}
        {mode === 'position' ? (
          <div className="grid gap-[13px]">
            <Field label="稳定岗位编码">
              <Input value={positionDraft.code} onChange={(event) => onPositionDraft({ ...positionDraft, code: event.target.value })} />
            </Field>
            <Field label="岗位名称">
              <Input value={positionDraft.name} onChange={(event) => onPositionDraft({ ...positionDraft, name: event.target.value })} />
            </Field>
            <CodeSelect label="岗位类型" value={positionDraft.type} options={positionTypes} onChange={(type) => onPositionDraft({ ...positionDraft, type })} />
            <Field label="岗位职责">
              <Textarea value={positionDraft.responsibility} onChange={(event) => onPositionDraft({ ...positionDraft, responsibility: event.target.value })} />
            </Field>
          </div>
        ) : null}
        {mode === 'member' ? (
          <Field label="成员">
            <RemoteMemberSelect
            tenantId={tenantId}
            value={selectedProfileId}
            onValueChange={onProfile}
            ariaLabel="成员"
            excludeUserId={excludeUserId}
            placeholder="搜索并选择要调入的成员"
          />
          </Field>
        ) : null}
        {mode === 'role' ? (
          <div className="grid gap-[12px]">
            <CodeSelect
              label="默认业务角色"
              value={selectedRoleId}
              options={roles.map((role) => ({
                code: role.id,
                name: `${role.name} · ${role.role_code}`,
                status: role.status,
              }))}
              onChange={onRole}
            />
            <Field label="授权截止时间（可选）">
              <Input
                aria-label="岗位角色授权截止时间"
                min={toLocalDateTimeInput(new Date())}
                type="datetime-local"
                value={roleEffectiveUntil}
                onChange={(event) => onRoleEffectiveUntil(event.target.value)}
              />
            </Field>
            <div className="rounded-[14px] border border-[#dce4f4] bg-[#f7f9ff] p-[12px]">
              <p className="text-[12px] font-semibold text-[#3c4a70]">绑定影响预览</p>
              <p className="mt-[5px] text-[12px] leading-[18px] text-[#66718a]">
                {selectedRoleId
                  ? `${formatRoleImpactSummary(roleImpacts)}。保存后，当前岗位的有效任职人会在该岗位组织范围内获得此角色。`
                  : '选择角色后，系统会列出它关联的已发布 SOP 人工节点。'}
              </p>
              {roleImpacts.length ? (
                <div className="mt-[8px] flex flex-wrap gap-[6px]">
                  {[...new Map(roleImpacts.map((impact) => [
                    `${impact.skillId}:${impact.participant.node_id}`,
                    impact,
                  ])).values()].map((impact) => (
                    <span key={`${impact.skillId}:${impact.participant.node_id}`} className="rounded-full border border-[#d5def2] bg-white px-[8px] py-[3px] text-[11px] text-[#506083]">
                      {impact.skillName} · {impact.participant.node_id}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          </div>
        ) : null}
        <div className="flex justify-end gap-[8px] pt-[6px]">
          <Button variant="outline" onClick={onClose}>取消</Button>
          <Button disabled={saving || !canSave} onClick={onSave}>
            {saving ? '保存中…' : '保存'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="grid gap-[6px] text-[11px] font-medium text-[#616a7d]">
      {label}
      {children}
    </label>
  );
}

function toLocalDateTimeInput(value: Date): string {
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function CodeSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: CodeOption[];
  onChange: (value: string) => void;
}) {
  return (
    <Field label={label}>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger><SelectValue placeholder={`选择${label}`} /></SelectTrigger>
        <SelectContent>
          {options.filter((option) => option.status === 'active').map((option) => (
            <SelectItem key={option.code} value={option.code}>{option.name}</SelectItem>
          ))}
        </SelectContent>
      </Select>
    </Field>
  );
}
