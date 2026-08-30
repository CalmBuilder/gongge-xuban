/**
 * @Time       : 2026/08/29 20:05
 * @Author     : zhanglp8181
 * @File       : skill-catalog.fullstack.e2e.ts
 * @CallChain  : Chromium → Skill 管理 → 管理员审核 → 成员详情安装 → Agent Skill 目录
 * @Description: 以真实浏览器验证项目内置 Skill 的候选、发布、权限隔离和能力分身安装闭环。
 */

import { expect, test, type Page } from '@playwright/test';

const TENANT_ID = 'tenant_demo';

async function loginAs(page: Page, username: string, password: string): Promise<void> {
  const status = await page.evaluate(async ({ tenantId, loginName, loginPassword }) => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: tenantId, username: loginName, password: loginPassword }),
    });
    const body = await response.json() as { token?: string; user?: { id?: string } };
    if (response.ok && body.token && body.user?.id) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
    }
    return response.status;
  }, { tenantId: TENANT_ID, loginName: username, loginPassword: password });
  expect(status).toBe(200);
}

async function catalogJson<T>(page: Page, path: string): Promise<T> {
  return page.evaluate(async (requestPath) => {
    const rawSession = localStorage.getItem('gongge_auth');
    const session = rawSession ? JSON.parse(rawSession) as { token?: string } : {};
    const response = await fetch(requestPath, {
      headers: session.token ? { Authorization: `Bearer ${session.token}` } : {},
    });
    if (!response.ok) throw new Error(`catalog request failed: ${response.status}`);
    return response.json() as Promise<T>;
  }, path);
}

test.describe.configure({ timeout: 90_000 });

test('S2 Skill 管理在管理员和普通成员角色下完成审核、开放平台发现与安装', async ({ page }) => {
  const browserFailures: string[] = [];
  const skillCatalogResponseFailures: string[] = [];
  page.on('pageerror', (error) => browserFailures.push(error.message));
  page.on('console', (message) => {
    // Shell 可能在用户切换瞬间继续完成旧员工范围的预加载；该既有请求的
    // 403 由页面范围切换处理，Skill 管理接口则由下方精确 URL 门禁校验。
    if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) {
      browserFailures.push(message.text());
    }
  });
  page.on('response', (response) => {
    const url = response.url();
    const isSkillCatalogRequest = url.includes('/api/enterprise/general-skill-catalog')
      || url.includes('/api/enterprise/my-general-skills')
      || url.includes('/api/enterprise/general-skills');
    if (isSkillCatalogRequest && response.status() >= 400) {
      skillCatalogResponseFailures.push(`${response.status()} ${url} [page=${page.url()}]`);
    }
  });

  await page.goto('/');
  await page.evaluate(() => localStorage.clear());
  await loginAs(page, 'admin', 'admin');
  await page.goto('/enterprise/general-skills/catalog');
  await expect(page.getByText('Skill 管理', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('共 37 个 Skill', { exact: true })).toBeVisible();
  await expect(page.getByRole('combobox', { name: '来源' })).toBeVisible();
  await expect(page.getByRole('button', { name: '导入外部 Skill' })).toBeVisible();
  await page.getByRole('combobox', { name: '来源' }).click();
  await page.getByRole('option', { name: '项目内置快照' }).click();
  await page.getByRole('button', { name: '查询' }).click();
  await expect(page).toHaveURL(/source_kind=platform_builtin/);
  await expect(page.getByText('共 37 个 Skill', { exact: true })).toBeVisible();
  await expect(page.getByText('向 Matt 提问', { exact: true })).toBeVisible();

  const firstDetailLink = page.getByRole('link', { name: '查看详情' }).first();
  const detailHref = await firstDetailLink.getAttribute('href');
  expect(detailHref).toMatch(/^\/enterprise\/general-skills\/catalog\//);
  await firstDetailLink.click();
  await expect(page.getByRole('heading', { name: 'Skill 详情' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '向 Matt 提问' })).toBeVisible();
  await expect(page.getByText('中文解读仅用于阅读；实际运行始终使用英文原文。', { exact: true })).toBeVisible();
  await expect(page.getByText('英文原名：ask-matt · ask-matt', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '英文原文（运行时使用）' }).click();
  await expect(page.locator('pre').first()).toContainText('name: ask-matt');
  await page.getByRole('button', { name: '中文解读' }).click();
  await expect(page.getByText('来源证据', { exact: true })).toBeVisible();
  await expect(page.getByText('6654f6b60cd9d5be8b54c6fafe44346dabeb3b76', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: '返回 Skill 管理' }).click();
  await expect(page.getByText('共 37 个 Skill', { exact: true })).toBeVisible();

  await page.getByRole('checkbox').first().check();
  await page.getByRole('button', { name: '批量通过' }).click();
  const reviewDialog = page.getByRole('dialog', { name: '批量审核 Skill' });
  await expect(reviewDialog).toBeVisible();
  await reviewDialog.getByLabel('审核说明（可选）').fill('真实浏览器复核来源、风险和固定修订');
  await reviewDialog.getByRole('button', { name: '确认通过' }).click();
  await expect(reviewDialog).toBeHidden();

  const published = await catalogJson<{
    items: Array<{ id: string; slug: string; revision_id: string | null }>;
    total: number;
  }>(page, `/api/enterprise/general-skill-catalog?tenant_id=${TENANT_ID}&status=published&page_size=20`);
  expect(published.total).toBe(1);
  const publishedSkill = published.items[0];
  expect(publishedSkill?.revision_id).toBeTruthy();

  await page.goto('/enterprise/platform/general-skills');
  await expect(page.getByRole('heading', { name: 'Skill' })).toBeVisible();
  await expect(page.getByRole('tab', { name: /Skill/ })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('button', { name: '发布' })).toHaveCount(0);
  const publishedSkillCard = page.locator('.gongge-platform-resource-card').first();
  await expect(publishedSkillCard).toBeVisible();
  await publishedSkillCard.click();
  await expect(page.getByRole('link', { name: '查看详情' })).toBeVisible();
  await page.getByRole('link', { name: '查看详情' }).click();
  await expect(page.getByRole('heading', { name: 'Skill 详情' })).toBeVisible();

  await page.goto('/enterprise/agents?view=expert');
  await expect(page.getByRole('heading', { name: '专家模板管理' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '专家模板目录' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '搜索专家模板' })).toBeVisible();
  await expect(page.getByRole('link', { name: '前往开放广场的专家分类' })).toHaveAttribute(
    'href',
    '/enterprise/platform/experts',
  );
  await expect(page.getByText(/^E2E 数据治理专家(?: @.*)?$/)).toBeVisible();

  await page.goto('/enterprise/agents');
  await expect(page.getByRole('heading', { name: '可管理数字员工' })).toBeVisible();
  await expect(page.getByText(/^E2E 数据治理专家(?: @.*)?$/)).toHaveCount(0);

  await loginAs(page, 'member', 'member');
  await page.goto(`/enterprise/general-skills/catalog/${encodeURIComponent(publishedSkill.slug)}`);
  await expect(page.getByRole('heading', { name: 'Skill 详情' })).toBeVisible();
  await expect(page.getByRole('button', { name: '安装到我的能力分身' })).toBeVisible();
  await expect(page.getByRole('button', { name: '绑定到组织数字员工' })).toHaveCount(0);
  await page.getByRole('button', { name: '安装到我的能力分身' }).click();
  const installDialog = page.getByRole('dialog', { name: '安装到我的能力分身' });
  await expect(installDialog).toBeVisible();
  await expect(installDialog.getByRole('combobox', { name: '目标 Agent' })).toBeVisible();
  await installDialog.getByRole('combobox', { name: '目标 Agent' }).click();
  await page.getByRole('option', { name: 'E2E 疑难故障诊断分身' }).click();
  await installDialog.getByRole('button', { name: '确认安装' }).click();
  await expect(installDialog).toBeHidden();

  const installed = await catalogJson<Array<{ id: string; status: string; binding_id?: string }>>(
    page,
    `/api/enterprise/general-skills?tenant_id=${TENANT_ID}&agent_id=agent_e2e_diagnosis`,
  );
  expect(installed.find((skill) => skill.id === publishedSkill.id)?.status).toBe('published');

  await page.goto('/enterprise/platform/experts');
  const expertCardName = page.getByText(/^E2E 数据治理专家(?: @.*)?$/);
  await expect(expertCardName).toBeVisible();
  await expertCardName.click();
  await expect(page.getByRole('button', { name: '添加使用并开始对话' })).toBeVisible();
  await page.getByRole('button', { name: '添加使用并开始对话' }).click();
  await expect(page).toHaveURL(/\/workspace\/chat\/draft\/agent_e2e_expert_template$/);

  await page.goto('/enterprise/platform/experts');
  const expertEmployeeCard = page.locator('.gongge-employee-card').filter({ hasText: 'E2E 数据治理专家' }).first();
  await expertEmployeeCard.click();
  await page.getByRole('button', { name: '复制并定制' }).click();
  const createAgentDialog = page.getByRole('dialog', { name: '新建数字员工' });
  await expect(createAgentDialog).toBeVisible();
  await createAgentDialog.getByLabel('数字员工姓名').fill('我的数据治理专家');
  await createAgentDialog.getByRole('button', { name: '创建' }).click();
  await expect(createAgentDialog).toBeHidden();
  const ownedExpertAgents = await catalogJson<Array<{
    id: string;
    name: string;
    owner_user_id?: string;
    source_agent_id?: string;
    visibility_scope?: string;
  }>>(page, `/api/enterprise/agents?tenant_id=${TENANT_ID}&scope=owned`);
  const copiedExpert = ownedExpertAgents.find((agent) => agent.name === '我的数据治理专家');
  expect(copiedExpert).toMatchObject({
    owner_user_id: 'member_e2e',
    source_agent_id: 'agent_e2e_expert_template',
    visibility_scope: 'private',
  });

  await loginAs(page, 'admin', 'admin');
  await page.goto(`/enterprise/general-skills/catalog/${encodeURIComponent(publishedSkill.slug)}`);
  await expect(page.getByRole('button', { name: '绑定到组织数字员工' })).toBeVisible();
  await page.getByRole('button', { name: '绑定到组织数字员工' }).click();
  const bindDialog = page.getByRole('dialog', { name: '绑定到组织数字员工' });
  await expect(bindDialog).toBeVisible();
  await expect(bindDialog.getByRole('combobox', { name: '目标 Agent' })).toBeVisible();
  await bindDialog.getByRole('combobox', { name: '目标 Agent' }).click();
  await page.getByRole('option', { name: 'E2E 数字员工' }).click();
  await bindDialog.getByRole('button', { name: '确认绑定' }).click();
  await expect(bindDialog).toBeHidden();

  const organizationSkills = await catalogJson<Array<{
    id: string;
    status: string;
    binding_id?: string;
  }>>(page, `/api/enterprise/general-skills?tenant_id=${TENANT_ID}&agent_id=agent_e2e_employee`);
  expect(organizationSkills.find((skill) => skill.id === publishedSkill.id)?.status).toBe('published');
  const organizationBinding = await catalogJson<Array<{
    id: string;
    agent_id: string;
    resource_id: string;
    status: string;
    metadata?: { managed_catalog?: boolean };
  }>>(page, `/api/enterprise/agents/agent_e2e_employee/resources?tenant_id=${TENANT_ID}`);
  const boundSkill = organizationBinding.find((binding) => binding.resource_id === publishedSkill.id);
  expect(boundSkill?.status).toBe('active');
  expect(boundSkill?.metadata?.managed_catalog).toBe(true);

  await page.goto('/enterprise/agents?view=organization');
  await expect(page.getByRole('link', { name: /^组织数字员工/ })).toBeVisible();
  const organizationManagement = await catalogJson<{
    items: Array<{ id: string; name: string; governance_form?: string; governance_reasons?: string[] }>;
    total: number;
  }>(page, `/api/enterprise/agents/management-page?tenant_id=${TENANT_ID}&view=organization&page=1&page_size=12`);
  expect(organizationManagement.items.map((item) => ({
    id: item.id,
    name: item.name,
    form: item.governance_form,
    reasons: item.governance_reasons,
  }))).toContainEqual({
    id: 'agent_e2e_employee',
    name: 'E2E 数字员工',
    form: 'organization_employee',
    reasons: ['active_organization_release'],
  });
  const organizationCardName = page.getByText(/^E2E 数字员工(?: @admin)?$/);
  await expect(organizationCardName).toBeVisible();
  await organizationCardName.click();
  const organizationPreview = page.getByRole('dialog', { name: /组织化检查：E2E 数字员工/ });
  await expect(organizationPreview).toBeVisible();
  await expect(organizationPreview.getByText('当前形态：', { exact: false })).toContainText('组织数字员工');
  await expect(organizationPreview.getByText('组织发布 Release', { exact: true })).toBeVisible();
  await expect(organizationPreview.getByText('已满足', { exact: true })).toHaveCount(5);
  await organizationPreview.getByRole('button', { name: '关闭' }).click();
  await expect(organizationPreview).toBeHidden();

  const pendingOrganizationCard = page.locator('.gongge-employee-card').filter({ hasText: 'E2E 待组织化分身' }).first();
  await pendingOrganizationCard.click();
  const pendingPreview = page.getByRole('dialog', { name: /组织化检查：E2E 待组织化分身/ });
  await expect(pendingPreview).toBeVisible();
  await pendingPreview.getByLabel('选择业务角色').selectOption('e2e.admin.process');
  await pendingPreview.getByLabel('选择监督者').selectOption({ index: 1 });
  await pendingPreview.getByRole('button', { name: '保存组织化配置' }).click();
  await expect(pendingPreview.getByText('业务角色与监督者', { exact: true })).toBeVisible();
  const configuredPreview = await catalogJson<{
    governance_form: string;
    can_submit: boolean;
    active_role_code?: string;
    active_supervisor_employee_profile_id?: string;
  }>(page, `/api/enterprise/agents/agent_e2e_pending_organization/organizationization-preview?tenant_id=${TENANT_ID}`);
  expect(configuredPreview).toMatchObject({
    governance_form: 'organization_pending',
    can_submit: true,
    active_role_code: 'e2e.admin.process',
  });
  expect(configuredPreview.active_supervisor_employee_profile_id).toBeTruthy();

  await loginAs(page, 'member', 'member');
  await page.goto('/enterprise/agents?view=capability');
  const memberPendingCard = page.locator('.gongge-employee-card').filter({ hasText: 'E2E 待组织化分身' }).first();
  await expect(memberPendingCard).toBeVisible();
  await memberPendingCard.click();
  const ownerOrganizationPreview = page.getByRole('dialog', { name: /组织化检查：E2E 待组织化分身/ });
  await expect(ownerOrganizationPreview).toBeVisible();
  await ownerOrganizationPreview.getByRole('button', { name: '提交组织审核' }).click();
  await expect(ownerOrganizationPreview).toBeHidden();
  const pendingPublicationAttentions = await catalogJson<{ items: Array<{
    kind: string;
    payload: { publication_request_kind?: string };
  }> }>(page, `/api/attention-items?tenant_id=${TENANT_ID}&view=active&page=1&page_size=100`);
  expect(pendingPublicationAttentions.items).toEqual(expect.arrayContaining([
    expect.objectContaining({
      kind: 'publication',
      payload: expect.objectContaining({ publication_request_kind: 'agent' }),
    }),
  ]));

  await loginAs(page, 'admin', 'admin');
  await page.goto('/enterprise/work-items');
  const publicationAttention = page.getByRole('button', { name: /审核组织发布：E2E 待组织化分身/ });
  await expect(publicationAttention).toBeVisible();
  await publicationAttention.click();
  const publicationReviewDialog = page.getByRole('dialog').filter({ hasText: '整 Agent 发布申请' });
  await expect(publicationReviewDialog).toBeVisible();
  await publicationReviewDialog.getByRole('button', { name: '批准发布到组织广场' }).click();
  await expect(publicationReviewDialog).toBeHidden();

  const approvedAgentReleases = await catalogJson<Array<{
    id: string;
    resource_id: string;
    status: string;
    snapshot_checksum: string;
  }>>(page, `/api/enterprise/publications/releases?resource_type=agent&include_history=true`);
  const approvedPendingRelease = approvedAgentReleases.find(
    (release) => release.resource_id === 'agent_e2e_pending_organization' && release.status === 'active',
  );
  expect(approvedPendingRelease?.snapshot_checksum).toBeTruthy();

  await page.goto('/enterprise/agents?view=organization');
  const publishedPendingCard = page.locator('.gongge-employee-card').filter({ hasText: 'E2E 待组织化分身' }).first();
  await expect(publishedPendingCard).toBeVisible();
  const publishedPendingManagement = await catalogJson<{
    items: Array<{ id: string; governance_form?: string; governance_reasons?: string[] }>;
  }>(page, `/api/enterprise/agents/management-page?tenant_id=${TENANT_ID}&view=organization&page=1&page_size=12`);
  expect(publishedPendingManagement.items).toEqual(expect.arrayContaining([
    expect.objectContaining({
      id: 'agent_e2e_pending_organization',
      governance_form: 'organization_employee',
      governance_reasons: ['active_organization_release'],
    }),
  ]));

  await page.getByRole('button', { name: '组织数字员工发布库' }).click();
  const releaseDialog = page.getByRole('dialog', { name: '组织数字员工发布库' });
  await expect(releaseDialog).toBeVisible();
  const historicalReleaseCard = releaseDialog.locator('article').filter({ hasText: '历史已下架' }).filter({ hasText: 'E2E 数字员工' }).first();
  await expect(historicalReleaseCard.getByRole('button', { name: '回滚此版本' })).toBeVisible();
  await historicalReleaseCard.getByRole('button', { name: '回滚此版本' }).click();
  const rollbackDialog = page.getByRole('dialog', { name: '回滚组织数字员工版本' });
  await expect(rollbackDialog).toBeVisible();
  await rollbackDialog.getByLabel('回滚原因').fill('真实浏览器验证历史组织发布恢复与发现投影同步');
  await rollbackDialog.getByRole('button', { name: '确认回滚' }).click();
  await expect(rollbackDialog).toBeHidden();
  const rolledBackReleases = await catalogJson<Array<{
    id: string;
    resource_id: string;
    status: string;
  }>>(page, `/api/enterprise/publications/releases?resource_type=agent&include_history=true`);
  expect(rolledBackReleases.find((release) => release.id === 'pubrel_e2e_employee_history')?.status).toBe('active');
  expect(rolledBackReleases.find((release) => release.id === 'pubrel_e2e_employee')?.status).toBe('unpublished');
  const rolledBackEmployee = await catalogJson<{
    governance_form?: string;
    organization_release_id?: string;
  }>(page, `/api/enterprise/agents/agent_e2e_employee?tenant_id=${TENANT_ID}`);
  expect(rolledBackEmployee.governance_form).toBe('organization_employee');
  expect(rolledBackEmployee.organization_release_id).toBe('pubrel_e2e_employee_history');
  expect(skillCatalogResponseFailures).toEqual([]);
  expect(browserFailures).toEqual([]);
});
