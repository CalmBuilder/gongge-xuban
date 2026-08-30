/**
 * @Time       : 2026/08/29 23:05
 * @Author     : zhanglp8181
 * @File       : skill-governance-closure.fullstack.e2e.ts
 * @CallChain  : Chromium → Skill 管理/待我处理/数字员工发布库 → CAS 治理 API → SQLite
 * @Description: 验证 Skill 批量审核冲突、组织发布审核分支和 Release 下架/安全撤销闭环。
 */

import { expect, test, type Page } from '@playwright/test';

const TENANT_ID = 'tenant_demo';

type ApiResult<T> = {
  status: number;
  body: T;
};

type CatalogItem = {
  id: string;
  name: string;
  name_zh?: string | null;
  row_version: number;
  revision_row_version: number | null;
};

type CatalogPage = {
  items: CatalogItem[];
  total: number;
};

type Release = {
  id: string;
  resource_id: string;
  status: string;
  row_version: number;
};

type Agent = {
  id: string;
  name: string;
  status: string;
  profile_revision?: number;
  metadata?: Record<string, unknown>;
};

type Attention = {
  id: string;
  title?: string;
  payload: Record<string, unknown>;
  revision: number;
  status: string;
};

type AttentionPage = {
  items: Attention[];
};

test.describe.configure({ mode: 'serial', timeout: 120_000 });

test('Skill 批量审核拒绝与并发冲突会回滚选择并刷新目录', async ({ page }) => {
  /** 通过真实目录 UI 验证批量拒绝、两级 CAS 冲突和成员可见性边界。 */

  await prepareLogin(page, 'admin', 'admin');
  await page.goto('/enterprise/general-skills/catalog');
  await expect(page.getByText('共 37 个 Skill', { exact: true })).toBeVisible();

  const initialDrafts = await catalogJson<CatalogPage>(
    page,
    `/api/enterprise/general-skill-catalog?tenant_id=${TENANT_ID}&status=draft&page_size=100`,
  );
  expect(initialDrafts.total).toBe(37);

  await page.getByRole('checkbox').nth(0).check();
  await page.getByRole('checkbox').nth(1).check();
  await page.getByRole('button', { name: '批量拒绝' }).click();
  const rejectDialog = page.getByRole('dialog', { name: '批量审核 Skill' });
  await expect(rejectDialog).toBeVisible();
  await rejectDialog.getByLabel('审核说明（可选）').fill('真实浏览器验证候选拒绝和审计说明');
  await rejectDialog.getByRole('button', { name: '确认拒绝' }).click();
  await expect(rejectDialog).toBeHidden();

  const rejected = await catalogJson<CatalogPage>(
    page,
    `/api/enterprise/general-skill-catalog?tenant_id=${TENANT_ID}&status=archived&page_size=100`,
  );
  expect(rejected.total).toBe(2);

  const draft = await catalogJson<CatalogPage>(
    page,
    `/api/enterprise/general-skill-catalog?tenant_id=${TENANT_ID}&status=draft&page_size=100`,
  );
  const staleCandidate = draft.items[0];
  expect(staleCandidate).toBeTruthy();
  if (!staleCandidate) throw new Error('expected a remaining catalog draft');

  await page.getByRole('checkbox', { name: `选择 ${staleCandidate.name_zh || staleCandidate.name}` }).check();
  await page.getByRole('button', { name: '批量通过' }).click();
  const staleReviewDialog = page.getByRole('dialog', { name: '批量审核 Skill' });
  await expect(staleReviewDialog).toBeVisible();

  const winner = await catalogRequest<unknown>(
    page,
    '/api/enterprise/general-skill-catalog/review',
    {
      method: 'POST',
      body: {
        tenant_id: TENANT_ID,
        command_id: 'browser-catalog-cas-winner',
        items: [{
          skill_id: staleCandidate.id,
          decision: 'approve',
          expected_skill_row_version: staleCandidate.row_version,
          expected_revision_row_version: staleCandidate.revision_row_version || 1,
          review_note: '并发审核胜者',
        }],
      },
    },
  );
  expect(winner.status).toBe(200);

  await staleReviewDialog.getByRole('button', { name: '确认通过' }).click();
  await expect(staleReviewDialog).toBeHidden();
  await expect(page.getByText(/候选版本已变化，审核未提交/)).toBeVisible();
  await expect(
    page.getByRole('region', { name: '批量审核工具' }).getByText('已选 0 个待审核 Skill'),
  ).toBeVisible();

  const published = await catalogJson<CatalogPage>(
    page,
    `/api/enterprise/general-skill-catalog?tenant_id=${TENANT_ID}&status=published&page_size=100`,
  );
  expect(published.total).toBe(1);
  expect(published.items[0]?.id).toBe(staleCandidate.id);

  await prepareLogin(page, 'member', 'member');
  await page.goto('/enterprise/general-skills/catalog');
  await expect(page.getByText('共 1 个 Skill', { exact: true })).toBeVisible();
  await expect(page.getByRole('checkbox')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '导入外部 Skill' })).toHaveCount(0);
});

test('组织发布支持拒绝、并发审核刷新、普通下架和安全撤销', async ({ page }) => {
  /** 通过真实页面完成两类组织审核分支，并验证安全撤销向采用副本传播。 */

  await prepareLogin(page, 'member', 'member');
  const rejectedAgent = await catalogJson<Agent>(
    page,
    `/api/enterprise/agents/agent_e2e_rejected_organization?tenant_id=${TENANT_ID}`,
  );
  const rejectedSubmission = await catalogRequest<{ id: string }>(
    page,
    '/api/enterprise/publications',
    {
      method: 'POST',
      body: {
        resource_type: 'agent',
        resource_id: rejectedAgent.id,
        expected_resource_revision: rejectedAgent.profile_revision || 1,
      },
    },
  );
  expect(rejectedSubmission.status).toBe(200);

  await prepareLogin(page, 'admin', 'admin');
  await page.goto('/enterprise/work-items');
  const rejectedAttention = page.getByRole('button', { name: '审核组织发布：E2E 被拒组织员工' });
  await expect(rejectedAttention).toBeVisible();
  await rejectedAttention.click();
  const rejectedDialog = page.getByRole('dialog').filter({ hasText: '整 Agent 发布申请' });
  await expect(rejectedDialog).toBeVisible();
  await rejectedDialog.getByRole('button', { name: '拒绝组织发布' }).click();
  await expect(rejectedDialog).toBeHidden();

  const rejectedReleases = await catalogJson<Release[]>(
    page,
    `/api/enterprise/publications/releases?resource_type=agent&include_history=true`,
  );
  expect(rejectedReleases.some((release) => release.resource_id === rejectedAgent.id)).toBe(false);
  const resolvedAfterReject = await catalogJson<AttentionPage>(
    page,
    `/api/attention-items?tenant_id=${TENANT_ID}&view=resolved&page=1&page_size=100`,
  );
  expect(resolvedAfterReject.items).toEqual(expect.arrayContaining([
    expect.objectContaining({
      title: '审核组织发布：E2E 被拒组织员工',
      status: 'completed',
    }),
  ]));

  await page.goto('/enterprise/agents?view=organization');
  const pendingCard = page.locator('.gongge-employee-card').filter({ hasText: 'E2E 待组织化分身' }).first();
  await expect(pendingCard).toBeVisible();
  await pendingCard.click();
  const organizationDialog = page.getByRole('dialog', { name: /组织化检查：E2E 待组织化分身/ });
  await expect(organizationDialog).toBeVisible();
  await organizationDialog.getByLabel('选择业务角色').selectOption('e2e.admin.process');
  await organizationDialog.getByLabel('选择监督者').selectOption({ index: 1 });
  await organizationDialog.getByRole('button', { name: '保存组织化配置' }).click();
  await expect(organizationDialog.getByText('业务角色与监督者', { exact: true })).toBeVisible();

  await prepareLogin(page, 'member', 'member');
  await page.goto('/enterprise/agents?view=capability');
  const memberPendingCard = page.locator('.gongge-employee-card').filter({ hasText: 'E2E 待组织化分身' }).first();
  await expect(memberPendingCard).toBeVisible();
  await memberPendingCard.click();
  const ownerOrganizationDialog = page.getByRole('dialog', { name: /组织化检查：E2E 待组织化分身/ });
  await ownerOrganizationDialog.getByRole('button', { name: '提交组织审核' }).click();
  await expect(ownerOrganizationDialog).toBeHidden();

  const pendingAttentions = await catalogJson<AttentionPage>(
    page,
    `/api/attention-items?tenant_id=${TENANT_ID}&view=active&page=1&page_size=100`,
  );
  const pendingAttention = pendingAttentions.items.find(
    (item) => item.title === '审核组织发布：E2E 待组织化分身',
  );
  expect(pendingAttention).toBeTruthy();
  if (!pendingAttention) throw new Error('expected pending organization publication attention');
  const publicationRequestId = stringPayload(pendingAttention.payload, 'publication_request_id');
  const requestRowVersion = numberPayload(pendingAttention.payload, 'request_row_version');
  expect(publicationRequestId).not.toBe('');

  await prepareLogin(page, 'admin', 'admin');
  await page.goto('/enterprise/work-items');
  const pendingAttentionButton = page.getByRole('button', { name: '审核组织发布：E2E 待组织化分身' });
  await expect(pendingAttentionButton).toBeVisible();
  await pendingAttentionButton.click();
  const stalePublicationDialog = page.getByRole('dialog').filter({ hasText: '整 Agent 发布申请' });
  await expect(stalePublicationDialog).toBeVisible();

  const reviewWinner = await catalogRequest<unknown>(
    page,
    `/api/enterprise/publications/${encodeURIComponent(publicationRequestId)}/review`,
    {
      method: 'POST',
      body: {
        command_id: 'browser-publication-cas-winner',
        command: 'approve',
        expected_request_row_version: requestRowVersion,
        expected_attention_revision: pendingAttention.revision,
        comment: '并发审核胜者',
      },
    },
  );
  expect(reviewWinner.status).toBe(200);
  await stalePublicationDialog.getByRole('button', { name: '批准发布到组织广场' }).click();
  await expect(stalePublicationDialog).toBeHidden();

  const approvedReleases = await catalogJson<Release[]>(
    page,
    `/api/enterprise/publications/releases?resource_type=agent&include_history=true`,
  );
  expect(approvedReleases.some(
    (release) => release.resource_id === 'agent_e2e_pending_organization' && release.status === 'active',
  )).toBe(true);

  await unpublishOrganizationRelease(page, 'agent_e2e_ab_org_control', '验证普通下架不影响其他发布物');
  const afterUnpublish = await catalogJson<Release[]>(
    page,
    `/api/enterprise/publications/releases?resource_type=agent&include_history=true`,
  );
  const unpublished = afterUnpublish.find((release) => release.id === 'pubrel_e2e_ab_org_control');
  expect(unpublished?.status).toBe('unpublished');

  await prepareLogin(page, 'member', 'member');
  await page.goto('/enterprise/agents?view=capability');
  await page.getByRole('button', { name: '组织数字员工发布库' }).click();
  const memberReleaseDialog = page.getByRole('dialog', { name: '组织数字员工发布库' });
  await expect(memberReleaseDialog).toBeVisible();
  const employeeReleaseCard = memberReleaseDialog.locator('article').filter({ hasText: 'E2E 数字员工' }).first();
  await employeeReleaseCard.getByRole('button', { name: '采用为我的员工' }).click();
  await expect(memberReleaseDialog).toBeHidden();

  const ownedAfterAdoption = await catalogJson<Agent[]>(
    page,
    `/api/enterprise/agents?tenant_id=${TENANT_ID}&scope=owned`,
  );
  const adoptedAgent = ownedAfterAdoption.find(
    (agent) => agent.metadata?.adopted_release_id === 'pubrel_e2e_employee',
  );
  expect(adoptedAgent?.status).toBe('active');
  expect(adoptedAgent?.id).toBeTruthy();
  if (!adoptedAgent) throw new Error('expected adopted employee clone');

  await prepareLogin(page, 'admin', 'admin');
  await page.goto('/enterprise/agents?view=organization');
  await page.getByRole('button', { name: '组织数字员工发布库' }).click();
  const adminReleaseDialog = page.getByRole('dialog', { name: '组织数字员工发布库' });
  const revokeCard = adminReleaseDialog.locator('article')
    .filter({ hasText: 'E2E 数字员工' })
    .filter({ hasText: '已审 Release' })
    .first();
  await revokeCard.getByRole('button', { name: '安全撤销' }).click();
  const revokeDialog = page.getByRole('dialog', { name: '安全撤销组织数字员工 Release' });
  await expect(revokeDialog).toBeVisible();
  await revokeDialog.getByLabel('发布状态变更原因').fill('验证安全撤销会停止发现并停用已有采用副本');
  await revokeDialog.getByRole('button', { name: '确认安全撤销' }).click();
  await expect(revokeDialog).toBeHidden();

  const afterRevoke = await catalogJson<Release[]>(
    page,
    `/api/enterprise/publications/releases?resource_type=agent&include_history=true`,
  );
  expect(afterRevoke.find((release) => release.id === 'pubrel_e2e_employee')?.status).toBe('security_revoked');
  expect(afterRevoke.some(
    (release) => release.id === 'pubrel_e2e_employee' && release.status === 'active',
  )).toBe(false);

  await prepareLogin(page, 'member', 'member');
  const adoptedAfterRevoke = await catalogJson<Agent>(
    page,
    `/api/enterprise/agents/${encodeURIComponent(adoptedAgent.id)}?tenant_id=${TENANT_ID}`,
  );
  expect(adoptedAfterRevoke.status).toBe('inactive');
  const resourcesAfterRevoke = await catalogJson<Array<{ status: string }>>(
    page,
    `/api/enterprise/agents/${encodeURIComponent(adoptedAgent.id)}/resources?tenant_id=${TENANT_ID}`,
  );
  expect(resourcesAfterRevoke.every((binding) => binding.status === 'inactive')).toBe(true);

  const revokedAdoption = await catalogRequest<unknown>(
    page,
    '/api/enterprise/publications/releases/pubrel_e2e_employee/adopt',
    {
      method: 'POST',
      body: { idempotency_key: 'browser-revoked-adoption' },
    },
  );
  expect(revokedAdoption.status).toBe(404);
});

async function prepareLogin(page: Page, username: string, password: string): Promise<void> {
  /** 在真实页面上下文写入登录回执，避免测试绕过前端路由和认证存储。 */

  await page.goto('/');
  await page.evaluate(() => {
    localStorage.clear();
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
  });
  const result = await page.evaluate(async ({ tenantId, loginName, loginPassword }) => {
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
  expect(result).toBe(200);
}

async function catalogJson<T>(
  page: Page,
  path: string,
  init?: { method?: string; body?: unknown },
): Promise<T> {
  /** 读取成功 API 的 JSON 正文，失败时抛出明确的浏览器回归错误。 */

  const result = await catalogRequest<T>(page, path, init);
  if (result.status < 200 || result.status >= 300) {
    throw new Error(`catalog request failed: ${result.status} ${path}`);
  }
  return result.body;
}

async function catalogRequest<T>(
  page: Page,
  path: string,
  init?: { method?: string; body?: unknown },
): Promise<ApiResult<T>> {
  /** 通过浏览器认证上下文读取或提交 API，并保留非 2xx 回执供 CAS 断言。 */

  return page.evaluate(async ({ requestPath, requestMethod, requestBody }) => {
    const session = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const headers: Record<string, string> = {};
    if (session.token) headers.Authorization = `Bearer ${session.token}`;
    if (requestBody !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(requestPath, {
      method: requestMethod,
      headers,
      body: requestBody === undefined ? undefined : JSON.stringify(requestBody),
    });
    const text = await response.text();
    let body: unknown = null;
    if (text) {
      try {
        body = JSON.parse(text) as unknown;
      } catch {
        body = { raw: text };
      }
    }
    return { status: response.status, body } as ApiResult<T>;
  }, {
    requestPath: path,
    requestMethod: init?.method || 'GET',
    requestBody: init?.body,
  });
}

async function unpublishOrganizationRelease(page: Page, agentId: string, reason: string): Promise<void> {
  /** 通过发布库真实 UI 普通下架指定组织数字员工 Release。 */

  await page.goto('/enterprise/agents?view=organization');
  await page.getByRole('button', { name: '组织数字员工发布库' }).click();
  const releaseDialog = page.getByRole('dialog', { name: '组织数字员工发布库' });
  await expect(releaseDialog).toBeVisible();
  const targetCard = releaseDialog.locator('article').filter({ hasText: 'E2E 组织员工 A/B 对照' }).first();
  await targetCard.getByRole('button', { name: '普通下架' }).click();
  const transitionDialog = page.getByRole('dialog', { name: '普通下架组织数字员工 Release' });
  await expect(transitionDialog).toBeVisible();
  await transitionDialog.getByLabel('发布状态变更原因').fill(reason);
  await transitionDialog.getByRole('button', { name: '确认普通下架' }).click();
  await expect(transitionDialog).toBeHidden();
  const active = await catalogJson<Release[]>(
    page,
    `/api/enterprise/publications/releases?resource_type=agent`,
  );
  expect(active.some((release) => release.resource_id === agentId)).toBe(false);
}

function stringPayload(payload: Record<string, unknown>, key: string): string {
  /** 从 Attention payload 中读取用于后续 CAS 请求的非空字符串。 */

  const value = payload[key];
  return typeof value === 'string' ? value : '';
}

function numberPayload(payload: Record<string, unknown>, key: string): number {
  /** 从 Attention payload 中读取用于后续 CAS 请求的整数修订。 */

  const value = payload[key];
  if (typeof value !== 'number' || !Number.isInteger(value)) {
    throw new Error(`Attention payload field ${key} is not an integer`);
  }
  return value;
}
