/** SD7限定收尾：真实页面/HTTP/双库/进程重启；只有末端SDK是固定协议替身。 */
import { expect, test, type Page } from '@playwright/test';
import { readFileSync, existsSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { FeedbackMaterialRead } from '../src/types/feedback';
import type { SkillCandidateRead, SkillCandidateReceipt } from '../src/types/skill-candidate';
import type { SkillRead } from '../src/types';

const AGENT = 'agent_sd7_feedback';
const SKILL = 'skill_sd7_feedback_wait';
const FEEDBACK = `/enterprise/feedback?agent_id=${AGENT}`;
const control = () => {
  const value = process.env.FULLSTACK_SD6B_CONTROL_DIR;
  if (!value) throw new Error('SD7 owned control directory required');
  return value;
};
const pause = (kind: string, name: string) => writeFileSync(join(control(), 'sdk-pause.json'), JSON.stringify({ kind, case: name }));
const release = (name: string) => writeFileSync(join(control(), 'sdk-release-' + name), 'release');
const entered = async (name: string) => expect.poll(() => existsSync(join(control(), 'sdk-entered-' + name)), { timeout: 30_000 }).toBe(true);
const calls = (name: string) => readFileSync(join(control(), 'sdk-observations.safe.jsonl'), 'utf8')
  .split('\n').filter(Boolean).map((line) => JSON.parse(line) as { case: string }).filter((row) => row.case === name).length;

async function login(page: Page, username: string, env: string) {
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
  await expect(page.getByRole('region', { name: '反馈改进材料' })).toBeVisible();
}

async function request(page: Page, path: string, method = 'GET', body?: unknown) {
  // R1使用当前页面本人认证走实际原API；此帮助器不用于替代R2分享/撤回/发布界面。
  return page.evaluate(async ({ path, method, body }) => {
    const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
    const response = await fetch(path, { method, headers: { Authorization: `Bearer ${auth.token || ''}`,
      'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: response.status, value: await response.json() as unknown };
  }, { path, method, body });
}

const responseFor = (page: Page, suffix: string, method: string) => page.waitForResponse((r) =>
  r.request().method() === method && new URL(r.url()).pathname.endsWith(suffix));

test('R1 real owner API commits rating/delete/reanalysis before bad JSON; old SDK stops and new task succeeds', async ({ page }, info) => {
  test.skip(process.env.FULLSTACK_SD7_REPAIR !== '1', 'Requires owned repair protocol fixture');
  test.setTimeout(180_000);
  await login(page, 'sd7-owner', 'FULLSTACK_SD7_OWNER_PASSWORD');
  const ratingPath = '/api/chat/messages/msg_sd7_feedback_reply/feedback';
  let feedbackId = 'fb_sd7_feedback_legacy';
  for (const change of ['rating', 'delete', 'reanalyze']) {
    pause('feedback', change);
    expect((await request(page, `/api/enterprise/feedback/${feedbackId}/reanalyze?tenant_id=tenant_demo`, 'POST')).status).toBe(200);
    await entered(change);
    if (change === 'rating') {
      expect((await request(page, ratingPath, 'POST', { tenant_id: 'tenant_demo', rating: 'up' })).status).toBe(200);
    } else if (change === 'delete') {
      expect((await request(page, ratingPath + '?tenant_id=tenant_demo', 'DELETE')).status).toBe(200);
      expect((await request(page, '/api/enterprise/feedback/sessions/session_sd7_feedback_legacy?tenant_id=tenant_demo')).status).toBe(200);
      const recreated = await request(page, ratingPath, 'POST', { tenant_id: 'tenant_demo', rating: 'down' });
      expect(recreated.status).toBe(200);
      feedbackId = (recreated.value as { id: string }).id;
      expect(feedbackId).not.toBe('fb_sd7_feedback_legacy');
    } else {
      expect((await request(page, `/api/enterprise/feedback/${feedbackId}/reanalyze?tenant_id=tenant_demo`, 'POST')).status).toBe(200);
    }
    release(change);
    await expect.poll(async () => {
      const response = await request(page, '/api/enterprise/feedback/sessions/session_sd7_feedback_legacy?tenant_id=tenant_demo');
      const detail = response.value as { feedback: Array<{ id: string; analysis: { status: string } }> };
      return detail.feedback.find((row) => row.id === feedbackId)?.analysis.status;
    }, { timeout: 30_000 }).toBe('analyzed');
    // 完成事件证明旧分析已经退出，不能仅在首次SDK未释放时数调用。
    await expect.poll(async () => {
      const response = await request(page, '/api/chat/sessions/session_sd7_feedback_legacy/events?tenant_id=tenant_demo');
      const value = response.value;
      return Array.isArray(value) ? value.filter((event: { event_type: string }) => event.event_type === 'feedback_analysis_error').length : 0;
    }, { timeout: 30_000 }).toBeGreaterThanOrEqual(['rating', 'delete', 'reanalyze'].indexOf(change) + 1);
    expect(calls(change)).toBe(1);
    await info.attach('sd7-repair-safe', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
      case: change, old_sdk_calls: 1, new_task_analyzed: true, real_provider_quality: false,
    })) });
  }
});

test('R2 real GET200 then owner withdrawal rejects stream and clears; legal network/restart editing and published receipt survive', async ({ page, browser }, info) => {
  test.skip(process.env.FULLSTACK_SD7_REPAIR !== '1', 'Requires owned repair protocol fixture');
  test.setTimeout(240_000);
  const context = await browser.newContext({ baseURL: info.project.use.baseURL });
  const manager = await context.newPage();
  manager.setDefaultTimeout(30_000);
  let allowStream: (() => void) | undefined;
  let allowPreflight: (() => void) | undefined;
  let pausedStream = false;
  try {
    await login(page, 'sd7-owner', 'FULLSTACK_SD7_OWNER_PASSWORD');
    await login(manager, 'sd7-manager', 'FULLSTACK_SD7_MANAGER_PASSWORD');
    const head = async () => (await request(manager, `/api/enterprise/skills/${SKILL}?tenant_id=tenant_demo&agent_id=${AGENT}`)).value as SkillRead;
    const original = await head();
    await page.getByRole('row').filter({ hasText: 'SD7 F2 Original Wait' }).getByRole('button', { name: '查看', exact: true }).click();
    await page.getByRole('dialog', { name: '对话日志详情' }).getByRole('button', { name: '预览改进材料' }).click();
    const preview = page.getByRole('dialog', { name: '预览改进材料' });
    await preview.getByRole('textbox', { name: '用户说明' }).fill('SD7_REPAIR_PRIVATE_NOTE');
    const prepared = responseFor(page, '/fb_sd7_f2_wait/materials/preview', 'POST');
    await preview.getByRole('button', { name: '生成脱敏预览' }).click();
    const material = await (await prepared).json() as FeedbackMaterialRead;
    expect(material.material.source.skill_version_id).toBe(original.publication_id);
    await preview.getByRole('button', { name: '确认分享给该员工管理者' }).click();
    await expect(preview).toHaveCount(0);
    await manager.getByRole('button', { name: '刷新材料列表' }).click();
    await manager.getByRole('button', { name: '查看共享材料' }).click();
    const opened = responseFor(manager, '/feedback-candidates', 'POST');
    await manager.getByRole('button', { name: '在当前编辑器改进 SOP' }).click();
    let candidate = await (await opened).json() as SkillCandidateRead;
    const review = manager.getByRole('region', { name: '反馈候选审核' });
    const title = manager.getByRole('textbox', { name: 'SOP名称', exact: true });
    const pointer = `skill-feedback-job:tenant_demo:${candidate.id}`;
    await expect(title).toHaveValue(original.name);

    // 真实网络请求中断；后端仍在首SDK，reload后的GET200与stream均走真实服务。
    pause('candidate', 'network');
    await manager.getByPlaceholder('说明你要如何改写右侧选中的部分').fill('Update the description for protocol reconnect.');
    await manager.getByRole('button', { name: '发送', exact: true }).click();
    await entered('network');
    await expect.poll(() => manager.evaluate((key) => sessionStorage.getItem(key), pointer)).not.toBeNull();
    const networkPointer = await manager.evaluate((key) => sessionStorage.getItem(key), pointer);
    const oldJob = (JSON.parse(networkPointer || '{}') as { job_id: string }).job_id;
    const editURL = manager.url();
    // navigation终止真实在途streamPost，不用假HTTP响应。
    await manager.goto(FEEDBACK);
    await manager.goto(editURL);
    await expect(title).toHaveValue(original.name);
    release('network');
    await expect(manager.getByRole('button', { name: '停止', exact: true })).toHaveCount(0);
    await expect.poll(() => manager.evaluate((key) => sessionStorage.getItem(key), pointer)).toBeNull();
    await expect(title).toHaveValue(original.name);
    expect(calls('network')).toBeGreaterThanOrEqual(2);

    // 普通合法编辑及旧job真实重启丢失：有效候选保留未保存内容。
    const saved = responseFor(manager, '/feedback-candidates/' + candidate.id, 'PUT');
    await title.fill('SD7_REPAIR_SAVED_CANDIDATE');
    await review.getByRole('button', { name: '仅保存隔离候选' }).click();
    candidate = await (await saved).json() as SkillCandidateRead;
    expect((await head()).publication_id).toBe(original.publication_id);
    // 当前有效服务器job定位，重启之后真正查不到；只恢复job ID，不存正文。
    await manager.evaluate(({ pointer, oldJob }) => sessionStorage.setItem(pointer, JSON.stringify({ job_id: oldJob })), { pointer, oldJob });
    writeFileSync(join(control(), 'restart.request'), 'restart');
    await expect.poll(() => existsSync(join(control(), 'restart.done')), { timeout: 60_000 }).toBe(true);
    await expect.poll(async () => { try { return (await manager.request.get('/api/health')).status(); } catch { return 0; } },
      { timeout: 60_000 }).toBe(200);
    const missing = manager.waitForResponse((r) => r.url().includes('/skills/jobs/' + oldJob) && r.request().method() === 'GET');
    await manager.reload();
    expect((await missing).status()).toBe(404);
    await expect.poll(() => manager.evaluate((key) => sessionStorage.getItem(key), pointer)).toBeNull();
    await expect(title).toHaveValue('SD7_REPAIR_SAVED_CANDIDATE');
    await title.fill('SD7_REPAIR_UNSAVED_AFTER_RESTART');
    await expect(review.getByRole('button', { name: '仅保存隔离候选' })).toBeEnabled();
    // 用已有人工发布链验证最小回执刷新，不声称业务质量。
    const savedAgain = responseFor(manager, '/feedback-candidates/' + candidate.id, 'PUT');
    await review.getByRole('button', { name: '仅保存隔离候选' }).click();
    candidate = await (await savedAgain).json() as SkillCandidateRead;
    await review.getByRole('button', { name: '人工确认发布候选' }).click();
    const confirmation = manager.getByRole('dialog', { name: '确认沿现有发布链发布候选' });
    const published = responseFor(manager, '/feedback-candidates/' + candidate.id + '/publish', 'POST');
    await confirmation.getByRole('button', { name: '确认发布', exact: true }).click();
    const receipt = await (await published).json() as SkillCandidateReceipt;
    await manager.reload();
    await expect(review.getByRole('status')).toContainText(receipt.skill_version_id);
    await expect(title).toHaveCount(0);
    await expect(review.getByRole('button', { name: '人工确认发布候选' })).toHaveCount(0);

    // 当前基础上创建另一候选，保存差异后打开发布弹窗，再使恢复stream入口明确失效。
    await manager.goto(FEEDBACK);
    await manager.getByRole('button', { name: '查看共享材料' }).click();
    const reopened = responseFor(manager, '/feedback-candidates', 'POST');
    await manager.getByRole('button', { name: '在当前编辑器改进 SOP' }).click();
    candidate = await (await reopened).json() as SkillCandidateRead;
    const stalePointer = `skill-feedback-job:tenant_demo:${candidate.id}`;
    pause('candidate', 'candidate');
    await manager.getByPlaceholder('说明你要如何改写右侧选中的部分').fill('Protocol invalidation candidate.');
    await manager.getByRole('button', { name: '发送', exact: true }).click();
    await entered('candidate');
    await expect.poll(() => manager.evaluate((key) => sessionStorage.getItem(key), stalePointer)).not.toBeNull();
    const staleJob = (JSON.parse(await manager.evaluate((key) => sessionStorage.getItem(key), stalePointer) || '{}') as { job_id: string }).job_id;
    let get200 = false;
    let preflightPaused = false;
    manager.on('response', (r) => { if (r.request().method() === 'GET' && /\/skills\/jobs\/[^/?]+$/.test(new URL(r.url()).pathname) && r.status() === 200) get200 = true; });
    await manager.route('**/api/enterprise/skills/jobs/*/stream?*', async (route) => {
      pausedStream = true;
      await new Promise<void>((resolve) => { allowStream = resolve; });
      await route.continue(); // 必须真实服务端拒绝，禁止fulfill伪造状态。
    });
    await manager.route('**/api/enterprise/skills/jobs/*', async (route) => {
      preflightPaused = true;
      await new Promise<void>((resolve) => { allowPreflight = resolve; });
      await route.continue();
    });
    await manager.reload();
    await expect.poll(() => preflightPaused).toBe(true);
    await expect(title).toBeVisible();
    await title.fill('SD7_REPAIR_PRIVATE_UNSAVED');
    await review.getByText('查看授权材料及原轮来源').click();
    await expect(review.getByText('SD7_REPAIR_PRIVATE_NOTE', { exact: true })).toBeVisible();
    await manager.getByRole('button', { name: '保存隔离候选', exact: true }).click();
    await expect(manager.getByRole('dialog', { name: '保存隔离候选' })).toBeVisible();
    allowPreflight?.();
    await expect.poll(() => pausedStream && get200).toBe(true);
    pause('feedback', 'withdraw');
    expect((await request(page, '/api/enterprise/feedback/fb_sd7_f2_wait/reanalyze?tenant_id=tenant_demo', 'POST')).status).toBe(200);
    await entered('withdraw');
    const withdrawn = responseFor(page, '/' + material.id + '/withdraw', 'POST');
    await page.getByRole('button', { name: '撤回分享' }).click();
    expect((await withdrawn).status()).toBe(200);
    const refused = manager.waitForResponse((r) => r.url().includes('/skills/jobs/') && r.url().includes('/stream?'));
    allowStream?.();
    const denied = await refused;
    expect([403, 404, 409]).toContain(denied.status());
    expect((await denied.json() as { detail: string }).detail).toBe('FEEDBACK_CANDIDATE_UNAVAILABLE');
    await expect(review.getByRole('alert')).toContainText('候选已失效或权限已变化');
    await expect(title).toHaveCount(0);
    await expect(manager.getByRole('dialog')).toHaveCount(0);
    await expect(review.getByRole('button', { name: '人工确认发布候选' })).toHaveCount(0);
    await expect.poll(() => manager.evaluate((key) => sessionStorage.getItem(key), stalePointer)).toBeNull();
    expect(await manager.locator('body').textContent()).not.toContain('SD7_REPAIR_PRIVATE_NOTE');
    expect(await manager.locator('body').textContent()).not.toContain('SD7_REPAIR_PRIVATE_UNSAVED');
    release('withdraw'); release('candidate');
    await expect.poll(() => calls('withdraw')).toBe(2);
    await expect.poll(async () => {
      const response = await request(page, '/api/enterprise/feedback/sessions/session_sd7_f2_wait?tenant_id=tenant_demo');
      return (response.value as { feedback: Array<{ analysis: { status: string } }> }).feedback[0]?.analysis.status;
    }).toBe('analyzed');
    // 已失效候选job真正收敛失败（原缓存读取已拒绝），SDK不再派发，头不变。
    await expect.poll(() => {
      const file = join(control(), 'job-observations.safe.jsonl');
      if (!existsSync(file)) return '';
      return readFileSync(file, 'utf8').split('\n').filter(Boolean)
        .map((line) => JSON.parse(line) as { job_id: string; status: string })
        .find((row) => row.job_id === staleJob)?.status;
    }).toBe('failed');
    expect(calls('candidate')).toBe(1);
    expect((await head()).publication_id).toBe(receipt.skill_version_id);
    await manager.reload();
    await expect(review.getByRole('alert')).toBeVisible();
    await expect(title).toHaveCount(0);
    await info.attach('sd7-repair-safe', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
      case: 'resume', get200: true, real_stream_refusal: true, source_cleared: true,
      job_pointer_cleared: true, network_reconnected: true, restart_generic404_valid: true,
      published_receipt_safe: true, old_candidate_sdk_calls: 1, owner_withdraw_sdk_calls: 2,
      explicit_publications: 1, revoked_publications: 0, tool_calls: 0, real_provider_quality: false,
    })) });
  } finally { allowPreflight?.(); allowStream?.(); release('candidate'); release('withdraw'); release('network'); await context.close(); }
});
