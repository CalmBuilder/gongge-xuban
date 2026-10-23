/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : staffdeck-final-f2-jrn05.fullstack.e2e.ts
 * @CallChain  : 三身份Composer/管理页面 → 原SOP/DTA/权限 → 业务与私密状态旁证
 * @Description: H1双库使用同一真实完整旅程；权限拒绝不能由历史引用摘录替代。
 */
import { expect, test } from '@playwright/test';
import { facts, login, object, openCitation, request, send } from './final-f1-browser-helpers';
import { approveDynamic, pendingAttention, startWaitingDynamic } from './final-f1-dta-journey';
import { startWaitingSop } from './final-f1-sop-journey';
import { audit, authenticatedRead, downgradeManager, foreignLogin, identityFacts, idle,
  logout, switchStaff } from './final-f2-browser-helpers';
import type { FeedbackMaterialRead } from '../src/types/feedback';
import type { SkillCandidateRead } from '../src/types/skill-candidate';

test('JRN05 identities and revocation through original consumers and editor', async ({ page, browser }, info) => {
  test.setTimeout(480_000);
  let owner = page;
  const managerContext = await browser.newContext({ baseURL: info.project.use.baseURL });
  const foreignContext = await browser.newContext({ baseURL: info.project.use.baseURL });
  let manager = await managerContext.newPage();
  let foreign = await foreignContext.newPage();
  let completed = false;
  try {
    foreign = await foreignLogin(foreign);
    await login(manager, 'SOP', true);
    manager = await idle(manager);
    const initial = await identityFacts(foreign);
    expect((await facts(foreign)).instances).toHaveLength(0);
    await audit(info, 'identity-baseline', { passed: true, independent_context_count: 3,
      initial, real_provider_quality: false });

    const sop = await startWaitingSop(owner, info);
    const state = await facts(owner);
    const cited = object([...state.messages].reverse().find(row => row.session_id === sop.instance.session_id
      && Array.isArray(row.citations) && row.citations.length));
    const citation = object((cited.citations as unknown[])[0]);
    await openCitation(owner, info, String(cited.id));
    const source = `/api/enterprise/knowledge/documents/${String(citation.document_id)}/content`;
    const ownerSource = await authenticatedRead(owner, `${source}?tenant_id=tenant_demo&agent_id=agent_f1_sop`);
    expect(ownerSource.status).toBe(200);
    expect(object(ownerSource.body).content_md).toContain('750');
    const foreignSource = await authenticatedRead(foreign, `${source}?tenant_id=tenant_f2_foreign&agent_id=agent_f2_foreign`);
    expect(foreignSource.status).toBe(404);
    const forgedSource = await authenticatedRead(foreign, `${source}?tenant_id=tenant_demo&agent_id=agent_f1_sop`);
    expect(forgedSource.status).toBe(403);
    const unbound = await authenticatedRead(owner, `${source}?tenant_id=tenant_demo&agent_id=agent_f2_unbound`);
    expect(unbound.status).toBe(404);
    for (const actor of [manager, foreign]) {
      const tenant = actor === foreign ? 'tenant_f2_foreign' : 'tenant_demo';
      const messages = await authenticatedRead(actor, `/api/chat/sessions/${String(sop.instance.session_id)}/messages?tenant_id=${tenant}`);
      expect(messages.status).toBe(404);
      const privateRead = await authenticatedRead(actor, `/api/enterprise/feedback/sessions/${String(sop.instance.session_id)}?tenant_id=${tenant}`);
      expect(privateRead.status).toBe(404);
      expect(JSON.stringify(messages.body)).not.toContain('F2_OWNER_PRIVATE_CANARY');
    }
    await audit(info, 'sop-current-authorization', { passed: true, source_document_id: citation.document_id,
      source_version_id: citation.version_id, owner_read_status: ownerSource.status,
      foreign_read_status: foreignSource.status, forged_tenant_status: forgedSource.status,
      unbound_agent_status: unbound.status, historical_excerpt_is_not_authorization: true });
    await owner.goto(`/workspace/chat/${String(sop.instance.session_id)}`);
    await expect(owner.getByPlaceholder('输入消息，按 Enter 发送...')).toBeVisible();
    expect(object(await request(owner, `/__e2e/f1/provider/complete/${String(sop.operation.external_task_id)}`, 'POST')).http_status).toBe(202);
    await expect.poll(async () => (await facts(owner)).instances.find(row => row.id === sop.instance.id)?.status,
      { timeout: 60_000 }).toBe('succeeded');
    const sopFinal = await facts(owner);
    expect(sopFinal.provider.submits).toHaveLength(1);
    expect(sopFinal.provider.finishes).toHaveLength(1);
    expect(sopFinal.provider.submits[0]?.request).toEqual({ product_name: 'ALPHA91', quantity: 3,
      customer: 'CYGNUS77', due_date: '2026-10-04 09:00' });
    await owner.reload();
    const receipt = await owner.context().newPage();
    await receipt.goto(`/__e2e/f1/provider/receipt/${String(sop.operation.external_task_id)}`);
    await expect(receipt.locator('#receipt-id')).toHaveText(String(sop.remote.receipt_id));
    await expect(receipt.getByRole('table')).toContainText('CYGNUS77');
    await receipt.close();
    const rate = owner.waitForResponse(row => row.request().method() === 'POST'
      && /\/api\/chat\/messages\/[^/]+\/feedback$/.test(new URL(row.url()).pathname));
    await owner.getByRole('button', { name: '点踩', exact: true }).last().click();
    const feedback = object(await (await rate).json());
    expect(feedback.session_id).toBe(sop.instance.session_id);
    await audit(info, 'sop-business-and-real-feedback', { passed: true, feedback_id: feedback.id,
      source_message_id: feedback.message_id, instance_id: sop.instance.id, ledger: sopFinal });

    // 同一所有者重新登录并经普通DTA创建另一任务，SOP原记录保持。
    await logout(owner);
    owner = await idle(owner);
    await audit(info, 'owner-logged-out', { passed: true });
    const dta = await startWaitingDynamic(owner, browser, info);
    owner = dta.page;
    expect(object(await request(owner, `/__e2e/f1/provider/complete/${String(dta.operation.external_task_id)}`, 'POST')).http_status).toBe(202);
    const attention = await pendingAttention(owner, String(dta.instance.id), 'tool_approval');
    owner = await approveDynamic(owner, browser, info, attention, [String(dta.remote.receipt_id)]);
    await expect.poll(async () => (await facts(owner)).instances.find(row => row.id === dta.instance.id)?.status,
      { timeout: 60_000 }).toBe('succeeded');
    const dtaFinal = await facts(owner);
    const result = object(dtaFinal.results.find(row => row.execution_id === dta.instance.id));
    expect(result).toMatchObject({ status: 'verified', verification_passed: true, contains_current_receipt: true });
    expect(dtaFinal.provider.submits).toHaveLength(2);
    expect(dtaFinal.provider.finishes).toHaveLength(2);
    const resultRead = await authenticatedRead(owner, `/api/executions/${String(dta.instance.id)}/result?tenant_id=tenant_demo`);
    expect(resultRead.status).toBe(200);
    expect(object(resultRead.body).id).toBe(result.id);
    for (const instance of [sop.instance, dta.instance]) {
      for (const suffix of ['', '/result']) {
        const denied = await authenticatedRead(foreign, `/api/executions/${String(instance.id)}${suffix}?tenant_id=tenant_f2_foreign`);
        expect(denied.status).toBe(404);
        expect(JSON.stringify(denied.body)).not.toContain('CYGNUS77');
      }
    }
    const answer = object(dtaFinal.messages.find(row => row.session_id === dta.instance.session_id
      && object(row.metadata).result_id === result.id));
    await owner.reload();
    await openCitation(owner, info, String(answer.id));
    await audit(info, 'dta-real-consumer-and-cross-result', { passed: true, result_id: result.id,
      instance_id: dta.instance.id, ledger: dtaFinal });

    owner = await idle(owner);
    for (const instance of [sop.instance, dta.instance]) {
      const refused = foreign.waitForResponse(row => new URL(row.url()).pathname
        === `/api/chat/sessions/${String(instance.session_id)}/messages`);
      await foreign.goto(`/workspace/chat/${String(instance.session_id)}`);
      expect((await refused).status()).toBe(404);
      expect(await foreign.locator('body').innerText()).not.toContain('F2_OWNER_PRIVATE_CANARY');
      expect(await foreign.locator('body').innerText()).not.toContain(String(sop.remote.receipt_id));
      expect(await foreign.locator('body').innerText()).not.toContain(String(dta.remote.receipt_id));
    }
    foreign = await idle(foreign);
    const afterDeniedPages = await identityFacts(foreign);
    expect(afterDeniedPages.all_instances).toHaveLength(2);
    expect(afterDeniedPages.all_instances.every(row => row.initiator_user_id === 'admin')).toBe(true);
    await audit(info, 'foreign-real-pages-refuse-both-session-identities', { passed: true,
      identity_ledger: afterDeniedPages, unauthorized_business_instances: 0 });

    await owner.goto('/enterprise/feedback');
    await switchStaff(owner, 'F1 SOP员工');
    await owner.goto('/enterprise/feedback');
    await owner.getByRole('button', { name: '查看', exact: true }).last().click();
    const logs = owner.getByRole('dialog', { name: '对话日志详情' });
    await logs.getByRole('button', { name: '预览改进材料', exact: true }).click();
    const preview = owner.getByRole('dialog', { name: '预览改进材料' });
    await preview.getByRole('textbox', { name: '用户说明' }).fill('F2 review delivery facts and preserve the original receipt.');
    const prepared = owner.waitForResponse(row => row.request().method() === 'POST'
      && new URL(row.url()).pathname.endsWith(`/${String(feedback.id)}/materials/preview`));
    await preview.getByRole('button', { name: '生成脱敏预览' }).click();
    const material = await (await prepared).json() as FeedbackMaterialRead;
    expect(material.material.source.status).toBe('verified');
    expect(material.material.source.execution_id).toBe(sop.instance.id);
    expect(material.material.source.skill_version_id).toBe(sop.instance.skill_version_id);
    expect(JSON.stringify(material)).not.toContain('F2_OWNER_PRIVATE_CANARY');
    const preShare = await authenticatedRead(manager, `/api/enterprise/feedback/materials/${material.id}?tenant_id=tenant_demo`);
    expect(preShare.status).toBe(404);
    await preview.getByRole('button', { name: '确认分享给该员工管理者' }).click();
    await expect(preview).toHaveCount(0);
    if (await logs.isVisible()) await logs.getByRole('button', { name: 'Close', exact: true }).click();
    owner = await idle(owner);
    await manager.goto('/enterprise/feedback');
    const managed = manager.getByRole('region', { name: '反馈改进材料' });
    await managed.getByRole('button', { name: '查看共享材料' }).click();
    await expect(managed).toContainText(String(sop.instance.skill_version_id));
    expect(await manager.locator('body').innerText()).not.toContain('F2_OWNER_PRIVATE_CANARY');
    const opened = manager.waitForResponse(row => row.request().method() === 'POST'
      && new URL(row.url()).pathname.endsWith('/feedback-candidates'));
    await managed.getByRole('button', { name: '在当前编辑器改进 SOP' }).click();
    const candidate = await (await opened).json() as SkillCandidateRead;
    expect(candidate.base_skill_version_id).toBe(sop.instance.skill_version_id);
    const review = manager.getByRole('region', { name: '反馈候选审核' });
    await expect(review).toBeVisible();
    const name = manager.getByRole('textbox', { name: 'SOP名称', exact: true });
    await name.fill('F2_CANDIDATE_PRIVATE_CANARY');
    const saved = manager.waitForResponse(row => row.request().method() === 'PUT'
      && new URL(row.url()).pathname.endsWith(`/feedback-candidates/${candidate.id}`));
    await review.getByRole('button', { name: '仅保存隔离候选' }).click();
    expect((await saved).status()).toBe(200);
    const foreignCandidate = await authenticatedRead(foreign, `/api/enterprise/skills/feedback-candidates/${candidate.id}?tenant_id=tenant_f2_foreign`);
    expect(foreignCandidate.status).toBe(404);
    const foreignMaterial = await authenticatedRead(foreign, `/api/enterprise/feedback/materials/${material.id}?tenant_id=tenant_f2_foreign`);
    expect(foreignMaterial.status).toBe(404);
    expect(JSON.stringify(foreignCandidate.body)).not.toContain('F2_CANDIDATE_PRIVATE_CANARY');
    await audit(info, 'shared-minimum-and-isolated-candidate', { passed: true, material_id: material.id,
      candidate_id: candidate.id, base_version_id: candidate.base_skill_version_id,
      candidate_actor_id: 'f1_reviewer', foreign_candidate_status: foreignCandidate.status,
      foreign_material_status: foreignMaterial.status, pre_share_status: preShare.status,
      identity_ledger: await identityFacts(foreign) });
    await review.getByRole('button', { name: '人工确认发布候选' }).click();
    const publishDialog = manager.getByRole('dialog', { name: '确认沿现有发布链发布候选' });
    await expect(publishDialog).toBeVisible();
    await owner.goto('/enterprise/accounts');
    await downgradeManager(owner);
    owner = await idle(owner);
    const refusedPublication = manager.waitForResponse(row => row.request().method() === 'POST'
      && new URL(row.url()).pathname.endsWith(`/feedback-candidates/${candidate.id}/publish`));
    await publishDialog.getByRole('button', { name: '确认发布', exact: true }).click();
    expect((await refusedPublication).status()).toBe(403);
    await expect(publishDialog).toHaveCount(0);
    await expect(review.getByRole('alert')).toContainText('候选已失效或权限已变化');
    await expect(manager.getByRole('textbox', { name: 'SOP名称', exact: true })).toHaveCount(0);
    await expect(review.getByRole('button', { name: '人工确认发布候选' })).toHaveCount(0);
    expect(await manager.locator('body').innerText()).not.toContain('F2_CANDIDATE_PRIVATE_CANARY');
    await manager.reload();
    await expect(review.getByRole('alert')).toBeVisible();
    await manager.goto('/enterprise/feedback');
    await expect(manager.getByRole('heading', { name: '还没有数字员工', exact: true })).toBeVisible();
    await expect(manager.getByRole('button', { name: '查看共享材料' })).toHaveCount(0);
    expect(await manager.locator('body').innerText()).not.toContain('F2_CANDIDATE_PRIVATE_CANARY');
    const lostMaterial = await authenticatedRead(manager,
      `/api/enterprise/feedback/materials/${material.id}?tenant_id=tenant_demo`);
    expect(lostMaterial.status).toBe(403);
    for (const instance of [sop.instance, dta.instance]) {
      const denied = await authenticatedRead(manager, `/api/executions/${String(instance.id)}?tenant_id=tenant_demo`);
      expect(denied.status).toBe(403);
    }
    const privateMemories = await authenticatedRead(manager,
      '/api/enterprise/memories?tenant_id=tenant_demo&agent_id=agent_f1_sop&user_id=admin');
    expect(privateMemories.status).toBe(200);
    expect(privateMemories.body).toEqual([]);
    await audit(info, 'manager-revocation-clears-private-editor', { passed: true,
      candidate_id: candidate.id, identity_ledger: await identityFacts(manager) });
    manager = await idle(manager);

    await owner.goto('/enterprise/feedback');
    await owner.getByRole('button', { name: '撤回分享', exact: true }).click();
    const withdrawn = await authenticatedRead(manager, `/api/enterprise/feedback/materials/${material.id}?tenant_id=tenant_demo`);
    expect(withdrawn.status).toBe(403);
    const withdrawnOwner = await authenticatedRead(owner,
      `/api/enterprise/feedback/materials/${material.id}?tenant_id=tenant_demo`);
    expect(withdrawnOwner.status).toBe(409);
    expect(object(withdrawnOwner.body).detail).toBe('FEEDBACK_MATERIAL_INVALID');
    await switchStaff(owner, 'F1 DTA员工');
    await owner.goto('/enterprise/feedback');
    await expect(owner.getByRole('button', { name: '撤回分享', exact: true })).toHaveCount(0);
    await switchStaff(owner, 'F1 SOP员工');
    const beforeClear = await identityFacts(owner);
    await owner.goto('/enterprise/memories');
    owner.once('dialog', dialog => dialog.accept());
    const cleared = owner.waitForResponse(row => row.request().method() === 'DELETE'
      && new URL(row.url()).pathname.endsWith('/memories/me'));
    await owner.getByRole('button', { name: '清空我的记忆', exact: true }).click();
    expect((await cleared).status()).toBe(200);
    const afterClear = await identityFacts(owner);
    expect(afterClear.personal_memories.filter(row => row.user_id === 'admin' && row.agent_id === 'agent_f1_sop')).toHaveLength(0);
    for (const [user, agent] of [['f1_reviewer', 'agent_f1_sop'], ['admin', 'agent_f1_dta'], ['f2_foreign', 'agent_f2_foreign']]) {
      expect(afterClear.personal_memories.filter(row => row.user_id === user && row.agent_id === agent))
        .toEqual(beforeClear.personal_memories.filter(row => row.user_id === user && row.agent_id === agent));
    }
    expect(afterClear.memory_scopes.some(row => row.user_id === 'admin' && row.agent_scope_key === 'agent_f1_sop'
      && Number(row.clear_generation) > 0)).toBe(true);
    expect(afterClear.skill_versions).toEqual(initial.skill_versions);
    const final = await facts(owner);
    expect(final.instances).toHaveLength(2);
    expect(final.instances.every(row => row.initiator_user_id === 'admin' && row.status === 'succeeded')).toBe(true);
    expect(final.provider.submits).toHaveLength(2);
    expect(final.provider.finishes).toHaveLength(2);
    await logout(owner);
    owner = await idle(owner);
    await login(owner, 'DTA');
    const engine = owner.getByRole('button', { name: '选择 DynamicTaskAgent 复杂任务引擎', exact: true });
    if (await engine.getAttribute('aria-pressed') === 'true') await engine.click();
    await expect(engine).toHaveAttribute('aria-pressed', 'false');
    const lastMessages = (await facts(owner)).messages.map(row => row.id);
    await send(owner, '请记住我的稳定服务偏好：F2_POST_REVOCATION_LEGAL_OWNER_FACT');
    await expect.poll(async () => (await facts(owner)).messages.filter(row => row.role === 'assistant'
      && !lastMessages.includes(row.id)).length, { timeout: 30_000 }).toBe(1);
    await audit(info, 'clear-isolation-withdrawal-and-legal-relogin', { passed: true,
      before_clear: beforeClear, after_clear: afterClear, final, extra_publications: 0,
      unauthorized_business_instances: 0, unauthorized_provider_writes: 0 });
    completed = true;
  } finally {
    if (!owner.isClosed()) {
      try { await audit(info, 'terminal-observation', { passed: completed,
        ledger: await facts(owner), identity_ledger: await identityFacts(owner) }); }
      catch (error) { await audit(info, 'terminal-observation-unavailable', { passed: false,
        error_type: error instanceof Error ? error.name : 'unknown' }); }
    }
    await managerContext.close().catch(error => {
      if (completed) throw error;
    });
    await foreignContext.close().catch(error => {
      if (completed) throw error;
    });
  }
});

test('JRN05 denied browser storage cannot restore private authentication', async ({ page }, info) => {
  await page.addInitScript(() => {
    Object.defineProperty(Storage.prototype, 'getItem', { configurable: true,
      value: () => { throw new DOMException('F2_STORAGE_DENIED', 'SecurityError'); } });
    Object.defineProperty(Storage.prototype, 'setItem', { configurable: true,
      value: () => { throw new DOMException('F2_STORAGE_DENIED', 'SecurityError'); } });
    Object.defineProperty(Storage.prototype, 'removeItem', { configurable: true,
      value: () => { throw new DOMException('F2_STORAGE_DENIED', 'SecurityError'); } });
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: /进入平台/ })).toBeVisible();
  expect(await page.locator('body').innerText()).not.toContain('F2_OWNER_PRIVATE_CANARY');
  const denied = await page.request.get('/api/auth/context');
  expect(denied.status()).toBe(401);
  await audit(info, 'storage-denied-fails-closed', { passed: true, anonymous_context_status: denied.status(),
    private_body_restored: false });
});
