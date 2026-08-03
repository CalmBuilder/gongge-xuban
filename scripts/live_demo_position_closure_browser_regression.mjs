/**
 * @Time       : 2026/08/02 14:20
 * @Author     : zhanglp8181
 * @File       : live_demo_position_closure_browser_regression.mjs
 * @CallChain  : ./app.sh:5137 + MySQL → Chromium 管理员 → 岗位/任职/角色治理 API → 企业拓扑
 * @Description: 幂等补齐演示租户 IT、法务与行政 SOP 所需岗位事实，只使用已有演示账号并验证页面闭环。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const tenantId = process.env.BROWSER_TEST_TENANT_ID || 'tenant_demo';
const username = process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin';
const password = process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin';
const screenshotPath = process.env.BROWSER_TEST_SCREENSHOT
  || '/tmp/gongge-demo-position-closure-regression.png';
const browserErrors = [];
const badResponses = [];
let createdPositionCount = 0;
let updatedPositionCount = 0;
let createdOrganizationAssignmentCount = 0;
let createdAssignmentCount = 0;
let endedObsoleteOrganizationAssignmentCount = 0;

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
const page = await context.newPage();

page.on('pageerror', (error) => browserErrors.push(`pageerror: ${error.message}`));
page.on('console', (message) => {
  if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`);
});
page.on('response', (response) => {
  const url = new URL(response.url());
  if (url.pathname.startsWith('/api/') && response.status() >= 500) {
    badResponses.push(`${response.status()} ${response.request().method()} ${url.pathname}`);
  }
});

const positionPlans = [
  {
    code: 'POS-OFFICE-MEMBER',
    roleCode: 'admin_seal_operator',
    username: 'user_demo',
  },
  {
    code: 'POS-DEMO-IT-OPERATOR',
    name: 'IT运维与权限工程师',
    organizationName: '中部区域支撑中心',
    responsibility: '演示租户岗位：承担应用运维、故障处理和普通 IT 权限开通。',
    roleCode: 'it_access_operator',
    username: 'it_engineer_demo',
  },
  {
    code: 'POS-DEMO-IT-APPROVER',
    name: 'IT安全审批负责人',
    organizationName: '党委办公室（办公室、安全监管部）',
    responsibility: '演示租户岗位：复核高权限申请和安全风险，与执行岗分离。',
    roleCode: 'it_access_approver',
    username: 'approver_demo',
  },
  {
    code: 'POS-DEMO-LEGAL-ANALYST',
    name: '法务合规专员',
    organizationName: '中国联合网络通信有限公司软件研究院',
    responsibility: '演示租户岗位：承担合同风险分析、合规检查和处理建议。',
    roleCode: 'legal_contract_risk_analyst',
    username: 'user_demo',
  },
  {
    code: 'POS-DEMO-LEGAL-REVIEWER',
    name: '法务合规复核负责人',
    organizationName: '联通软件研究院本部',
    responsibility: '演示租户岗位：独立复核合同风险结论，与分析岗分离。',
    roleCode: 'legal_contract_reviewer',
    username: 'approver_demo',
  },
];

/** 登录统一应用并保留真实浏览器认证上下文。 */
async function login() {
  await page.goto(`${baseUrl}/enterprise/dashboard`);
  await page.evaluate(() => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill(username);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.waitForFunction(() => Boolean(localStorage.getItem('gongge_auth')));
}

/** 使用当前 Chromium 登录令牌调用组织治理 API。 */
async function authenticatedFetch(path, options = {}) {
  return page.evaluate(async ({ requestPath, requestOptions }) => {
    const raw = localStorage.getItem('gongge_auth');
    if (!raw) throw new Error('浏览器认证会话不存在');
    const response = await fetch(requestPath, {
      ...requestOptions,
      headers: {
        Authorization: `Bearer ${JSON.parse(raw).token}`,
        'Content-Type': 'application/json',
        ...(requestOptions.headers || {}),
      },
    });
    return { status: response.status, body: await response.json() };
  }, { requestPath: path, requestOptions: options });
}

try {
  await login();
  const [unitsResponse, positionsResponse, organizationAssignmentsResponse, assignmentsResponse, rolesResponse, usersResponse] = await Promise.all([
    authenticatedFetch(`/api/organization/units?tenant_id=${tenantId}`),
    authenticatedFetch(`/api/organization/positions?tenant_id=${tenantId}`),
    authenticatedFetch(`/api/organization/member-org-assignments?tenant_id=${tenantId}`),
    authenticatedFetch(`/api/organization/position-assignments?tenant_id=${tenantId}`),
    authenticatedFetch(`/api/organization/business-roles?tenant_id=${tenantId}`),
    authenticatedFetch(`/api/auth/users?tenant_id=${tenantId}`),
  ]);
  for (const response of [unitsResponse, positionsResponse, organizationAssignmentsResponse, assignmentsResponse, rolesResponse, usersResponse]) {
    assert.equal(response.status, 200);
  }
  const unitsByName = new Map(unitsResponse.body.map((item) => [item.name, item]));
  const positionsByCode = new Map(positionsResponse.body.map((item) => [item.code, item]));
  const rolesByCode = new Map(rolesResponse.body.map((item) => [item.role_code, item]));
  const usersByName = new Map(usersResponse.body.map((item) => [item.username, item]));
  const organizationAssignments = [...organizationAssignmentsResponse.body];
  const assignments = [...assignmentsResponse.body];

  for (const plan of positionPlans) {
    let position = positionsByCode.get(plan.code);
    if (!position) {
      const organization = unitsByName.get(plan.organizationName);
      assert.ok(organization, `缺少目标组织：${plan.organizationName}`);
      const created = await authenticatedFetch('/api/organization/positions', {
        method: 'POST',
        body: JSON.stringify({
          tenant_id: tenantId,
          org_unit_id: organization.id,
          code: plan.code,
          name: plan.name,
          position_type_code: plan.code.includes('APPROVER') || plan.code.includes('REVIEWER')
            ? 'management'
            : 'professional',
          responsibility: plan.responsibility,
        }),
      });
      assert.equal(
        created.status,
        200,
        `创建岗位 ${plan.code} 应成功：${JSON.stringify(created.body)}`,
      );
      position = created.body;
      positionsByCode.set(plan.code, position);
      createdPositionCount += 1;
    }
    const intendedOrganization = plan.organizationName
      ? unitsByName.get(plan.organizationName)
      : null;
    if (plan.organizationName) {
      assert.ok(intendedOrganization, `缺少目标组织：${plan.organizationName}`);
    }
    if (intendedOrganization && position.org_unit_id !== intendedOrganization.id) {
      for (const assignment of assignments.filter((item) => (
        item.status === 'active' && item.position_id === position.id
      ))) {
        const ended = await authenticatedFetch(
          `/api/organization/position-assignments/${assignment.id}/end`,
          {
            method: 'POST',
            body: JSON.stringify({ tenant_id: tenantId }),
          },
        );
        assert.equal(
          ended.status,
          200,
          `调整 ${plan.code} 组织前应结束原任职：${JSON.stringify(ended.body)}`,
        );
        Object.assign(assignment, ended.body);
      }
      const updated = await authenticatedFetch(`/api/organization/positions/${position.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          tenant_id: tenantId,
          org_unit_id: intendedOrganization.id,
        }),
      });
      assert.equal(
        updated.status,
        200,
        `校正岗位 ${plan.code} 责任组织应成功：${JSON.stringify(updated.body)}`,
      );
      position = updated.body;
      positionsByCode.set(plan.code, position);
      updatedPositionCount += 1;
    }

    const role = rolesByCode.get(plan.roleCode);
    assert.ok(role, `缺少业务角色：${plan.roleCode}`);
    const binding = await authenticatedFetch('/api/organization/position-role-bindings', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: tenantId,
        position_id: position.id,
        business_role_id: role.id,
      }),
    });
    assert.equal(binding.status, 200, `${plan.code} 应幂等绑定 ${plan.roleCode}`);
    assert.equal(binding.body.status, 'active');
    assert.ok(binding.body.granted_by_user_id);

    const user = usersByName.get(plan.username);
    assert.ok(user?.employee_profile_id, `演示账号 ${plan.username} 必须有员工档案`);
    const existingOrganizationAssignment = organizationAssignments.find((item) => (
      item.status === 'active'
      && item.org_unit_id === position.org_unit_id
      && item.employee_profile_id === user.employee_profile_id
    ));
    if (!existingOrganizationAssignment) {
      const organizationAssigned = await authenticatedFetch('/api/organization/member-org-assignments', {
        method: 'POST',
        body: JSON.stringify({
          tenant_id: tenantId,
          employee_profile_id: user.employee_profile_id,
          org_unit_id: position.org_unit_id,
          assignment_type: 'concurrent',
        }),
      });
      assert.equal(
        organizationAssigned.status,
        200,
        `${plan.username} 兼任组织应成功：${JSON.stringify(organizationAssigned.body)}`,
      );
      organizationAssignments.push(organizationAssigned.body);
      createdOrganizationAssignmentCount += 1;
    }
    const existingAssignment = assignments.find((item) => (
      item.status === 'active'
      && item.position_id === position.id
      && item.employee_profile_id === user.employee_profile_id
    ));
    if (!existingAssignment) {
      const assigned = await authenticatedFetch('/api/organization/position-assignments', {
        method: 'POST',
        body: JSON.stringify({
          tenant_id: tenantId,
          employee_profile_id: user.employee_profile_id,
          position_id: position.id,
          assignment_type: 'concurrent',
        }),
      });
      assert.equal(
        assigned.status,
        200,
        `${plan.username} 任职 ${plan.code} 应成功：${JSON.stringify(assigned.body)}`,
      );
      assignments.push(assigned.body);
      createdAssignmentCount += 1;
    }
  }

  for (const [obsoleteUsername, obsoleteOrganizationName] of [
    ['it_engineer_demo', '联通软件研究院本部'],
    ['user_demo', '联通软件研究院本部'],
  ]) {
    const obsoleteUser = usersByName.get(obsoleteUsername);
    const obsoleteOrganization = unitsByName.get(obsoleteOrganizationName);
    const obsoleteAssignment = organizationAssignments.find((item) => (
      item.status === 'active'
      && item.assignment_type === 'concurrent'
      && item.employee_profile_id === obsoleteUser?.employee_profile_id
      && item.org_unit_id === obsoleteOrganization?.id
    ));
    if (obsoleteAssignment) {
      const ended = await authenticatedFetch(
        `/api/organization/member-org-assignments/${obsoleteAssignment.id}/end`,
        { method: 'POST', body: JSON.stringify({ tenant_id: tenantId }) },
      );
      assert.equal(
        ended.status,
        200,
        `结束 ${obsoleteUsername} 错误的过渡组织兼任应成功`,
      );
      Object.assign(obsoleteAssignment, ended.body);
      endedObsoleteOrganizationAssignmentCount += 1;
    }
  }

  await page.goto(`${baseUrl}/enterprise/enterprise-info`);
  const topology = page.getByRole('region', {
    name: '真人、组织与岗位、组织角色、数字员工和专家拓扑图',
  });
  await topology.waitFor();
  const scenarios = ['费用报销', '用章申请', '请假与假勤', 'IT 权限开通', '合同风险初筛'];
  for (const scenario of scenarios) {
    await topology.getByRole('tab', { name: scenario, exact: true }).click();
    const live = topology.locator('[data-topology-contract="live"]');
    for (const relation of [
      'OrganizationUnit.contains.Position',
      'PositionAssignment.assigns.Human',
      'PositionRoleBinding.grants.BusinessRole',
      'EmployeeRoleAssignment.delegates.BusinessRole',
      'AgentRoleBinding.grants.BusinessRole',
      'AgentResourceBinding.loads.SOP',
      'Human.supervises.Agent',
    ]) {
      assert.equal(
        await live.locator(`[data-topology-relation="${relation}"]`).getAttribute('data-relation-status'),
        'verified',
        `${scenario} 的 ${relation} 应由真实数据验证`,
      );
    }
    assert.equal(
      await live.locator('[data-topology-relation="Human.owns.ExpertClone"]').getAttribute('data-relation-status'),
      'missing',
      `${scenario} 不应伪造能力分身`,
    );
  }

  await page.screenshot({ path: screenshotPath, fullPage: true });
  assert.deepEqual(badResponses, []);
  assert.deepEqual(browserErrors, []);
  console.log(JSON.stringify({
    createdPositionCount,
    updatedPositionCount,
    createdOrganizationAssignmentCount,
    createdAssignmentCount,
    endedObsoleteOrganizationAssignmentCount,
    governedPositionCount: positionPlans.length,
    verifiedScenarioCount: scenarios.length,
    screenshotPath,
    badResponses,
    browserErrors,
  }, null, 2));
} finally {
  await browser.close();
}
