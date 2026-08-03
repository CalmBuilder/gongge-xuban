/**
 * @Time       : 2026/08/02 12:45
 * @Author     : zhanglp8181
 * @File       : live_position_role_closure_browser_regression.mjs
 * @CallChain  : ./app.sh:5137 + MySQL → Chromium 管理员 → 岗位角色配置/覆盖轨道
 * @Description: 通过真实管理界面和认证 API 幂等补齐可靠岗位角色，并验证影响预览、审计字段和覆盖变化。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const tenantId = process.env.BROWSER_TEST_TENANT_ID || 'tenant_demo';
const username = process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin';
const password = process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin';
const screenshotPath = process.env.BROWSER_TEST_SCREENSHOT
  || '/tmp/gongge-position-role-closure-regression.png';
const browserErrors = [];
const badResponses = [];
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

const intendedBindings = [
  ['POS-FIN-MANAGER', 'expense_department_approver'],
  ['POS-HR-MANAGER', 'expense_department_approver'],
  ['POS-OFFICE-MANAGER', 'expense_department_approver'],
  ['POS-GOV-PM', 'expense_department_approver'],
  ['POS-FIN-MANAGER', 'expense_finance_approver'],
  ['POS-FIN-MEMBER', 'finance_expense_specialist'],
  ['POS-HR-MEMBER', 'hr_leave_specialist'],
  ['POS-HR-MANAGER', 'hr_certificate_reviewer'],
  ['POS-OFFICE-MANAGER', 'admin_seal_approver'],
];

const intendedBackupAssignments = [
  ['POS-FIN-MEMBER', '财务部'],
  ['POS-HR-MEMBER', '人力资源部'],
  ['POS-OFFICE-MEMBER', '综合办公室'],
  ['POS-GOV-ARCH', '政企研发项目组'],
];
const backupGrantReason = '超标报销部门审批备用代理；负责人本人发起时由另一名真人独立复核';
const backupEffectiveUntil = '2026-10-31T23:59:59+08:00';

try {
  await login();
  const [positionsResponse, positionAssignmentsResponse, rolesResponse, beforeCoverage] = await Promise.all([
    authenticatedFetch(`/api/organization/positions?tenant_id=${tenantId}`),
    authenticatedFetch(`/api/organization/position-assignments?tenant_id=${tenantId}`),
    authenticatedFetch(`/api/organization/business-roles?tenant_id=${tenantId}`),
    authenticatedFetch(`/api/sop-migrations/coverage?tenant_id=${tenantId}`),
  ]);
  assert.equal(positionsResponse.status, 200);
  assert.equal(positionAssignmentsResponse.status, 200);
  assert.equal(rolesResponse.status, 200);
  assert.equal(beforeCoverage.status, 200);
  const positionsByCode = new Map(positionsResponse.body.map((item) => [item.code, item]));
  const rolesByCode = new Map(rolesResponse.body.map((item) => [item.role_code, item]));
  for (const [positionCode, roleCode] of intendedBindings) {
    assert.ok(positionsByCode.has(positionCode), `真实组织缺少岗位 ${positionCode}`);
    assert.ok(rolesByCode.has(roleCode), `角色目录缺少 ${roleCode}`);
  }
  for (const [positionCode] of intendedBackupAssignments) {
    assert.ok(positionsByCode.has(positionCode), `真实组织缺少备用岗位 ${positionCode}`);
  }

  await page.goto(`${baseUrl}/enterprise/organization`);
  await page.getByRole('tree', { name: '企业组织树' }).waitFor();
  await page.getByLabel('搜索组织').fill('财务部');
  await page.getByRole('button', { name: /财务部/ }).first().click();
  const financeManagerButton = page.getByRole('button', { name: /财务部门经理/ });
  await financeManagerButton.waitFor();
  await financeManagerButton.click();

  const financeManager = positionsByCode.get('POS-FIN-MANAGER');
  const departmentApprover = rolesByCode.get('expense_department_approver');
  const existingForManager = await authenticatedFetch(
    `/api/organization/position-role-bindings?tenant_id=${tenantId}&position_id=${financeManager.id}`,
  );
  assert.equal(existingForManager.status, 200);
  const alreadyBound = existingForManager.body.some(
    (binding) => binding.business_role_code === departmentApprover.role_code
      && binding.status === 'active',
  );
  if (!alreadyBound) {
    await page.getByRole('button', { name: '绑定', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.waitFor();
    await dialog.getByRole('combobox').click();
    await page.getByRole('option', { name: /超标报销部门负责人/ }).click();
    await dialog.getByText('绑定影响预览', { exact: true }).waitFor();
    const responsePromise = page.waitForResponse((response) => (
      new URL(response.url()).pathname === '/api/organization/position-role-bindings'
      && response.request().method() === 'POST'
    ));
    await dialog.getByRole('button', { name: '保存', exact: true }).click();
    const response = await responsePromise;
    assert.equal(response.status(), 200, '真实界面创建岗位角色绑定应成功');
  }

  const persistedBindings = [];
  for (const [positionCode, roleCode] of intendedBindings) {
    const position = positionsByCode.get(positionCode);
    const role = rolesByCode.get(roleCode);
    const response = await authenticatedFetch('/api/organization/position-role-bindings', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: tenantId,
        position_id: position.id,
        business_role_id: role.id,
      }),
    });
    assert.equal(response.status, 200, `${positionCode} → ${roleCode} 应幂等保存`);
    assert.equal(response.body.status, 'active');
    assert.ok(response.body.granted_by_user_id, '绑定必须记录授权人');
    assert.ok(response.body.effective_from, '绑定必须记录生效起点');
    persistedBindings.push(response.body);
  }

  const activeAssignmentsByPositionId = new Map(
    positionAssignmentsResponse.body
      .filter((assignment) => assignment.status === 'active')
      .map((assignment) => [assignment.position_id, assignment]),
  );
  const persistedBackupAssignments = [];
  for (const [positionCode, organizationName] of intendedBackupAssignments) {
    const position = positionsByCode.get(positionCode);
    const positionAssignment = activeAssignmentsByPositionId.get(position.id);
    assert.ok(positionAssignment, `${organizationName} 的备用岗位 ${positionCode} 必须有真人任职`);
    const response = await authenticatedFetch('/api/organization/employee-role-assignments', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: tenantId,
        employee_profile_id: positionAssignment.employee_profile_id,
        role_code: 'expense_department_approver',
        scope_type: 'org_unit',
        scope_id: position.org_unit_id,
        include_descendants: true,
        grant_reason: backupGrantReason,
        effective_until: backupEffectiveUntil,
      }),
    });
    assert.equal(response.status, 200, `${organizationName} 备用审批授权应幂等保存`);
    assert.equal(response.body.grant_reason, backupGrantReason);
    assert.equal(response.body.status, 'active');
    assert.ok(response.body.granted_by_user_id, '备用授权必须记录授权人');
    assert.ok(response.body.effective_until, '备用授权必须有截止时间');
    persistedBackupAssignments.push(response.body);
  }

  await page.reload();
  await page.getByLabel('搜索组织').fill('财务部');
  await page.getByRole('button', { name: /财务部/ }).first().click();
  await page.getByRole('button', { name: /财务部门经理/ }).click();
  const impactRail = page.getByRole('region', { name: '岗位流程责任影响' });
  await impactRail.waitFor();
  await impactRail.getByText('责任闭环轨道', { exact: true }).waitFor();
  await impactRail.getByText('超标报销部门负责人', { exact: true }).waitFor();
  const expenseImpact = impactRail.getByText('超标报销特批', { exact: true });
  await expenseImpact.first().waitFor();
  assert.ok(await expenseImpact.count() >= 1, '影响轨道应展示关联 SOP');

  await page.getByRole('button', { name: '解除', exact: true }).first().click();
  const confirmation = page.getByRole('alertdialog');
  await confirmation.getByText(/覆盖状态将按真实剩余来源重新计算/).waitFor();
  await confirmation.getByRole('button', { name: '取消', exact: true }).click();

  const afterCoverage = await authenticatedFetch(
    `/api/sop-migrations/coverage?tenant_id=${tenantId}`,
  );
  assert.equal(afterCoverage.status, 200);
  const expense = afterCoverage.body.entries.find(
    (entry) => entry.skill_id === 'expense_over_limit_approval',
  );
  const departmentNode = expense.dependency_assessment.human_participants.find(
    (participant) => participant.node_id === 'department_special_approval',
  );
  assert.equal(departmentNode.covered_context_count, departmentNode.context_count);
  assert.equal(departmentNode.uncovered_org_unit_ids.length, 0);
  assert.equal(afterCoverage.body.readiness_counts.ready, 22);
  assert.equal(afterCoverage.body.readiness_counts.blocked, 0);

  await page.goto(`${baseUrl}/enterprise/organization-roles?section=assignments`);
  const assignmentTable = page.getByRole('table', { name: '成员角色授权列表' });
  await assignmentTable.waitFor();
  await assignmentTable.getByText(backupGrantReason, { exact: true }).first().waitFor();
  await page.getByRole('button', { name: '授予成员角色', exact: true }).click();
  const assignmentDialog = page.getByRole('dialog');
  await assignmentDialog.getByRole('textbox', { name: '成员角色授权原因' }).waitFor();
  const defaultUntil = await assignmentDialog.getByLabel('成员角色授权截止时间').inputValue();
  assert.match(defaultUntil, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
  await assignmentDialog.getByRole('button', { name: '取消', exact: true }).click();

  await page.goto(`${baseUrl}/enterprise/organization-roles?section=agents`);
  const bindAgentButton = page.getByRole('button', { name: '绑定业务角色', exact: true });
  if (await bindAgentButton.count()) {
    await bindAgentButton.first().click();
    const agentDialog = page.getByRole('dialog');
    await agentDialog.getByRole('combobox', { name: '数字员工工作范围' }).click();
    await page.getByRole('option', { name: '指定组织', exact: true }).click();
    await agentDialog.getByText('授权组织', { exact: true }).waitFor();
    await agentDialog.getByLabel('数字员工角色授权截止时间').waitFor();
    await agentDialog.getByRole('button', { name: '取消', exact: true }).click();
  }

  await page.goto(`${baseUrl}/enterprise/organization`);
  await page.getByRole('tree', { name: '企业组织树' }).waitFor();
  await page.getByLabel('搜索组织').fill('财务部');
  await page.getByRole('button', { name: /财务部/ }).first().click();
  await page.getByRole('button', { name: /财务部门经理/ }).click();
  const finalImpactRail = page.getByRole('region', { name: '岗位流程责任影响' });
  await finalImpactRail.waitFor();
  await finalImpactRail.getByText('超标报销部门负责人', { exact: true }).waitFor();
  await page.screenshot({ path: screenshotPath, fullPage: true });
  assert.deepEqual(badResponses, []);
  assert.deepEqual(browserErrors, []);
  console.log(JSON.stringify({
    persistedBindingCount: persistedBindings.length,
    persistedBackupAssignmentCount: persistedBackupAssignments.length,
    coverageBefore: beforeCoverage.body.readiness_counts,
    coverageAfter: afterCoverage.body.readiness_counts,
    expenseContexts: {
      total: departmentNode.context_count,
      covered: departmentNode.covered_context_count,
      uncovered: departmentNode.uncovered_org_unit_ids.length,
    },
    screenshotPath,
    badResponses,
    browserErrors,
  }, null, 2));
} finally {
  await browser.close();
}
