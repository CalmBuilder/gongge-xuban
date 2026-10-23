/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : staffdeck-final-f2-jrn06.fullstack.e2e.ts
 * @CallChain  : 正文/版本/Composer真实UI → 原worker与双Runtime → 精确来源及远端读回
 * @Description: H2多文档重建、CAS/真重启、合法旧绑定和记忆迟到/清空/真实消费者完整旅程。
 */
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { facts, login, object, request, type Row } from './final-f1-browser-helpers';
import { approveDynamic, pendingAttention, startWaitingDynamic } from './final-f1-dta-journey';
import { startWaitingSop } from './final-f1-sop-journey';
import { audit, authenticatedRead, idle, logout, switchStaff } from './final-f2-browser-helpers';
import { capture, captureState, clearMemory, consume, control, draft, h2Facts, H2_FACTS,
  manageMemory, NEW_BODY, NEW_MARKER, OLD_MARKER, openEditor, restart, revisionState,
  rows, saveBody, waitControl } from './final-f2-h2-helpers';

test.afterEach(async ({ context }, info) => {
  /** 失败原轮在自有服务清理之前读回账本；只读旁证不改变失败或业务状态。 */
  if (info.status === info.expectedStatus) return;
  try {
    const ledger = await context.request.get('/__e2e/f1/facts');
    const snapshot = await context.request.get('/__e2e/f2/h2/facts');
    await audit(info, 'h2-failed-case-original-ledger', {
      ledger: object(await ledger.json()), snapshot: object(await snapshot.json()) });
  } catch {
    await audit(info, 'h2-failed-case-observer-unavailable', { original_failure_retained: true });
  }
});

async function synchronize(page: Page, kind: 'SOP' | 'DTA'): Promise<void> {
  /** 当前员工的原菜单同步广场head，版本变动由原branch职责执行。 */
  await page.goto('/enterprise/knowledge');
  await switchStaff(page, `F1 ${kind}员工`);
  await page.goto('/enterprise/knowledge');
  await page.getByPlaceholder('搜索知识库名称、描述、状态或版本').fill('F1配送政策');
  await page.getByRole('row').filter({ hasText: 'F1配送政策' }).getByLabel('知识库操作').click();
  const response = page.waitForResponse(r => new URL(r.url()).pathname.endsWith('/sync-from-overall')
    && r.request().method() === 'POST');
  await page.getByRole('menuitem', { name: '从广场同步', exact: true }).click();
  expect((await response).status()).toBe(200);
}

async function citation(page: Page, info: TestInfo, execution: Row, marker: string, versionId: unknown): Promise<void> {
  /** 打开实际知识引用并另核当前保护正文API；历史excerpt不能替代当前授权。 */
  await page.goto(`/workspace/chat/${String(execution.session_id)}`);
  const state = await facts(page);
  const messages = state.messages.filter(r => r.session_id === execution.session_id
    && Array.isArray(r.citations) && r.citations.length > 0);
  expect(messages.length).toBeGreaterThan(0);
  const actual = object(rows(await h2Facts(page), 'citations').find(r => r.session_id === execution.session_id
    && (marker === OLD_MARKER ? r.old : r.new)));
  expect(actual.id).toBeTruthy();
  expect(actual.version_id).toBe(versionId);
  const source = await authenticatedRead(page, `/api/enterprise/knowledge/documents/${String(actual.document_id)}/content`
    + `?tenant_id=tenant_demo&agent_id=${String(execution.agent_id)}`);
  expect(source.status).toBe(200);
  expect(String(object(source.body).content_md)).toContain(marker);
  if (actual.kind === 'concept') {
    const concept = await authenticatedRead(page, `/api/enterprise/knowledge-bases/${String(actual.knowledge_base_id)}/okf/concepts/${String(actual.concept_path).split('/').map(encodeURIComponent).join('/')}`
      + `?tenant_id=tenant_demo&agent_id=${String(execution.agent_id)}`);
    expect(concept.status).toBe(200);
    expect(object(concept.body).id).toBe(actual.concept_id);
    expect(object(concept.body).knowledge_base_version_id).toBe(versionId);
    expect(String(object(concept.body).content_md)).toContain(marker);
  }
  await page.getByLabel('知识引用').first().getByRole('button', { name: /cygnus77/i }).first().click();
  await expect(page.getByRole('dialog')).toContainText(marker);
  await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
  await audit(info, 'h2-actual-version-citation', { execution_id: execution.id,
    kind: execution.kind, actual_citation: actual, current_source_status: source.status,
    expected_version_id: versionId, expected_registered_marker: marker, actual_popup_body: true });
}

async function finishBusiness(page: Page, info: TestInfo, instance: Row, operation: Row): Promise<void> {
  /** 独立远端真正完成原任务，原Signal/消费者必须结算，不seed数据库成功。 */
  const accepted = object(await request(page, `/__e2e/f1/provider/complete/${String(operation.external_task_id)}`, 'POST'));
  expect(accepted.http_status).toBe(202);
  await expect.poll(async () => (await facts(page)).instances.find(r => r.id === instance.id)?.status,
    { timeout: 60_000 }).toBe('succeeded');
  const state = await facts(page);
  const original = object(state.operations.find(r => r.id === operation.id));
  expect(original.status).toBe('succeeded');
  const remote = object(state.provider.submits.find(r => r.task_id === operation.external_task_id));
  expect(remote).toMatchObject({ effect_completed: true, post_count: 1 });
  expect(state.provider.finishes.filter(r => r.receipt_id === remote.receipt_id)).toHaveLength(1);
  const signals = state.signals.filter(r => r.execution_id === instance.id && r.signal_type === 'external_event'
    && r.status === 'consumed');
  expect(signals).toHaveLength(1);
  await audit(info, 'h2-sop-original-business-complete', { instance, operation: original, remote,
    signal_count: signals.length });
}

test('JRN06 knowledge rebuild, conflict, restart, exact binding and original SOP/DTA consumers', async ({ page, browser }, info) => {
  test.setTimeout(360_000);
  const oldSop = await startWaitingSop(page, info);
  const baseline = await h2Facts(page);
  const oldVersion = rows(baseline, 'versions').find(r => r.version === baseline.overall_version);
  expect(oldVersion).toBeTruthy();
  const oldAssets = rows(baseline, 'assets').filter(r => r.version_id === oldVersion?.id);
  expect(oldAssets.filter(r => r.kind === 'document')).toHaveLength(2);
  await citation(page, info, oldSop.instance, OLD_MARKER, oldVersion?.id);
  await logout(page);
  page = await idle(page);
  const completedOldDynamic = await startWaitingDynamic(page, browser, info);
  page = completedOldDynamic.page;
  expect(object(await request(page, `/__e2e/f1/provider/complete/${String(completedOldDynamic.operation.external_task_id)}`, 'POST')).http_status).toBe(202);
  const oldApproval = await pendingAttention(page, String(completedOldDynamic.instance.id), 'tool_approval');
  page = await idle(page);
  page = await approveDynamic(page, browser, info, oldApproval, [String(completedOldDynamic.remote.receipt_id)]);
  await expect.poll(async () => (await facts(page)).instances.find(r => r.id === completedOldDynamic.instance.id)?.status,
    { timeout: 60_000 }).toBe('succeeded');
  await citation(page, info, completedOldDynamic.instance, OLD_MARKER, oldVersion?.id);
  await logout(page);
  page = await idle(page);
  const oldDynamic = await startWaitingDynamic(page, browser, info);
  page = oldDynamic.page;
  await openEditor(page);
  const competingContext = page.context();
  let competing: Page | undefined;
  try {
    competing = await competingContext.newPage();
    await openEditor(competing);
    await competing.getByRole('textbox', { name: '文档正文' }).fill(NEW_BODY + '\n竞争者草稿：F2_CAS_LOSER。');
    await control('knowledge.pause');
    const accepted = await saveBody(page, NEW_BODY);
    await waitControl('knowledge.started');
    const queued = await revisionState(page, accepted.job_id, 'queued');
    const queuedSnapshot = await h2Facts(page);
    const persistentJob = object(rows(queuedSnapshot, 'revision_jobs').find(r => r.id === queued.id));
    expect(typeof persistentJob.candidate_version_id).toBe('string');
    expect(persistentJob.candidate_version_id).toBeTruthy();
    const hidden = await authenticatedRead(page, '/api/enterprise/knowledge/documents?tenant_id=tenant_demo&include_all_versions=true');
    expect(hidden.status).toBe(200);
    expect(Array.isArray(hidden.body)).toBe(true);
    const publicDocumentVersions = (hidden.body as unknown[]).map(object).map(r => r.knowledge_base_version_id);
    expect(publicDocumentVersions.includes(persistentJob.candidate_version_id)).toBe(false);
    const versionRead = await authenticatedRead(page,
      `/api/enterprise/knowledge-bases/${String(queuedSnapshot.knowledge_base_id)}/versions?tenant_id=tenant_demo`);
    expect(versionRead.status).toBe(200);
    expect(Array.isArray(versionRead.body)).toBe(true);
    const publicVersions = (versionRead.body as unknown[]).map(object).map(r => r.id);
    expect(publicVersions.includes(persistentJob.candidate_version_id)).toBe(false);
    expect((await h2Facts(page)).overall_version).toBe(baseline.overall_version);
    await audit(info, 'h2-queued-candidate-hidden', { job: queued, persistent_job: persistentJob,
      current_head_unchanged: true, public_document_versions: publicDocumentVersions,
      public_version_ids: publicVersions, public_candidate_documents: 0, snapshot: queuedSnapshot });
    // 竞争页面保留真实旧CAS，真重启不能关闭它或重建其草稿。
    page = await idle(page);
    await restart(page, info);
    expect(rows(await h2Facts(page), 'revision_jobs').find(r => r.id === accepted.job_id)?.status).toBe('queued');
    await control('knowledge.pause', true);
    const published = await revisionState(page, accepted.job_id, 'completed');
    const current = await h2Facts(page);
    const newAssets = rows(current, 'assets').filter(r => r.version_id === published.published_version_id);
    expect(rows(current, 'assets').filter(r => r.version_id === oldVersion?.id)).toEqual(oldAssets);
    expect(newAssets.filter(r => r.kind === 'document')).toHaveLength(2);
    expect(newAssets.some(r => r.old)).toBe(false);
    for (const kind of ['document', 'bucket', 'chunk', 'concept']) {
      expect(newAssets.some(r => r.kind === kind && r.new)).toBe(true);
      expect(newAssets.some(r => r.kind === kind && r.stable)).toBe(true);
    }
    await saveBody(competing, NEW_BODY + '\n竞争者草稿：F2_CAS_LOSER。', 409);
    await expect(competing.getByRole('textbox', { name: '文档正文' })).toHaveValue(NEW_BODY + '\n竞争者草稿：F2_CAS_LOSER。');
    expect((await h2Facts(page)).overall_version).toBe(current.overall_version);
    await audit(info, 'h2-immutable-whole-version-and-CAS', { old_version: oldVersion,
      old_assets: oldAssets, new_version_id: published.published_version_id, new_assets: newAssets,
      original_job: published, competing_write_status: 409, losing_draft_preserved: true,
      snapshot: current });
    competing = await idle(competing);
  } finally {
    if (competing && !competing.isClosed()) await competing.close();
  }
  await openEditor(page);
  await control('knowledge.fail-once');
  const failedBody = NEW_BODY + '\n失败重建草稿：F2_BUILD_FAILURE。';
  const failedAccepted = await saveBody(page, failedBody);
  const failed = await revisionState(page, failedAccepted.job_id, 'failed');
  expect(failed.error_code).toBe('KNOWLEDGE_REVISION_BUILD_FAILED');
  const afterFailure = await h2Facts(page);
  expect(afterFailure.overall_revision).toBe(Number(baseline.overall_revision) + 1);
  await expect(page.getByRole('button', { name: '保存正文', exact: true })).toBeVisible();
  // 已发布head保持；取消原queued任务后放开唤醒，迟到worker也不能发布。
  await control('knowledge.started', true);
  await control('knowledge.pause');
  const cancelAccepted = await saveBody(page, NEW_BODY + '\n取消重建草稿：F2_BUILD_CANCELLED。');
  await waitControl('knowledge.started');
  await revisionState(page, cancelAccepted.job_id, 'queued');
  const cancelResponse = page.waitForResponse(r => new URL(r.url()).pathname.endsWith('/cancel')
    && r.request().method() === 'POST');
  await page.getByRole('button', { name: '取消重建', exact: true }).click();
  expect((await cancelResponse).status()).toBe(200);
  const cancelled = await revisionState(page, cancelAccepted.job_id, 'cancelled');
  await control('knowledge.pause', true);
  expect((await h2Facts(page)).overall_revision).toBe(afterFailure.overall_revision);
  const cancelledSnapshot = await h2Facts(page);
  const cancelledPersistent = object(rows(cancelledSnapshot, 'revision_jobs').find(r => r.id === cancelled.id));
  expect(typeof cancelledPersistent.candidate_version_id).toBe('string');
  expect(rows(cancelledSnapshot, 'assets').some(r => r.version_id === cancelledPersistent.candidate_version_id)).toBe(false);
  await audit(info, 'h2-cancelled-rebuild-no-publication', { cancelled_job: cancelled,
    persistent_job: cancelledPersistent, actual_cancel_ui: true, head_unchanged: true, candidate_assets: 0 });
  // 重试同一未发布失败正文，避免原UI相同正文走200字段更新；原客户端身份必须更新。
  const retry = await saveBody(page, failedBody);
  expect(retry.job_id).not.toBe(failedAccepted.job_id);
  const retryPublished = await revisionState(page, retry.job_id, 'completed');
  await audit(info, 'h2-failed-build-recoverable', { failed_job: failed, retry_job: retryPublished,
    no_failure_head_advance: true, old_assets_unchanged: true });
  // SOP员工仍绑定旧版本，旧实例合法恢复；DTA员工明确换head后旧源权限必须拒绝。
  const oldSopRead = await authenticatedRead(page, `/api/enterprise/knowledge/documents/${String(oldAssets.find(r => r.kind === 'document' && r.old)?.id)}/content?tenant_id=tenant_demo&agent_id=agent_f1_sop`);
  expect(oldSopRead.status).toBe(200);
  await page.goto(`/workspace/chat/${String(oldSop.instance.session_id)}`);
  await finishBusiness(page, info, oldSop.instance, oldSop.operation);
  await synchronize(page, 'DTA');
  const revoked = await authenticatedRead(page, `/api/enterprise/knowledge/documents/${String(oldAssets.find(r => r.kind === 'document' && r.old)?.id)}/content?tenant_id=tenant_demo&agent_id=agent_f1_dta`);
  expect(revoked.status).toBe(404);
  const beforeRevoked = await facts(page);
  const revokedSource = object(object(oldDynamic.instance.metadata).time_context).source_input_id;
  expect(revokedSource).toBeTruthy();
  await request(page, `/__e2e/f1/provider/complete/${String(oldDynamic.operation.external_task_id)}`, 'POST');
  try {
    await expect.poll(async () => (await facts(page)).instances.find(r => r.id === oldDynamic.instance.id)?.status,
      { timeout: 60_000 }).toBe('failed');
  } finally {
    // 原失败也保存Signal/租约/Operation，不以未来重跑覆盖当前闭合证据。
    await audit(info, 'h2-revoked-runtime-closure-observation', {
      execution_id: oldDynamic.instance.id, before: beforeRevoked,
      ledger: await facts(page), snapshot: await h2Facts(page) });
  }
  const afterRevoked = await facts(page);
  expect(afterRevoked.sdk.filter(r => r.source_input_id === revokedSource)).toHaveLength(
    beforeRevoked.sdk.filter(r => r.source_input_id === revokedSource).length);
  expect(afterRevoked.work_items.filter(r => r.instance_id === oldDynamic.instance.id
    && r.status === 'offered' && r.attention_kind === 'tool_approval')).toHaveLength(0);
  expect(afterRevoked.provider.finishes.filter(r => r.receipt_id === oldDynamic.remote.receipt_id)).toHaveLength(0);
  await audit(info, 'h2-old-binding-authorized-or-revoked', { legal_old_source_status: 200,
    revoked_old_source_status: revoked.status, revoked_instance: afterRevoked.instances.find(r => r.id === oldDynamic.instance.id),
    revoked_successor_write_count: 0, revoked_source_input_id: revokedSource, next_sdk_count: 0, before: beforeRevoked, after: afterRevoked });
  await synchronize(page, 'SOP');
  // 引擎选择按tenant/user持久；沿原UI明确切回SOP，再复用原完整SOP旅程。
  await draft(page, 'SOP');
  await logout(page);
  page = await idle(page);
  const freshSop = await startWaitingSop(page, info);
  await citation(page, info, freshSop.instance, NEW_MARKER, retryPublished.published_version_id);
  await finishBusiness(page, info, freshSop.instance, freshSop.operation);
  await logout(page);
  page = await idle(page);
  const freshDynamic = await startWaitingDynamic(page, browser, info);
  page = freshDynamic.page;
  await request(page, `/__e2e/f1/provider/complete/${String(freshDynamic.operation.external_task_id)}`, 'POST');
  const approval = await pendingAttention(page, String(freshDynamic.instance.id), 'tool_approval');
  page = await idle(page);
  page = await approveDynamic(page, browser, info, approval, [String(freshDynamic.remote.receipt_id)]);
  await expect.poll(async () => (await facts(page)).instances.find(r => r.id === freshDynamic.instance.id)?.status,
    { timeout: 60_000 }).toBe('succeeded');
  await citation(page, info, freshDynamic.instance, NEW_MARKER, retryPublished.published_version_id);
  const final = await facts(page);
  const originalResult = final.results.filter(r => r.execution_id === freshDynamic.instance.id);
  expect(originalResult).toHaveLength(1);
  expect(originalResult[0]).toMatchObject({ verification_passed: true, status: 'verified' });
  expect(final.provider.finishes.filter(r => r.receipt_id === freshDynamic.remote.receipt_id)).toHaveLength(1);
  await expect(page.getByText(NEW_MARKER, { exact: false }).last()).toBeVisible();
  await audit(info, 'h2-new-original-consumers-and-business', { sop_instance: freshSop.instance,
    dynamic_instance: freshDynamic.instance, result: originalResult[0], ledger: final,
    snapshot: await h2Facts(page), expected_version_id: retryPublished.published_version_id });
});

for (const kind of ['SOP', 'DTA'] as const) {
  test(`JRN06 ${kind} exact memory correction, late job, real restart, clear race and fresh consumption`, async ({ page, browser }, info) => {
    test.setTimeout(300_000);
    await login(page, kind);
    const baseTurn = await capture(page, kind, H2_FACTS.base);
    await captureState(page, baseTurn.job.job_id, 'completed');
    let frozen: Awaited<ReturnType<typeof startWaitingDynamic>> | undefined;
    let initialProjection: Row | undefined;
    if (kind === 'DTA') {
      await logout(page);
      page = await idle(page);
      frozen = await startWaitingDynamic(page, browser, info);
      page = frozen.page;
      initialProjection = object(rows(await h2Facts(page), 'projections')
        .find(r => r.execution_id === frozen?.instance.id)?.initial);
      const savedBase = rows(await h2Facts(page), 'memories').find(m => m.capture_job_id === baseTurn.job.job_id);
      expect(rows(initialProjection, 'selected').some(r => r.id === savedBase?.id)).toBe(true);
    }
    const late = await capture(page, kind, H2_FACTS.late);
    await waitControl('memory-late.started');
    const running = await captureState(page, late.job.job_id, 'running');
    const corrected = await capture(page, kind, H2_FACTS.correct);
    const correction = await captureState(page, corrected.job.job_id, 'completed');
    expect(Number(correction.source_sequence)).toBeGreaterThan(Number(running.source_sequence));
    await control('memory-late.release');
    const lateResult = await captureState(page, late.job.job_id, 'completed');
    expect(lateResult).toMatchObject({ applied_count: 0, discarded_count: 1 });
    const correctedRows = rows(await h2Facts(page), 'memories').filter(r => r.agent_id === `agent_f1_${kind.toLowerCase()}`);
    expect(correctedRows.filter(r => object(r.fact_flags).correct)).toHaveLength(1);
    expect(correctedRows.some(r => object(r.fact_flags).late || object(r.fact_flags).base)).toBe(false);
    await audit(info, `${kind}-h2-correction-beats-late-source`, { initial_turn: baseTurn,
      late_turn: late, corrected_turn: corrected, correction, late_result: lateResult, memories: correctedRows });
    await consume(page, info, kind, 'correct');
    await control('pause');
    await control('drop-notification');
    const pending = await capture(page, kind, H2_FACTS.correct);
    expect(pending.job).toMatchObject({ status: 'pending', model_dispatch_count: 0 });
    page = await idle(page);
    await restart(page, info);
    await control('pause', true);
    const recovered = await captureState(page, pending.job.job_id, 'completed');
    expect(recovered).toMatchObject({ applied_count: 1, model_dispatch_count: 1,
      source_message_id: pending.source.id, assistant_message_id: pending.assistant.id });
    await control('drop-notification', true);
    await audit(info, `${kind}-h2-same-source-scanner-recovery`, { pending_turn: pending, recovered });
    await control('memory-clear.started', true);
    await control('memory-clear.release', true);
    const clearing = await capture(page, kind, H2_FACTS.clear);
    await waitControl('memory-clear.started');
    await captureState(page, clearing.job.job_id, 'running');
    const beforeClear = await h2Facts(page);
    const other = rows(beforeClear, 'memories').filter(r => r.agent_id !== `agent_f1_${kind.toLowerCase()}`);
    await clearMemory(page, kind);
    await control('memory-clear.release');
    const discarded = await captureState(page, clearing.job.job_id, 'discarded');
    expect(discarded.applied_count).toBe(0);
    const cleared = await h2Facts(page);
    expect(rows(cleared, 'memories').filter(r => r.agent_id === `agent_f1_${kind.toLowerCase()}` && r.user_id === 'admin')).toHaveLength(0);
    expect(rows(cleared, 'memories').filter(r => r.agent_id !== `agent_f1_${kind.toLowerCase()}`)).toEqual(other);
    const fresh = await capture(page, kind, H2_FACTS.fresh);
    const freshResult = await captureState(page, fresh.job.job_id, 'completed');
    expect(freshResult.applied_count).toBe(1);
    expect(Number(freshResult.agent_generation)).toBeGreaterThan(Number(discarded.agent_generation));
    await manageMemory(page, kind);
    await page.getByRole('textbox', { name: '用户名', exact: true }).fill('admin');
    await page.getByRole('button', { name: '查询', exact: true }).click();
    await expect(page.getByRole('table', { name: '员工记忆' }).getByText(H2_FACTS.fresh, { exact: false }).first()).toBeVisible();
    const full = await authenticatedRead(page, `/api/enterprise/memories/page?tenant_id=tenant_demo&agent_id=agent_f1_${kind.toLowerCase()}&user_id=admin&page=1&page_size=10`);
    expect(full.status).toBe(200);
    expect(object(full.body).total).toBe(1);
    await audit(info, `${kind}-h2-clear-race-and-new-source`, { late_clear_turn: clearing, discarded,
      fresh_turn: fresh, fresh_result: freshResult, full_management_count: 1, other_memories_preserved: true,
      snapshot: await h2Facts(page) });
    await consume(page, info, kind, 'fresh');
    if (frozen && initialProjection) {
      await request(page, `/__e2e/f1/provider/complete/${String(frozen.operation.external_task_id)}`, 'POST');
      const approval = await pendingAttention(page, String(frozen.instance.id), 'tool_approval');
      page = await idle(page);
  page = await approveDynamic(page, browser, info, approval, [String(frozen.remote.receipt_id)]);
      await expect.poll(async () => (await facts(page)).instances.find(r => r.id === frozen?.instance.id)?.status,
        { timeout: 60_000 }).toBe('succeeded');
      const active = object(rows(await h2Facts(page), 'projections')
        .find(r => r.execution_id === frozen?.instance.id)?.active);
      expect(rows(active, 'selected')).toHaveLength(0);
      const resumedSdk = rows(await h2Facts(page), 'sdk').filter(r => r.phase === 'Dynamic Action'
        && r.source_input_id === initialProjection?.source_input_id);
      expect(resumedSdk.length).toBeGreaterThan(0);
      const revokedResume = resumedSdk.filter(r => Object.values(object(r.memory_fact_flags))
        .every(value => value === false));
      expect(revokedResume.length).toBeGreaterThan(0);
      expect(revokedResume.every(r => Object.values(object(r.completed_output_fact_flags))
        .every(value => value === false))).toBe(true);
      expect(revokedResume.every(r => r.completed_output_customer_present === false)).toBe(true);
      expect(revokedResume.some(r => Array.isArray(r.completed_output_reference_fields)
        && r.completed_output_reference_fields.includes('receipt_id'))).toBe(true);
      await audit(info, 'DTA-h2-frozen-recovery-only-subtracts', { execution_id: frozen.instance.id,
        initial_projection: initialProjection, resumed_projection: active, resumed_sdk: resumedSdk,
        no_new_fact_added: true, selected_after_clear: 0, ledger: await facts(page) });
    }
    await audit(info, `${kind}-h2-memory-terminal`, { snapshot: await h2Facts(page), ledger: await facts(page) });
    await control('memory-late.started', true);
    await control('memory-late.release', true);
  });
}
