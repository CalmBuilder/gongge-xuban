/**
 * @Time       : 2026/08/01 23:48
 * @Author     : zhanglp8181
 * @File       : live_enterprise_topology_browser_regression.mjs
 * @CallChain  : ./app.sh:5137 + MySQL → Chromium 管理员 → 企业信息/组织拓扑
 * @Description: 真实浏览器核对企业组织读取、七节点责任拓扑、真实关系状态和新增企业弹窗，不写入测试数据。
 */

import assert from 'node:assert/strict';

import { chromium } from '../frontend-enterprise/node_modules/playwright/index.mjs';

const baseUrl = process.env.BROWSER_TEST_BASE_URL || 'http://127.0.0.1:5137';
const username = process.env.BROWSER_TEST_ADMIN_USERNAME || 'admin';
const password = process.env.BROWSER_TEST_ADMIN_PASSWORD || 'admin';
const screenshotPath = process.env.BROWSER_TEST_SCREENSHOT
  || '/tmp/gongge-enterprise-topology-regression.png';
const browserErrors = [];
const badResponses = [];
let organizationCreateRequests = 0;
const relationDisplayNames = {
  'OrganizationUnit.contains.Position': '组织单元包含岗位',
  'PositionAssignment.assigns.Human': '真人任职于岗位',
  'PositionRoleBinding.grants.BusinessRole': '岗位授予组织角色',
  'EmployeeRoleAssignment.delegates.BusinessRole': '真人获授组织角色',
  'AgentRoleBinding.grants.BusinessRole': '数字员工获授组织角色',
  'AgentResourceBinding.loads.SOP': '数字员工装载 SOP',
  'Human.supervises.Agent': '真人监督数字员工',
  'Human.owns.ExpertClone': '真人拥有能力分身',
};
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
const page = await context.newPage();

page.on('pageerror', (error) => browserErrors.push(`pageerror: ${error.message}`));
page.on('console', (message) => {
  if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`);
});
page.on('request', (request) => {
  const url = new URL(request.url());
  if (request.method() === 'POST' && url.pathname === '/api/organization/units') {
    organizationCreateRequests += 1;
  }
});
page.on('response', (response) => {
  const url = new URL(response.url());
  if (url.pathname.startsWith('/api/') && response.status() >= 500) {
    badResponses.push(`${response.status()} ${url.pathname}${url.search}`);
  }
});

/** 登录统一应用，并关闭会遮挡被测页面的首次使用引导。 */
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

try {
  await login();

  const [unitsResponse, positionRolesResponse, positionAssignmentsResponse, bindingsResponse, employeeRolesResponse] = await Promise.all([
    page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === '/api/organization/units' && response.request().method() === 'GET';
    }),
    page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === '/api/organization/position-role-bindings'
        && response.request().method() === 'GET';
    }),
    page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === '/api/organization/position-assignments'
        && response.request().method() === 'GET';
    }),
    page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === '/api/organization/agent-role-bindings'
        && response.request().method() === 'GET';
    }),
    page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === '/api/organization/employee-role-assignments'
        && response.request().method() === 'GET';
    }),
    page.goto(`${baseUrl}/enterprise/enterprise-info`),
  ]);
  assert.equal(unitsResponse.status(), 200, '企业组织列表应从真实后端成功读取');
  assert.equal(bindingsResponse.status(), 200, '数字员工角色绑定应从真实后端成功读取');
  assert.equal(employeeRolesResponse.status(), 200, '真人角色任职应从真实后端成功读取');
  assert.equal(positionRolesResponse.status(), 200, '岗位角色绑定应从真实后端成功读取');
  assert.equal(positionAssignmentsResponse.status(), 200, '岗位任职应从真实后端成功读取');
  const units = await unitsResponse.json();
  const bindings = await bindingsResponse.json();
  const employeeRoles = await employeeRolesResponse.json();
  const positionRoles = await positionRolesResponse.json();
  const positionAssignments = await positionAssignmentsResponse.json();
  assert.ok(Array.isArray(units) && units.some((unit) => unit.is_root), '组织数据应包含租户根节点');
  assert.ok(Array.isArray(bindings) && bindings.some((binding) => binding.status === 'active'), '应存在有效数字员工角色绑定');
  assert.ok(Array.isArray(employeeRoles) && employeeRoles.some((assignment) => assignment.status === 'active'), '应存在有效真人角色任职');
  assert.ok(Array.isArray(positionRoles) && positionRoles.some((binding) => binding.status === 'active'), '应存在有效岗位角色绑定');
  assert.ok(Array.isArray(positionAssignments) && positionAssignments.some((assignment) => assignment.status === 'active'), '应存在有效岗位任职');

  const topology = page.getByRole('region', {
    name: '真人、组织与岗位、组织角色、数字员工和专家拓扑图',
  });
  await topology.waitFor();
  const nodeKinds = await topology.locator('[data-topology-node]').evaluateAll((nodes) => (
    nodes.map((node) => node.getAttribute('data-topology-node'))
  ));
  assert.deepEqual([...new Set(nodeKinds)].sort(), ['agent', 'expert', 'human', 'organization', 'position', 'role', 'sop']);
  await topology.locator('[data-topology-example="live"]').waitFor();
  const modelRelations = await topology.locator('[data-topology-contract="model"] [data-topology-relation]').evaluateAll(
    (items) => items.map((item) => item.getAttribute('data-topology-relation')),
  );
  const liveRelations = await topology.locator('[data-topology-contract="live"] [data-topology-relation]').evaluateAll(
    (items) => items.map((item) => item.getAttribute('data-topology-relation')),
  );
  assert.equal(modelRelations.length, 8, '系统模型应声明八条统一关系契约');
  assert.deepEqual(liveRelations, modelRelations, '真实示例必须与系统模型使用同一关系契约');
  const liveRelationStatuses = await topology.locator('[data-topology-contract="live"] [data-topology-relation]').evaluateAll(
    (items) => Object.fromEntries(items.map((item) => [
      item.getAttribute('data-topology-relation'),
      item.getAttribute('data-relation-status'),
    ])),
  );
  assert.equal(liveRelationStatuses['AgentRoleBinding.grants.BusinessRole'], 'verified');
  assert.equal(liveRelationStatuses['AgentResourceBinding.loads.SOP'], 'verified');
  assert.equal(liveRelationStatuses['Human.owns.ExpertClone'], 'missing');
  const modelRelationCards = topology.locator('[data-topology-contract="model"] [data-topology-relation]');
  const chineseModelRelationTexts = await modelRelationCards.allTextContents();
  for (const [contract, chineseLabel] of Object.entries(relationDisplayNames)) {
    const card = topology.locator(`[data-topology-contract="model"] [data-topology-relation="${contract}"]`);
    assert.equal((await card.textContent())?.trim(), `${chineseLabel} · 已支持`);
    assert.equal((await card.textContent())?.includes(contract), false, `${contract} 在中文界面不应直接显示英文契约名`);
  }
  const undersizedText = await page.locator('[data-enterprise-info-page] *').evaluateAll((elements) => (
    elements.flatMap((element) => {
      const hasDirectText = [...element.childNodes].some(
        (node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim(),
      );
      const fontSize = Number.parseFloat(getComputedStyle(element).fontSize);
      return hasDirectText && fontSize < 11
        ? [`${fontSize}px: ${element.textContent?.trim().slice(0, 60)}`]
        : [];
    })
  ));
  assert.deepEqual(undersizedText, [], '企业与组织页面不应再出现小于 11px 的可见文字');
  for (const relation of ['包含岗位', '真人任职', '岗位默认角色', '数字员工绑定', '创建 / 拥有', '监督数字员工', '装载并运行']) {
    assert.ok(await topology.getByText(relation, { exact: true }).count() > 0, `拓扑应显示关系：${relation}`);
  }
  await topology.getByText('系统关系模型', { exact: true }).waitFor();
  await topology.getByText('实线：核心关系', { exact: true }).waitFor();
  await topology.getByText('虚线：能力分身等满足条件后成立', { exact: true }).waitFor();
  const model = topology.locator('[data-topology-model]');
  const modelBoxes = Object.fromEntries(await Promise.all(
    ['organization', 'position', 'human', 'expert', 'role', 'agent', 'sop'].map(async (kind) => [
      kind,
      await model.locator(`[data-topology-node="${kind}"]`).boundingBox(),
    ]),
  ));
  for (const [kind, box] of Object.entries(modelBoxes)) {
    assert.ok(box, `系统关系模型应显示 ${kind} 节点`);
  }
  assert.ok(modelBoxes.organization.x < modelBoxes.position.x, '组织单元应连接到其包含的岗位');
  assert.ok(modelBoxes.position.x < modelBoxes.human.x, '真人应位于岗位任职关系的右侧');
  assert.ok(modelBoxes.human.x < modelBoxes.expert.x, '能力分身应位于真人条件派生关系的右侧');
  assert.ok(modelBoxes.role.y > modelBoxes.position.y, '组织角色应位于岗位的可选默认角色关系下方');
  assert.ok(modelBoxes.agent.y > modelBoxes.human.y, '数字员工应位于真人治理监督关系下方');
  assert.ok(modelBoxes.role.x < modelBoxes.agent.x, '数字员工应向左绑定共同的组织角色枢纽');
  assert.ok(modelBoxes.agent.x < modelBoxes.sop.x, '数字员工应通过资源绑定装载并运行 SOP');
  await topology.getByText('当前系统真实示例', { exact: true }).waitFor();
  await topology.getByText('一次业务如何在人与数字员工之间闭环', { exact: true }).waitFor();
  assert.equal(await topology.locator('[data-workflow-step]').count(), 4, '每个场景应显示四步人机协作闭环');
  await topology.locator('[data-loop-return]').waitFor();
  assert.equal(await topology.getByText('合同审核岗', { exact: true }).count(), 0, '不得展示系统中不存在的合同审核岗');
  await topology.getByText('当前数据库没有可用于本图的来源绑定实例', { exact: false }).waitFor();
  const sopExamples = [
    ['费用报销', '财务报销专员', '财务报销专员'],
    ['用章申请', '用章申请操作员', '用章审批人'],
    ['请假与假勤', 'HR 假勤专员', 'HR 假勤专员'],
    ['IT 权限开通', 'IT 权限开通操作员', 'IT 高权限审批人'],
    ['合同风险初筛', '法务合同风险分析员', '法务合同复核专员'],
  ];
  assert.equal(await topology.getByRole('tab').count(), sopExamples.length, '应展示五个真实 SOP 场景');
  for (const [scenarioName, roleName, humanRoleName] of sopExamples) {
    await topology.getByRole('tab', { name: scenarioName, exact: true }).click();
    assert.ok(await topology.getByText(roleName, { exact: true }).count() > 0, `${scenarioName} 应显示数字员工执行角色`);
    assert.ok(await topology.getByText(humanRoleName, { exact: false }).count() > 0, `${scenarioName} 应显示真人把关角色`);
    const humanAssignment = employeeRoles.find(
      (assignment) => assignment.status === 'active' && assignment.role_name === humanRoleName,
    );
    assert.ok(humanAssignment?.employee_name, `${scenarioName} 的真人把关角色应存在有效员工任职`);
    assert.ok(
      await topology.getByText(humanAssignment.employee_name, { exact: false }).count() > 0,
      `${scenarioName} 应展示真实任职员工`,
    );
  }

  const enterprisePanelBox = await page.locator('[data-enterprise-panel]').boundingBox();
  const organizationPanelBox = await page.locator('[data-organization-panel]').boundingBox();
  const topologyBox = await topology.boundingBox();
  assert.ok(enterprisePanelBox && organizationPanelBox && topologyBox, '左右布局的三个主要区域都应可见');
  assert.ok(
    topologyBox.x > enterprisePanelBox.x + enterprisePanelBox.width,
    `拓扑图应位于企业操作区右侧：${JSON.stringify({ enterprisePanelBox, topologyBox })}`,
  );
  assert.ok(Math.abs(organizationPanelBox.x - enterprisePanelBox.x) < 2, '组织单元应与企业资料在同一左栏');
  const organizationUnitCardBoxes = await page.locator('[data-organization-unit-card]').evaluateAll((cards) => (
    cards.map((card) => {
      const box = card.getBoundingClientRect();
      return { left: box.left, right: box.right, width: box.width };
    })
  ));
  assert.ok(organizationUnitCardBoxes.length > 0, '组织面板应显示至少一个企业组织单元卡片');
  for (const cardBox of organizationUnitCardBoxes) {
    assert.ok(
      cardBox.left >= organizationPanelBox.x - 1
        && cardBox.right <= organizationPanelBox.x + organizationPanelBox.width + 1,
      `组织单元卡片不得溢出左栏：${JSON.stringify({ organizationPanelBox, cardBox })}`,
    );
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  const compactOrganizationPanelBox = await page.locator('[data-organization-panel]').boundingBox();
  const compactOrganizationUnitCardBoxes = await page.locator('[data-organization-unit-card]').evaluateAll((cards) => (
    cards.map((card) => {
      const box = card.getBoundingClientRect();
      return { left: box.left, right: box.right, width: box.width };
    })
  ));
  assert.ok(compactOrganizationPanelBox, '1440px 视口下组织面板应可见');
  for (const cardBox of compactOrganizationUnitCardBoxes) {
    assert.ok(
      cardBox.left >= compactOrganizationPanelBox.x - 1
        && cardBox.right <= compactOrganizationPanelBox.x + compactOrganizationPanelBox.width + 1,
      `1440px 视口下组织单元卡片不得溢出左栏：${JSON.stringify({ compactOrganizationPanelBox, cardBox })}`,
    );
  }

  const addButton = page.getByRole('button', { name: '新增企业' });
  await addButton.waitFor();
  assert.equal(await addButton.isDisabled(), false, '管理员在根组织加载后应可新增企业');
  await addButton.click();
  await page.getByRole('dialog').waitFor();
  await page.getByRole('textbox', { name: '稳定企业编码' }).fill('1-invalid-code');
  await page.getByRole('textbox', { name: '新增企业名称' }).fill('浏览器回归企业');
  await page.getByRole('button', { name: '创建企业组织' }).click();
  const validationToast = page.getByText('企业编码须以字母开头，只能包含字母、数字、下划线或短横线', { exact: true });
  await validationToast.waitFor();
  assert.equal(organizationCreateRequests, 0, '非法编码应在客户端拦截，不能污染真实组织数据');
  await page.getByRole('button', { name: '取消' }).click();
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  await validationToast.waitFor({ state: 'hidden', timeout: 10_000 });

  await page.screenshot({ path: screenshotPath, fullPage: true });
  await page.getByRole('button', { name: '切换语言' }).click();
  await page.getByRole('menuitem', { name: 'English' }).click();
  for (const contract of Object.keys(relationDisplayNames)) {
    await page.waitForFunction(
      ({ relationContract, expectedText }) => document.querySelector(`[data-topology-contract="model"] [data-topology-relation="${relationContract}"]`)?.textContent?.trim() === expectedText,
      { relationContract: contract, expectedText: `${contract} · Supported` },
    );
  }
  assert.deepEqual(badResponses, []);
  assert.deepEqual(browserErrors, []);
  console.log(JSON.stringify({
    organizationUnitCount: units.length,
    activeAgentRoleBindingCount: bindings.filter((binding) => binding.status === 'active').length,
    activeEmployeeRoleAssignmentCount: employeeRoles.filter((assignment) => assignment.status === 'active').length,
    activePositionRoleBindingCount: positionRoles.filter((binding) => binding.status === 'active').length,
    activePositionAssignmentCount: positionAssignments.filter((assignment) => assignment.status === 'active').length,
    sopExampleCount: sopExamples.length,
    nodeKinds,
    relationContracts: modelRelations,
    chineseModelRelationTexts,
    liveRelationStatuses,
    organizationCreateRequests,
    organizationUnitCardBoxes,
    compactOrganizationUnitCardBoxes,
    screenshotPath,
    badResponses,
    browserErrors,
  }, null, 2));
} finally {
  await browser.close();
}
