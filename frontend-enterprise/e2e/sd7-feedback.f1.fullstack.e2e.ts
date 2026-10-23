/**
 * @Time       : 2026/10/02
 * @Author     : zhanglp8181
 * @File       : sd7-feedback.f1.fullstack.e2e.ts
 * @CallChain  : 双 actor 真实页面 → 原反馈预览 → 明确分享 → 管理投影 → 撤回后拒绝
 * @Description: 同一断言运行于隔离 SQLite/MySQL，不预置来源、材料或分享，不替代必要 UI。
 */
import { expect, test, type Locator, type Page, type Response } from '@playwright/test';

import type { FeedbackMaterialRead } from '../src/types/feedback';

const AGENT_ID = 'agent_sd7_feedback';
const SESSION_ID = 'session_sd7_feedback_legacy';
const FEEDBACK_ID = 'fb_sd7_feedback_legacy';
const FEEDBACK_PAGE = `/enterprise/feedback?agent_id=${AGENT_ID}`;
const NOTE = 'Please verify the business result before marking the task complete.';
const PRIVATE_MARKERS = [
  'SD7_PRIVATE_USER_BYTES', 'SD7_PRIVATE_CONVERSATION_BYTES', 'SD7_PRIVATE_SLOT_BYTES',
  'SD7_PRIVATE_MODEL_INFERENCE_BYTES', 'SD7_PRIVATE_MODEL_REASON_BYTES', 'SD7_PRIVATE_MODEL_SLOT_BYTES',
];

async function login(page: Page, username: string, passwordEnvironment: string): Promise<void> {
  /** 登录也经过真实页面，凭据仅存在进程环境及浏览器内存，不输出认证状态。 */
  const password = process.env[passwordEnvironment];
  if (!password) throw new Error('SD7 synthetic login environment is required');
  await page.goto(FEEDBACK_PAGE);
  await page.evaluate((agentId) => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
    localStorage.setItem('gongge_enterprise_agent_scope', agentId);
  }, AGENT_ID);
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill(username);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect.poll(() => page.evaluate(() => Boolean(localStorage.getItem('gongge_auth')))).toBe(true);
  await page.goto(FEEDBACK_PAGE);
  await expect(page.getByRole('region', { name: '反馈改进材料' })).toBeVisible();
}

function materialResponse(page: Page, suffix: string, method: string): Promise<Response> {
  return page.waitForResponse((response) => response.request().method() === method
    && new URL(response.url()).pathname.endsWith(suffix));
}

async function assertProjectionPrivate(scope: Locator, value?: FeedbackMaterialRead): Promise<void> {
  /** 本人只核对预览域，管理者核对整页；同一全量标记集合及返回投影不进入报告。 */
  const text = await scope.innerText();
  const encoded = value ? JSON.stringify(value) : '';
  for (const marker of PRIVATE_MARKERS) {
    expect(text.includes(marker), 'Private conversation data must not be shown').toBe(false);
    expect(encoded.includes(marker), 'Private conversation data must not enter the material').toBe(false);
  }
}

test('F1 FB-R01/03: owner previews and explicitly shares; another manager sees only safe material; withdrawal rejects stale detail', async ({ page, browser }, info) => {
  test.setTimeout(180_000);
  test.skip(process.env.FULLSTACK_SD7 !== '1', 'Requires the isolated SD7 fixture');
  const managerContext = await browser.newContext({ baseURL: info.project.use.baseURL });
  const manager = await managerContext.newPage();
  try {
    await login(page, 'sd7-owner', 'FULLSTACK_SD7_OWNER_PASSWORD');
    await login(manager, 'sd7-manager', 'FULLSTACK_SD7_MANAGER_PASSWORD');
    const ownerMaterials = page.getByRole('region', { name: '反馈改进材料' });
    const managerMaterials = manager.getByRole('region', { name: '反馈改进材料' });
    await expect(managerMaterials).toHaveAttribute('aria-busy', 'false');
    await expect(managerMaterials.getByText('暂无可管理的共享材料')).toBeVisible();
    await assertProjectionPrivate(manager.locator('body'));

    await page.getByRole('button', { name: '查看', exact: true }).last().click();
    const conversationDialog = page.getByRole('dialog', { name: '对话日志详情' });
    await expect(conversationDialog.getByText('SD7_PRIVATE_CONVERSATION_BYTES', { exact: true })).toBeVisible();
    await conversationDialog.getByRole('button', { name: '预览改进材料', exact: true }).click();
    const previewDialog = page.getByRole('dialog', { name: '预览改进材料' });
    await previewDialog.getByRole('textbox', { name: '用户说明' }).fill(NOTE);
    const previewResult = materialResponse(page, `/${FEEDBACK_ID}/materials/preview`, 'POST');
    await previewDialog.getByRole('button', { name: '生成脱敏预览' }).click();
    const previewResponse = await previewResult;
    expect(previewResponse.status()).toBe(200);
    const preview = await previewResponse.json() as FeedbackMaterialRead;
    expect(preview.feedback_id).toBe(FEEDBACK_ID);
    expect(preview.target_agent_id).toBe(AGENT_ID);
    expect(preview.status).toBe('preview');
    expect(preview.material.source.status).toBe('missing');
    expect(preview.material.source.execution_id).toBeNull();
    expect(preview.material.source.skill_version_id).toBeNull();
    await expect(previewDialog.getByRole('heading', { name: '确定执行事实' })).toBeVisible();
    await expect(previewDialog.getByRole('heading', { name: '模型归因（推断）' })).toBeVisible();
    expect(preview.material.facts.operations_known).toBe(false);
    await expect(previewDialog.getByText('缺少可验证工具调用记录')).toBeVisible();
    await expect(previewDialog.getByText('截至该回复无工具调用')).toHaveCount(0);
    await expect(previewDialog.getByRole('paragraph').filter({ hasText: NOTE })).toHaveText(NOTE);
    await assertProjectionPrivate(previewDialog, preview);

    // 预览不会授权管理者；在明确确认之前通过真实刷新再次核对。
    await managerMaterials.getByRole('button', { name: '刷新材料列表' }).click();
    await expect(managerMaterials).toHaveAttribute('aria-busy', 'false');
    await expect(managerMaterials.getByText('暂无可管理的共享材料')).toBeVisible();
    const sharedResponse = materialResponse(page, `/materials/${preview.id}/share`, 'POST');
    await previewDialog.getByRole('button', { name: '确认分享给该员工管理者' }).click();
    expect((await sharedResponse).status()).toBe(200);
    await expect(previewDialog).toHaveCount(0);
    await expect(ownerMaterials.getByRole('button', { name: '撤回分享' })).toBeVisible();

    await managerMaterials.getByRole('button', { name: '刷新材料列表' }).click();
    const detailResponse = materialResponse(manager, `/materials/${preview.id}`, 'GET');
    await managerMaterials.getByRole('button', { name: '查看共享材料' }).click();
    const materialRead = await detailResponse;
    expect(materialRead.status()).toBe(200);
    const shared = await materialRead.json() as FeedbackMaterialRead;
    expect(shared.id).toBe(preview.id);
    expect(shared.material_checksum).toBe(preview.material_checksum);
    expect(shared.feedback_revision).toBe(preview.feedback_revision);
    await expect(managerMaterials.getByText(NOTE, { exact: true })).toBeVisible();
    await assertProjectionPrivate(manager.locator('body'), shared);

    // 当前目标切换销毁旧投影，另一员工不会借现有使用权获得管理材料。
    await manager.goto('/enterprise/feedback?agent_id=agent_sd7_other');
    await expect(manager.getByRole('region', { name: '反馈改进材料' })).toBeVisible();
    await expect(manager.getByRole('region', { name: '反馈改进材料' })).toHaveAttribute('aria-busy', 'false');
    await expect(manager.getByText(NOTE, { exact: true })).toHaveCount(0);
    await expect(manager.getByRole('button', { name: '查看共享材料' })).toHaveCount(0);
    await assertProjectionPrivate(manager.locator('body'));
    await manager.goto(FEEDBACK_PAGE);
    await manager.getByRole('button', { name: '查看共享材料' }).click();
    await expect(manager.getByText(NOTE, { exact: true })).toBeVisible();

    // 另一个 actor 即使知道会话 ID 也没有本人会话全文权限；这里只读旁证。
    const privateReadStatus = await manager.evaluate(async (sessionId) => {
      const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
      const result = await fetch(`/api/enterprise/feedback/sessions/${sessionId}?tenant_id=tenant_demo`, {
        headers: { Authorization: `Bearer ${auth.token || ''}` },
      });
      return result.status;
    }, SESSION_ID);
    expect([403, 404]).toContain(privateReadStatus);

    const withdrawResponse = materialResponse(page, `/materials/${preview.id}/withdraw`, 'POST');
    await ownerMaterials.getByRole('button', { name: '撤回分享' }).click();
    expect((await withdrawResponse).status()).toBe(200);
    await expect(ownerMaterials.getByRole('button', { name: '撤回分享' })).toHaveCount(0);
    const staleResponse = materialResponse(manager, `/materials/${preview.id}`, 'GET');
    await managerMaterials.getByRole('button', { name: '刷新材料详情' }).click();
    const staleStatus = (await staleResponse).status();
    expect([403, 404, 409]).toContain(staleStatus);
    await expect(managerMaterials.getByRole('alert')).toHaveText('材料已失效或权限已变化，请重新加载');
    await expect(managerMaterials.getByText(NOTE, { exact: true })).toHaveCount(0);
    await managerMaterials.getByRole('button', { name: '刷新材料列表' }).click();
    await expect(managerMaterials).toHaveAttribute('aria-busy', 'false');
    await expect(managerMaterials.getByRole('button', { name: '查看共享材料' })).toHaveCount(0);
    await manager.reload();
    await expect(manager.getByRole('region', { name: '反馈改进材料' })).toHaveAttribute('aria-busy', 'false');
    await expect(manager.getByRole('region', { name: '反馈改进材料' }).getByText('暂无可管理的共享材料')).toBeVisible();
    await assertProjectionPrivate(manager.locator('body'));
    await info.attach('sd7-f1-safe-journey', {
      body: Buffer.from(JSON.stringify({
        feedback_id: FEEDBACK_ID, material_id: preview.id,
        feedback_revision: preview.feedback_revision, material_checksum: preview.material_checksum,
        exact_target_agent: preview.target_agent_id === AGENT_ID, source_status: preview.material.source.status,
        preview_requires_confirmation: true, distinct_actors: true, private_session_read_status: privateReadStatus,
        stale_material_read_status: staleStatus, sensitive_markers_absent: true, withdrawal_refresh_closed: true,
        unrelated_scope_cleared_old_material: true,
        fixture_does_not_seed_execution_or_share: true,
        operations_known: preview.material.facts.operations_known,
      })), contentType: 'application/json',
    });
  } finally {
    await managerContext.close();
  }
});
