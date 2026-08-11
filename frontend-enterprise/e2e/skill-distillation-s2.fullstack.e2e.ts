/**
 * @Time       : 2026/08/13
 * @Author     : zhanglp8181
 * @File       : skill-distillation-s2.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → Skill 治理 UI/API → 真实 FastAPI/SQLite/authorization ledger
 * @Description: 验证用户隔离、升级、pinned/follow-latest、回滚和停用后的目录即时失效。
 */

import { expect, test, type Page } from '@playwright/test';

type Catalog = {
  authorization_revision: number;
  items: Array<{
    skill_id: string;
    revision_id: string;
    revision_number: number;
    name: string;
  }>;
};

function scenarioSkill(catalogValue: Catalog) {
  const item = catalogValue.items.find((candidate) => candidate.name === 's2-versioned-skill');
  expect(item, 'S2 场景 Skill 必须存在于当前用户权威目录').toBeTruthy();
  return item!;
}

async function login(page: Page, username: string, password: string, agentId: string) {
  const status = await page.evaluate(async ({ username, password, agentId }) => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
    localStorage.setItem('gongge_enterprise_agent_scope', agentId);
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username, password }),
    });
    const body = await response.json();
    if (response.ok) localStorage.setItem('gongge_auth', JSON.stringify(body));
    return response.status;
  }, { username, password, agentId });
  expect(status).toBe(200);
}

async function catalog(page: Page): Promise<Catalog> {
  return page.evaluate(async () => {
    const session = JSON.parse(localStorage.getItem('gongge_auth') || '{}');
    const response = await fetch(
      '/api/enterprise/general-skill-governance/agents/agent_e2e_member_employee/catalog',
      { headers: { Authorization: `Bearer ${session.token}` } },
    );
    return response.json();
  });
}

async function importMarkdown(page: Page, markdown: string) {
  await page.getByRole('button', { name: /新增/ }).click();
  await page.getByRole('menuitem', { name: '安全导入 Skill' }).click();
  const dialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await dialog.locator('input[type="file"]').setInputFiles({
    name: 'SKILL.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from(markdown),
  });
  await dialog.getByRole('button', { name: '生成安全预览' }).click();
  await expect(dialog.getByText('s2-versioned-skill', { exact: true })).toBeVisible();
  const confirmResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/confirm')
    && response.request().method() === 'POST'
  ));
  await dialog.getByRole('button', { name: '固定版本并绑定' }).click();
  expect((await confirmResponse).status()).toBe(200);
}

async function openGovernance(page: Page) {
  const row = page.getByRole('row').filter({ hasText: 's2-versioned-skill' });
  await expect(row).toBeVisible();
  await row.getByRole('button', { name: '技能操作' }).click();
  await page.getByRole('menuitem', { name: '版本与调用策略' }).click();
  const dialog = page.getByRole('dialog', { name: '版本与调用策略' });
  await expect(dialog).toBeVisible();
  return dialog;
}

test('S2 三身份完成安全升级、版本策略、回滚与撤权即时闭环', async ({ page }) => {
  await page.goto('/enterprise/dashboard');
  await login(page, 'member', 'member', 'agent_e2e_member_employee');
  await page.goto('/enterprise/general-skills');
  await importMarkdown(
    page,
    '---\nname: s2-versioned-skill\ndescription: S2 version one.\n---\n# Version One\n',
  );
  const versionOne = await catalog(page);
  const versionOneSkill = scenarioSkill(versionOne);
  expect(versionOneSkill.revision_number).toBe(1);

  let governance = await openGovernance(page);
  await expect(governance.getByText('v1 · published')).toBeVisible();
  await governance.getByRole('button', { name: '导入新修订' }).click();
  const importDialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await importDialog.locator('input[type="file"]').setInputFiles({
    name: 'SKILL.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from(
      '---\nname: s2-versioned-skill\ndescription: S2 version two.\n---\n# Version Two\n',
    ),
  });
  await importDialog.getByRole('button', { name: '生成安全预览' }).click();
  await expect(importDialog.getByText('s2-versioned-skill', { exact: true })).toBeVisible();
  await importDialog.getByRole('button', { name: '固定版本并绑定' }).click();

  const stillPinned = await catalog(page);
  expect(scenarioSkill(stillPinned).revision_number).toBe(1);
  governance = await openGovernance(page);
  await expect(governance.getByText('v2 · published')).toBeVisible();
  await expect(governance.getByText('v1 · superseded')).toBeVisible();
  await page.screenshot({
    path: 'test-results/skill-s2-version-governance.png',
    fullPage: true,
  });
  await page.setViewportSize({ width: 375, height: 760 });
  await expect(governance.getByRole('button', { name: '保存策略' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.setViewportSize({ width: 1280, height: 720 });
  await governance.getByLabel('版本策略').click();
  await page.getByRole('option', { name: '跟随最新' }).click();
  const policyResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.includes('/general-skill-governance/bindings/')
    && response.request().method() === 'PATCH'
  ));
  await governance.getByRole('button', { name: '保存策略' }).click();
  expect((await policyResponse).status()).toBe(200);
  const following = await catalog(page);
  expect(scenarioSkill(following).revision_number).toBe(2);
  expect(following.authorization_revision).toBeGreaterThan(stillPinned.authorization_revision);

  governance = await openGovernance(page);
  const rollbackResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith('/rollback')
    && response.request().method() === 'POST'
  ));
  await governance.getByRole('button', { name: '回滚到此版本' }).click();
  expect((await rollbackResponse).status()).toBe(200);
  const rolledBack = await catalog(page);
  expect(scenarioSkill(rolledBack).revision_number).toBe(1);

  const row = page.getByRole('row').filter({ hasText: 's2-versioned-skill' });
  await row.getByRole('button', { name: '技能操作' }).click();
  const disableResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.includes('/general-skill-governance/bindings/')
    && response.request().method() === 'PATCH'
  ));
  await page.getByRole('menuitem', { name: '停用' }).click();
  expect((await disableResponse).status()).toBe(200);
  const disabled = await catalog(page);
  expect(disabled.items.some((item) => item.skill_id === versionOneSkill.skill_id)).toBe(false);

  await login(page, 'requestor', 'requestor', 'agent_e2e_member_employee');
  expect(
    (await catalog(page)).items.some((item) => item.skill_id === versionOneSkill.skill_id),
  ).toBe(false);

  await login(page, 'admin', 'admin', 'agent_e2e_employee');
  const adminRevisionStatus = await page.evaluate(async (skillId) => {
    const session = JSON.parse(localStorage.getItem('gongge_auth') || '{}');
    const response = await fetch(
      `/api/enterprise/general-skill-governance/skills/${skillId}/revisions?tenant_id=tenant_demo`,
      { headers: { Authorization: `Bearer ${session.token}` } },
    );
    return response.status;
  }, versionOneSkill.skill_id);
  expect(adminRevisionStatus).toBe(200);
});
