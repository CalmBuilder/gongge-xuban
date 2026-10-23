/**
 * @Time       : 2026/10/03
 * @Author     : zhanglp8181
 * @File       : sd7-feedback.f2.fullstack.e2e.ts
 * @CallChain  : 原等待来源 → 本人分享 → 当前编辑页 → 隔离保存/刷新/人工确认 → 原发布身份
 * @Description: F2真实双库页面；来源由原Coordinator产生，所有改进操作走UI，非F3业务质量。
 */
import { expect, test, type Page } from '@playwright/test';
import type { FeedbackMaterialRead } from '../src/types/feedback';
import type { SkillCandidateRead, SkillCandidateReceipt } from '../src/types/skill-candidate';
import type { SkillRead } from '../src/types';

const AGENT = 'agent_sd7_feedback';
const SKILL = 'skill_sd7_feedback_wait';
const FEEDBACK = `/enterprise/feedback?agent_id=${AGENT}`;

async function login(page: Page, username: string, env: string) {
  /** 实际页面登录；合成凭据及认证状态始终不进入附件/日志归档。 */
  const password = process.env[env];
  if (!password) throw new Error('SD7 synthetic login required');
  await page.goto(FEEDBACK);
  await page.evaluate((id) => {
    localStorage.setItem('gongge_onboarding_guide_seen', '1');
    localStorage.setItem('gongge_quick_start_guide_seen', '1');
    localStorage.setItem('gongge_enterprise_agent_scope', id);
  }, AGENT);
  await page.getByRole('button', { name: '进入平台' }).click();
  await page.getByRole('textbox', { name: '账号' }).fill(username);
  await page.getByRole('textbox', { name: '密码', exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect.poll(() => page.evaluate(() => Boolean(localStorage.getItem('gongge_auth')))).toBe(true);
  await page.goto(FEEDBACK);
  await expect(page.getByRole('region', { name: '反馈改进材料' })).toBeVisible({ timeout: 30_000 });
}

async function head(page: Page): Promise<SkillRead> {
  /** 只读原管理API旁证；无写API替代分享、保存、拒绝或发布UI。 */
  return page.evaluate(async ({ agent, skill }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(`/api/enterprise/skills/${skill}?tenant_id=tenant_demo&agent_id=${agent}`,
      { headers: { Authorization: `Bearer ${auth.token || ''}` } });
    if (!response.ok) throw new Error('SD7 head read refused');
    return response.json();
  }, { agent: AGENT, skill: SKILL });
}

test('F2 FB-R02/03/04: current editor isolates save and refresh, explicit publication uses exact scope; withdrawal clears stale candidate', async ({ page, browser }, info) => {
  test.setTimeout(240_000);
  page.setDefaultTimeout(30_000);
  test.skip(process.env.FULLSTACK_SD7_F2 !== '1', 'Requires isolated F2 source fixture');
  const context = await browser.newContext({ baseURL: info.project.use.baseURL });
  const manager = await context.newPage();
  manager.setDefaultTimeout(30_000);
  const responseFor = (actor: Page, suffix: string, method: string) => actor.waitForResponse((r) =>
    r.request().method() === method && new URL(r.url()).pathname.endsWith(suffix), { timeout: 30_000 });
  const checkpoint = async (phase: string) => info.attach('sd7-f2-safe-journey', {
    contentType: 'application/json', body: Buffer.from(JSON.stringify({ phase })),
  });
  try {
    await login(page, 'sd7-owner', 'FULLSTACK_SD7_OWNER_PASSWORD');
    await login(manager, 'sd7-manager', 'FULLSTACK_SD7_MANAGER_PASSWORD');
    const original = await head(manager);
    expect(original.publication_scope_agent_id).toBe(AGENT);
    expect(original.publication_id).toBeTruthy();
    await page.getByRole('row').filter({ hasText: 'SD7 F2 Original Wait' }).getByRole('button', { name: '查看', exact: true }).click();
    await page.getByRole('dialog', { name: '对话日志详情' }).getByRole('button', { name: '预览改进材料', exact: true }).click();
    const preview = page.getByRole('dialog', { name: '预览改进材料' });
    await preview.getByRole('textbox', { name: '用户说明' }).fill('Explain the waiting requirement clearly.');
    const prepared = responseFor(page, '/fb_sd7_f2_wait/materials/preview', 'POST');
    await preview.getByRole('button', { name: '生成脱敏预览' }).click();
    const material = await (await prepared).json() as FeedbackMaterialRead;
    expect(material.material.source.status).toBe('verified');
    expect(material.material.source.skill_version_id).toBe(original.publication_id);
    expect(material.material.source.agent_id).toBe(AGENT);
    await preview.getByRole('button', { name: '确认分享给该员工管理者' }).click();
    await expect(preview).toHaveCount(0);
    await checkpoint('owner-share-completed');
    await manager.getByRole('button', { name: '刷新材料列表' }).click();
    await manager.getByRole('button', { name: '查看共享材料' }).click();
    const opened = responseFor(manager, '/feedback-candidates', 'POST');
    await manager.getByRole('button', { name: '在当前编辑器改进 SOP' }).click();
    let candidate = await (await opened).json() as SkillCandidateRead;
    await checkpoint('first-candidate-created');
    const review = manager.getByRole('region', { name: '反馈候选审核' });
    await expect(review).toBeVisible();
    await expect(manager.getByRole('textbox', { name: 'SOP名称', exact: true })).toHaveValue(original.name);
    await manager.getByRole('textbox', { name: 'SOP名称', exact: true }).fill('SD7 F2 Saved Isolated Candidate');
    const saved = responseFor(manager, '/feedback-candidates/' + candidate.id, 'PUT');
    await review.getByRole('button', { name: '仅保存隔离候选' }).click();
    candidate = await (await saved).json() as SkillCandidateRead;
    expect(candidate.changed_paths).toContain('/name');
    expect((await head(manager)).publication_id).toBe(original.publication_id);
    expect((await head(manager)).name).toBe(original.name);
    await manager.reload();
    await expect(manager.getByRole('textbox', { name: 'SOP名称', exact: true })).toHaveValue('SD7 F2 Saved Isolated Candidate');
    expect((await head(manager)).publication_id).toBe(original.publication_id);
    await review.getByText('查看当前原版与已保存候选差异').click();
    await expect(review.getByText('/name', { exact: true })).toBeVisible();
    await review.getByRole('button', { name: '人工确认发布候选' }).click();
    const confirmation = manager.getByRole('dialog', { name: '确认沿现有发布链发布候选' });
    const published = responseFor(manager, '/feedback-candidates/' + candidate.id + '/publish', 'POST');
    await confirmation.getByRole('button', { name: '确认发布', exact: true }).click();
    let receipt = await (await published).json() as SkillCandidateReceipt;
    let active = await head(manager);
    expect(receipt.scope_agent_id).toBe(AGENT);
    expect(active.publication_id).toBe(receipt.skill_version_id);
    expect(active.publication_checksum).toBe(receipt.definition_checksum);
    expect(active.publication_id).not.toBe(original.publication_id);
    expect(active.name).toBe('SD7 F2 Saved Isolated Candidate');
    await expect(review.getByRole('status')).toContainText(receipt.skill_version_id);
    await checkpoint('first-candidate-published');

    // 合法旧反馈与新基础：勾选确认必须先保存确认修订，再沿原链发布该修订。
    await manager.goto(FEEDBACK);
    await manager.getByRole('button', { name: '查看共享材料' }).click();
    const reopened = responseFor(manager, '/feedback-candidates', 'POST');
    await manager.getByRole('button', { name: '在当前编辑器改进 SOP' }).click();
    const different = await (await reopened).json() as SkillCandidateRead;
    await checkpoint('new-base-candidate-created');
    expect(different.base_difference).toBe(true);
    await manager.getByRole('textbox', { name: 'SOP名称', exact: true }).fill('SD7 F2 Confirmed Current Base');
    const secondSave = responseFor(manager, '/feedback-candidates/' + different.id, 'PUT');
    await review.getByRole('button', { name: '仅保存隔离候选' }).click();
    const savedDifferent = await (await secondSave).json() as SkillCandidateRead;
    await review.getByRole('button', { name: '人工确认发布候选' }).click();
    await expect(confirmation.getByRole('button', { name: '确认发布', exact: true })).toBeDisabled();
    await confirmation.getByRole('checkbox', { name: '我已核对原反馈版本与当前基础的差异' }).click();
    const confirmedSave = responseFor(manager, '/feedback-candidates/' + different.id, 'PUT');
    const secondPublication = responseFor(manager, '/feedback-candidates/' + different.id + '/publish', 'POST');
    await confirmation.getByRole('button', { name: '确认发布', exact: true }).click();
    const confirmed = await (await confirmedSave).json() as SkillCandidateRead;
    expect(confirmed.base_difference_confirmed).toBe(true);
    expect(confirmed.revision).toBe(savedDifferent.revision + 1);
    receipt = await (await secondPublication).json() as SkillCandidateReceipt;
    active = await head(manager);
    expect(active.publication_id).toBe(receipt.skill_version_id);
    expect(active.publication_checksum).toBe(receipt.definition_checksum);
    await checkpoint('base-difference-confirmation-published');

    // 拒绝已保存隔离候选保持合法活动版。
    await manager.goto(FEEDBACK);
    await manager.getByRole('button', { name: '查看共享材料' }).click();
    const rejectedOpen = responseFor(manager, '/feedback-candidates', 'POST');
    await manager.getByRole('button', { name: '在当前编辑器改进 SOP' }).click();
    const rejected = await (await rejectedOpen).json() as SkillCandidateRead;
    const rejectedResponse = responseFor(manager, '/feedback-candidates/' + rejected.id + '/reject', 'POST');
    await review.getByRole('button', { name: '拒绝此候选' }).click();
    expect((await (await rejectedResponse).json() as SkillCandidateRead).status).toBe('rejected');
    expect((await head(manager)).publication_id).toBe(receipt.skill_version_id);
    await checkpoint('rejected-candidate-keeps-head');

    // 撤回发生在保存弹窗打开期间；真实保存被拒绝后旧弹窗与私密diff都须清空。
    await manager.goto(FEEDBACK);
    await manager.getByRole('button', { name: '查看共享材料' }).click();
    const staleOpen = responseFor(manager, '/feedback-candidates', 'POST');
    await manager.getByRole('button', { name: '在当前编辑器改进 SOP' }).click();
    const stale = await (await staleOpen).json() as SkillCandidateRead;
    await expect(review).toBeVisible();
    await manager.getByRole('textbox', { name: 'SOP名称', exact: true }).fill('SD7 F2 SYNTHETIC_PRIVATE_UNPUBLISHED');
    await manager.getByRole('button', { name: '保存隔离候选', exact: true }).click();
    const saveDialog = manager.getByRole('dialog', { name: '保存隔离候选' });
    await expect(saveDialog).toBeVisible();
    await page.getByRole('button', { name: '撤回分享' }).click();
    const refused = responseFor(manager, '/feedback-candidates/' + stale.id, 'PUT');
    await saveDialog.getByRole('button', { name: '保存', exact: true }).click();
    expect((await refused).status()).toBe(409);
    await expect(saveDialog).toHaveCount(0);
    await expect(manager.getByText('SD7 F2 SYNTHETIC_PRIVATE_UNPUBLISHED', { exact: true })).toHaveCount(0);
    await expect(review.getByRole('alert')).toContainText('候选已失效或权限已变化');
    await expect(manager.getByRole('textbox', { name: 'SOP名称', exact: true })).toHaveCount(0);
    await expect(review.getByRole('button', { name: '人工确认发布候选' })).toHaveCount(0);
    expect((await head(manager)).publication_id).toBe(receipt.skill_version_id);
    await manager.reload();
    await expect(review.getByRole('alert')).toBeVisible();
    await review.getByRole('button', { name: '丢弃并加载当前范围原版' }).click();
    await expect(manager.getByRole('textbox', { name: 'SOP名称', exact: true })).toHaveValue(active.name);
    await info.attach('sd7-f2-safe-journey', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
      material_id: material.id, candidate_id: candidate.id, published_id: receipt.skill_version_id,
      original_id: original.publication_id, definition_checksum: receipt.definition_checksum,
      exact_scope: true, source_verified: true, save_and_refresh_isolated: true,
      explicit_publication: true, withdrawn_candidate_cleared: true, stale_base_difference: true,
      base_difference_confirmed_revision_published: true, rejected_preserves_head: true,
      invalidated_save_dialog_cleared: true,
      original_wait_from_real_coordinator: true, real_provider_quality: false,
    })) });
  } finally { await context.close(); }
});
