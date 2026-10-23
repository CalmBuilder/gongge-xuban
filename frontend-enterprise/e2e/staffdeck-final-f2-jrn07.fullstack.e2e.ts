/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : staffdeck-final-f2-jrn07.fullstack.e2e.ts
 * @CallChain  : Composer旧不足 → 本人分享 → SkillEditor → 人工原发布 → 新旧真实业务读回
 * @Description: 完整四case单worker零retry；故障、准确来源及真实重启与原账本交叉。
 */
import { expect, test, type Page } from '@playwright/test';
import type { SkillRead } from '../src/types';
import type { FeedbackMaterialRead } from '../src/types/feedback';
import type { SkillCandidateRead, SkillCandidateReceipt } from '../src/types/skill-candidate';
import { object, type Row } from './final-f1-browser-helpers';
import { audit, authenticatedRead, downgradeManager } from './final-f2-browser-helpers';
import {
  AGENT, FEEDBACK, PRIVATE_NOTE, SKILL, arm, candidatePath, cleared, draft, entered, head,
  idle, inputHash, instance, login, openCandidate, ownerDetail, pointer, read, release, responseFor, restart,
  rewrite, send, share, state, terminal, withdraw,
} from './final-f2-h3-helpers';

test.skip(process.env.FULLSTACK_FINAL_F2 !== '1' || process.env.FULLSTACK_F2_JOURNEY !== 'JRN07',
  '需要显式自有H3服务和隔离数据库');
test.describe.configure({ mode: 'serial' });
test.setTimeout(720_000);

let original: SkillRead;
let sourceSession: string;
let originalMaterial: FeedbackMaterialRead;
let published: SkillCandidateReceipt;

function binding(value: SkillCandidateRead): unknown {
  /** 从实际候选的共享SkillCard读取绑定，不以人工标签构造候选正文。 */
  const node = value.content?.nodes.find(row => row.node_id === 'query');
  return object(node?.metadata).operation_input;
}

async function save(page: Page, id: string): Promise<SkillCandidateRead> {
  /** 原候选审核区的实际保存按钮，不通过API预置ready或正文。 */
  const response = responseFor(page, '/feedback-candidates/' + id, 'PUT');
  await page.getByRole('region', { name: '反馈候选审核' }).getByRole('button', { name: '仅保存隔离候选' }).click();
  const result = await response;
  expect(result.status()).toBe(200);
  const sent = object(result.request().postDataJSON());
  const sentContent = object(sent.content);
  const saved = await result.json() as SkillCandidateRead;
  expect(saved.content?.name).toBe(sentContent.name);
  return saved;
}

async function confirm(page: Page, candidate: SkillCandidateRead, status = 200): Promise<unknown> {
  /** 人工沿唯一现有发布入口确认；差异确认适用时不能绕过checkbox。 */
  await page.getByRole('region', { name: '反馈候选审核' }).getByRole('button', { name: '人工确认发布候选' }).click();
  const dialog = page.getByRole('dialog', { name: '确认沿现有发布链发布候选' });
  if (candidate.base_difference) await dialog.getByRole('checkbox', { name: '我已核对原反馈版本与当前基础的差异' }).click();
  const response = responseFor(page, '/feedback-candidates/' + candidate.id + '/publish', 'POST');
  await dialog.getByRole('button', { name: '确认发布', exact: true }).click();
  const result = await response;
  expect(result.status()).toBe(status);
  return result.json() as Promise<unknown>;
}

async function unchanged(page: Page, id: string, versionCount: number): Promise<void> {
  /** 发布持久表与员工活动头共同核验，候选保存或失败不能产生额外发布。 */
  expect((await head(page)).publication_id).toBe(id);
  expect((await state(page)).publication_count).toBe(versionCount);
}

function sdkRows(ledger: Awaited<ReturnType<typeof state>>, name: string): Row[] {
  /** 只计原HTTP端点收到的对应case实际SDK调用，不能在首响应未释放时定结论。 */
  return ledger.sdk.filter(row => row.case === name && row.kind === 'candidate');
}

test('JRN07 isolation: actual old goal missing; exact owner share; reject, compile, model failure, repair and cancellation', async ({ page, browser }, info) => {
  let owner = await login(page);
  const context = await browser.newContext({ baseURL: info.project.use.baseURL });
  let manager = await login(await context.newPage(), true);
  try {
    original = await head(owner);
    const initial = await state(owner);
    expect(initial.instances).toHaveLength(0);
    expect(initial.publication_count).toBe(1);
    expect(initial.labels.business).toEqual({ E001: 1500, E002: 600 });
    expect(original.publication_scope_agent_id).toBe(AGENT);
    await draft(owner);
    sourceSession = await send(owner, '查询本人额度 F2-H3-OLD：本人员工号E001，关联人编号E002。请完成本人的额度查询。');
    const old = await instance(owner, sourceSession, 'succeeded');
    const oldLedger = await state(owner);
    const operations = oldLedger.operations.filter(row => row.instance_id === old.id);
    expect(operations).toHaveLength(1);
    expect(operations[0]).toMatchObject({ status: 'succeeded', request: { employee_id: 'E002' }, result: { employee_id: 'E002', quota: 600 } });
    expect(oldLedger.business_calls).toHaveLength(1);
    expect(oldLedger.business_calls[0]).toMatchObject({ actual_http: true, request: { employee_id: 'E002' }, response: { employee_id: 'E002', quota: 600 } });
    expect(oldLedger.business_calls[0].request_hash).toBe(operations[0].request_fingerprint);
    expect(old.skill_version_id).toBe(original.publication_id);
    expect(old.definition_checksum).toBe(original.publication_checksum);
    await expect(owner.locator('body')).toContainText('600');
    await audit(info, 'h3-old-actual-business-goal-missing', { passed: true, session_id: sourceSession,
      instance_id: old.id, operation_id: operations[0].id, ledger: oldLedger, expected_employee: 'E001', expected_quota: 1500 });

    arm('feedback', 'initial-feedback-fail', 'fail');
    const rated = responseFor(owner, '/feedback', 'POST');
    await owner.getByRole('button', { name: '点踩', exact: true }).last().click();
    const feedback = object(await (await rated).json());
    expect(feedback.session_id).toBe(sourceSession);
    await expect.poll(async () => (await state(owner)).feedback.find(row => row.id === feedback.id)?.analysis_status).toBe('failed');
    // 原分析3个外层attempt，各走原JSON初次+3次修复；不缩短产品重试来造失败。
    expect((await state(owner)).sdk.filter(row => row.case === 'initial-feedback-fail' && row.kind === 'feedback')).toHaveLength(12);
    originalMaterial = await share(owner, sourceSession);
    const source = originalMaterial.material.source;
    expect(source).toMatchObject({ status: 'verified', branch: 'sop', execution_id: old.id,
      skill_id: SKILL, agent_id: AGENT, skill_version_id: original.publication_id,
      definition_checksum: original.publication_checksum, message_id: feedback.message_id });
    expect(oldLedger.messages.some(row => row.id === source.turn_id && row.role === 'user' && row.session_id === sourceSession)).toBe(true);
    expect(oldLedger.messages.some(row => row.id === source.message_id && row.role === 'assistant' && row.session_id === sourceSession)).toBe(true);
    expect(originalMaterial.material.facts.operations.map(row => row.id)).toEqual([operations[0].id]);
    await audit(info, 'h3-exact-owner-minimal-redacted-share', { passed: true, material_id: originalMaterial.id,
      feedback_id: feedback.id, material_checksum: originalMaterial.material_checksum, source,
      pii_redacted: true, facts_operation_ids: originalMaterial.material.facts.operations.map(row => row.id),
      ledger: await state(owner) });
    owner = await idle(owner);

    let candidate = await openCandidate(manager, originalMaterial);
    const rejectId = candidate.id;
    await manager.getByRole('region', { name: '反馈候选审核' }).getByRole('button', { name: '拒绝此候选' }).click();
    await cleared(manager, rejectId);
    await manager.reload();
    await cleared(manager, rejectId);
    await unchanged(manager, original.publication_id || '', 1);
    await audit(info, 'h3-rejected-candidate-no-publication', { passed: true, candidate_id: rejectId, ledger: await state(manager) });

    candidate = await openCandidate(manager, originalMaterial);
    await manager.getByRole('combobox').filter({ hasText: '调用工具' }).click();
    await manager.getByRole('option', { name: '子流程', exact: true }).click();
    candidate = await save(manager, candidate.id);
    const compile = object(await confirm(manager, candidate, 422));
    await unchanged(manager, original.publication_id || '', 1);
    await audit(info, 'h3-real-compile-failure-no-publication', { passed: true, candidate_id: candidate.id,
      response_status: 422, error_hash_present: Boolean(compile.detail), ledger: await state(manager) });

    candidate = await openCandidate(manager, originalMaterial);
    const failedBefore = object((await state(manager)).candidates.find(row => row.id === candidate.id));
    const failedJob = await rewrite(manager, 'model-fail', 'fail');
    await terminal(manager, 'model-fail', 'failed');
    await cleared(manager, candidate.id);
    const failedLedger = await state(manager);
    const failedAfter = object(failedLedger.candidates.find(row => row.id === candidate.id));
    expect(failedAfter.content_hash).toBe(failedBefore.content_hash);
    expect(failedAfter.revision).toBe(failedBefore.revision);
    expect(sdkRows(failedLedger, 'model-fail')).toHaveLength(2);
    await unchanged(manager, original.publication_id || '', 1);
    await audit(info, 'h3-invalid-model-two-calls-no-write-or-publication', { passed: true,
      candidate_id: candidate.id, job_id: failedJob, before: failedBefore, after: failedAfter, ledger: failedLedger });

    candidate = await openCandidate(manager, originalMaterial);
    const retryJob = await rewrite(manager, 'legal-repair', 'bad_first');
    await terminal(manager, 'legal-repair', 'succeeded');
    const repaired = await read<SkillCandidateRead>(manager, candidatePath(candidate.id));
    expect(repaired.revision).toBe(candidate.revision + 1);
    expect(binding(repaired)).toEqual({ employee_id: 'slots.employee_id' });
    const expected = structuredClone(original.content);
    const query = expected.nodes.find(row => row.node_id === 'query');
    if (!query) throw new Error('H3_ORIGINAL_QUERY_REQUIRED');
    object(query.metadata).operation_input = { employee_id: 'slots.employee_id' };
    expect(repaired.content).toEqual(expected);
    expect(JSON.stringify(repaired.content)).not.toMatch(/E001|E002|1500|\b600\b/);
    expect(sdkRows(await state(manager), 'legal-repair')).toHaveLength(3);
    await unchanged(manager, original.publication_id || '', 1);
    await audit(info, 'h3-legal-sdk-repair-preserves-only-intended-binding', { passed: true,
      candidate_id: repaired.id, job_id: retryJob, revision_before: candidate.revision,
      revision_after: repaired.revision, behavior_only_intended_change: true, ledger: await state(manager) });

    candidate = await openCandidate(manager, originalMaterial);
    const cancelBefore = object((await state(manager)).candidates.find(row => row.id === candidate.id));
    const cancelJob = await rewrite(manager, 'cancel-late', 'hold_first');
    await entered('cancel-late');
    const cancelResponse = responseFor(manager, '/jobs/' + cancelJob + '/cancel', 'POST');
    await manager.getByRole('button', { name: '停止', exact: true }).click();
    expect((await cancelResponse).status()).toBe(200);
    release('cancel-late');
    const cancelTerminal = await terminal(manager, 'cancel-late', 'failed');
    const cancelLedger = await state(manager);
    const cancelAfter = object(cancelLedger.candidates.find(row => row.id === candidate.id));
    expect(cancelAfter.content_hash).toBe(cancelBefore.content_hash);
    expect(sdkRows(cancelLedger, 'cancel-late')).toHaveLength(1);
    expect(cancelTerminal.cache_has_complete).toBe(false);
    await unchanged(manager, original.publication_id || '', 1);
    await audit(info, 'h3-ui-cancel-late-result-next-sdk-zero', { passed: true, candidate_id: candidate.id,
      job_id: cancelJob, before: cancelBefore, after: cancelAfter, ledger: cancelLedger });
    manager = await idle(manager);
  } finally { release('cancel-late'); await context.close(); }
});

test('JRN07 publication: real stream reconnect, generic404 preserves valid edit; human publication improves new task while old restart stays frozen', async ({ page, browser }, info) => {
  let owner = await login(page);
  const context = await browser.newContext({ baseURL: info.project.use.baseURL });
  let manager = await login(await context.newPage(), true);
  let allowMetadata: (() => void) | undefined;
  try {
    const frozenVersion = object((await state(owner)).versions.find(row => row.id === original.publication_id));
    await draft(owner);
    const waitingSession = await send(owner, '查询本人额度 F2-H3-WAIT：请先询问我的员工号和关联人编号。');
    const waiting = await instance(owner, waitingSession, 'waiting');
    expect(waiting.skill_version_id).toBe(original.publication_id);
    expect((await state(owner)).business_calls).toHaveLength(1);
    await audit(info, 'h3-old-waiting-before-publication', { passed: true, session_id: waitingSession,
      instance_id: waiting.id, ledger: await state(owner) });
    const competing = await openCandidate(owner, originalMaterial);
    const competitorUrl = owner.url();
    await owner.getByRole('textbox', { name: 'SOP名称', exact: true }).fill('F2_H3_COMPETING_OLD_BASE');
    const competitorSaved = await save(owner, competing.id);
    owner = await idle(owner);
    let candidate = await openCandidate(manager, originalMaterial);
    const editURL = manager.url();
    const key = pointer(candidate.id);
    const reconnectJob = await rewrite(manager, 'network-reconnect', 'hold_first');
    await entered('network-reconnect');
    await expect.poll(() => manager.evaluate(key => sessionStorage.getItem(key), key)).not.toBeNull();
    let reconnectGet200 = false;
    manager.on('response', response => {
      if (new URL(response.url()).pathname === '/api/enterprise/skills/jobs/' + reconnectJob
        && response.request().method() === 'GET' && response.status() === 200) reconnectGet200 = true;
    });
    await manager.goto(FEEDBACK); // 实际navigation中断在途stream，不伪造响应。
    await manager.goto(editURL);
    await expect.poll(() => reconnectGet200).toBe(true);
    release('network-reconnect');
    await terminal(manager, 'network-reconnect', 'succeeded');
    await expect(manager.getByRole('button', { name: '停止', exact: true })).toHaveCount(0);
    await expect.poll(() => manager.evaluate(key => sessionStorage.getItem(key), key)).toBeNull();
    candidate = await read<SkillCandidateRead>(manager, candidatePath(candidate.id));
    expect(binding(candidate)).toEqual({ employee_id: 'slots.employee_id' });
    expect(sdkRows(await state(manager), 'network-reconnect')).toHaveLength(3);
    const rewrittenContent = candidate.content;
    expect(rewrittenContent?.name).toBe(original.content.name);
    await unchanged(manager, original.publication_id || '', 1);
    await audit(info, 'h3-real-network-abort-valid-stream-reconnect', { passed: true,
      candidate_id: candidate.id, job_id: reconnectJob, real_get200: reconnectGet200, ledger: await state(manager) });

    const restartProof = await restart(manager);
    expect(object((await state(manager)).instances.find(row => row.id === waiting.id)).time_context)
      .toEqual(waiting.time_context);
    await manager.evaluate(({ key, job }) => sessionStorage.setItem(key, JSON.stringify({ job_id: job })), { key, job: reconnectJob });
    let metadataPaused = false;
    await manager.route('**/api/enterprise/skills/jobs/*', async route => {
      metadataPaused = true;
      await new Promise<void>(resolve => { allowMetadata = resolve; });
      await route.continue();
    });
    const realMissing = responseFor(manager, '/jobs/' + reconnectJob, 'GET');
    await manager.reload();
    await expect.poll(() => metadataPaused).toBe(true);
    const title = manager.getByRole('textbox', { name: 'SOP名称', exact: true });
    await expect(title).toBeVisible();
    await title.fill('F2_H3_PRIVATE_UNSAVED');
    await manager.getByRole('button', { name: '保存隔离候选', exact: true }).click();
    const saveDialog = manager.getByRole('dialog', { name: '保存隔离候选' });
    await expect(saveDialog).toBeVisible();
    allowMetadata?.();
    const missingResponse = await realMissing;
    expect(missingResponse.status()).toBe(404);
    expect(object(await missingResponse.json()).detail).toBe('Job not found');
    await manager.unroute('**/api/enterprise/skills/jobs/*');
    await expect.poll(() => manager.evaluate(key => sessionStorage.getItem(key), key)).toBeNull();
    await expect(title).toHaveValue('F2_H3_PRIVATE_UNSAVED');
    await expect(saveDialog).toBeVisible();
    await saveDialog.getByRole('button', { name: '取消', exact: true }).click();
    await expect(saveDialog).toHaveCount(0);
    await title.focus();
    await expect(title).toBeFocused();
    await title.fill('');
    await title.pressSequentially(original.content.name);
    await title.press('Tab');
    await expect(title).toHaveValue(original.content.name);
    await expect(title).toHaveAttribute('value', original.content.name);
    candidate = await save(manager, candidate.id);
    expect(candidate.content).toEqual(rewrittenContent);
    expect(binding(candidate)).toEqual({ employee_id: 'slots.employee_id' });
    await audit(info, 'h3-real-restart-generic404-fresh-keeps-valid-private-edit', { passed: true,
      candidate_id: candidate.id, job_id: reconnectJob, real_status: missingResponse.status(), restart: restartProof,
      unsaved_title_kept: true, private_dialog_kept: true, pointer_cleared: true, ledger: await state(manager) });
    const review = manager.getByRole('region', { name: '反馈候选审核' });
    await review.getByText('查看当前原版与已保存候选差异').click();
    await expect(review).toContainText('slots.employee_id');
    published = await confirm(manager, candidate) as SkillCandidateReceipt;
    expect(published).toMatchObject({ candidate_id: candidate.id, skill_id: SKILL, scope_agent_id: AGENT });
    expect(published.skill_version_id).not.toBe(original.publication_id);
    await unchanged(manager, published.skill_version_id, 2);
    await manager.reload();
    await expect(review.getByRole('status')).toContainText(published.skill_version_id);
    await expect(manager.getByRole('textbox', { name: 'SOP名称', exact: true })).toHaveCount(0);
    await audit(info, 'h3-human-confirm-original-publisher-only-one-successor', { passed: true,
      candidate_id: candidate.id, receipt: { ...published }, ledger: await state(manager) });
    manager = await idle(manager);

    await draft(owner);
    const newSession = await send(owner, '查询本人额度 F2-H3-NEW：本人员工号E001，关联人编号E002。请完成本人的额度查询。');
    const improved = await instance(owner, newSession, 'succeeded');
    expect(improved.skill_version_id).toBe(published.skill_version_id);
    expect(improved.definition_checksum).toBe(published.definition_checksum);
    const improvedLedger = await state(owner);
    const improvedOps = improvedLedger.operations.filter(row => row.instance_id === improved.id);
    expect(improvedOps).toHaveLength(1);
    expect(improvedOps[0]).toMatchObject({ status: 'succeeded', request: { employee_id: 'E001' }, result: { employee_id: 'E001', quota: 1500 } });
    expect(improvedLedger.business_calls).toHaveLength(2);
    expect(improvedLedger.business_calls[1]).toMatchObject({ actual_http: true, request: { employee_id: 'E001' }, response: { employee_id: 'E001', quota: 1500 } });
    expect(improvedLedger.business_calls[1].request_hash).toBe(improvedOps[0].request_fingerprint);
    await expect(owner.locator('body')).toContainText('1500');
    await audit(info, 'h3-new-composer-independent-business-improvement', { passed: true, session_id: newSession,
      instance_id: improved.id, operation_id: improvedOps[0].id, ledger: improvedLedger });

    const oldMessagesLoaded = responseFor(owner, '/api/chat/sessions/' + waitingSession + '/messages', 'GET');
    await owner.goto(`/workspace/chat/${waitingSession}`);
    expect((await oldMessagesLoaded).status()).toBe(200);
    const resumeText = '查询本人额度 F2-H3-RESUME：本人员工号E001，关联人编号E002，请继续原来的本人额度查询。';
    await send(owner, resumeText);
    const resumed = await instance(owner, waitingSession, 'succeeded');
    expect(resumed.id).toBe(waiting.id);
    expect(resumed.skill_version_id).toBe(waiting.skill_version_id);
    expect(resumed.definition_checksum).toBe(waiting.definition_checksum);
    const resumedLedger = await state(owner);
    const resumeInputs = resumedLedger.messages.filter(row => row.session_id === waitingSession
      && row.role === 'user' && row.content_hash === inputHash(resumeText));
    expect(resumeInputs).toHaveLength(1);
    const resumeInput = object(resumeInputs[0]);
    expect(object(resumed.time_context).source_input_id).toBe(resumeInput.id);
    expect(resumed.time_context).toEqual(resumeInput.time_context);
    const collect = object(resumedLedger.nodes.find(row => row.instance_id === waiting.id && row.node_id === 'collect'));
    expect(collect.input_time_context).toEqual(waiting.time_context);
    expect(collect.resume_time_context).toEqual(resumeInput.time_context);
    const resumedOps = resumedLedger.operations.filter(row => row.instance_id === waiting.id);
    expect(resumedOps).toHaveLength(1);
    expect(resumedOps[0]).toMatchObject({ request: { employee_id: 'E002' }, result: { employee_id: 'E002', quota: 600 } });
    expect(resumedLedger.business_calls).toHaveLength(3);
    expect(resumedLedger.business_calls[2].request_hash).toBe(resumedOps[0].request_fingerprint);
    const oldVersion = object(resumedLedger.versions.find(row => row.id === original.publication_id));
    expect(oldVersion).toEqual(frozenVersion);
    await owner.reload();
    expect((await state(owner)).business_calls).toHaveLength(3);
    await audit(info, 'h3-restarted-old-waiting-original-version-business-and-time', { passed: true,
      instance_id: resumed.id, operation_id: resumedOps[0].id, waiting_before: waiting,
      restart: restartProof, original_version_before: frozenVersion,
      resume_input_id: resumeInput.id, collect_node_id: collect.id,
      ledger: resumedLedger, reload_duplicate_dispatches: 0 });

    const staleResponse = responseFor(owner, '/feedback-candidates/' + competing.id, 'GET');
    await owner.goto(competitorUrl);
    expect((await staleResponse).status()).toBe(409);
    await cleared(owner, competing.id);
    const stale = await authenticatedRead(owner, candidatePath(competing.id));
    expect(stale.status).toBe(409);
    await unchanged(owner, published.skill_version_id, 2);
    await audit(info, 'h3-other-actor-old-base-race-refused-no-extra-publication', { passed: true,
      candidate_id: competing.id, competing_base: competitorSaved.base_skill_version_id, status: stale.status,
      two_actor_identity: true, ledger: await state(owner) });
  } finally { allowMetadata?.(); release('network-reconnect'); await context.close(); }
});

test('JRN07 withdrawal: real GET200 then stream invalidation clears; owner analysis remains legal; pre-dispatch and final CAS deny stale results', async ({ page, browser }, info) => {
  let owner = await login(page);
  const context = await browser.newContext({ baseURL: info.project.use.baseURL });
  let manager = await login(await context.newPage(), true);
  let allowMetadata: (() => void) | undefined;
  let allowStream: (() => void) | undefined;
  try {
    const candidate = await openCandidate(manager, originalMaterial);
    const before = object((await state(manager)).candidates.find(row => row.id === candidate.id));
    const firstJob = await rewrite(manager, 'withdraw-first', 'hold_first');
    await entered('withdraw-first');
    let get200 = false;
    let metadataPaused = false;
    let streamPaused = false;
    manager.on('response', response => {
      if (new URL(response.url()).pathname === '/api/enterprise/skills/jobs/' + firstJob
        && response.status() === 200 && response.request().method() === 'GET') get200 = true;
    });
    await manager.route('**/api/enterprise/skills/jobs/*/stream?*', async route => {
      streamPaused = true;
      await new Promise<void>(resolve => { allowStream = resolve; });
      await route.continue();
    });
    await manager.route('**/api/enterprise/skills/jobs/*', async route => {
      metadataPaused = true;
      await new Promise<void>(resolve => { allowMetadata = resolve; });
      await route.continue();
    });
    await manager.reload();
    await expect.poll(() => metadataPaused).toBe(true);
    await expect(manager.getByRole('textbox', { name: 'SOP名称', exact: true })).toBeVisible();
    await manager.getByRole('region', { name: '反馈候选审核' }).getByText('查看授权材料及原轮来源').click();
    await expect(manager.getByRole('region', { name: '反馈候选审核' })).toContainText(PRIVATE_NOTE);
    const privateTitle = manager.getByRole('textbox', { name: 'SOP名称', exact: true });
    const authorizedBeforeEdit = await read<SkillCandidateRead>(manager, candidatePath(candidate.id));
    expect(authorizedBeforeEdit.content?.name).not.toBe('F2_H3_WITHDRAW_UNSAVED');
    await privateTitle.fill('');
    await privateTitle.pressSequentially('F2_H3_WITHDRAW_UNSAVED');
    await privateTitle.press('Tab');
    await expect(manager.getByRole('textbox', { name: 'SOP名称', exact: true })).toHaveValue('F2_H3_WITHDRAW_UNSAVED');
    console.info('H3_PRIVATE_EDIT_OBSERVATION', JSON.stringify({
      metadata_paused: metadataPaused, stream_paused: streamPaused,
      save_enabled: await manager.getByRole('button', { name: '保存隔离候选', exact: true }).isEnabled(),
      review_save_enabled: await manager.getByRole('region', { name: '反馈候选审核' })
        .getByRole('button', { name: '仅保存隔离候选' }).isEnabled(),
      title_matches_private: await manager.getByRole('textbox', { name: 'SOP名称', exact: true }).inputValue() === 'F2_H3_WITHDRAW_UNSAVED',
      title_attribute_matches_private: await privateTitle.getAttribute('value') === 'F2_H3_WITHDRAW_UNSAVED',
      candidate_name_is_private: authorizedBeforeEdit.content?.name === 'F2_H3_WITHDRAW_UNSAVED',
      active_stop_count: await manager.getByRole('button', { name: '停止', exact: true }).count(),
    }));
    await manager.getByRole('button', { name: '保存隔离候选', exact: true }).click();
    await expect(manager.getByRole('dialog', { name: '保存隔离候选' })).toBeVisible();
    allowMetadata?.();
    await expect.poll(() => get200 && streamPaused).toBe(true);
    // 仅owner的日志/材料页活动，manager已持状态但无另一个重编辑器。
    await ownerDetail(owner, sourceSession);
    arm('feedback', 'owner-withdraw-analysis', 'hold_first');
    const reanalysis = responseFor(owner, '/' + originalMaterial.feedback_id + '/reanalyze', 'POST');
    await owner.getByRole('dialog', { name: '对话日志详情' }).getByRole('button', { name: '重新分析', exact: true }).click();
    expect((await reanalysis).status()).toBe(200);
    await entered('owner-withdraw-analysis');
    await withdraw(owner, originalMaterial);
    owner = await idle(owner);
    const streamRefused = manager.waitForResponse(row => new URL(row.url()).pathname === '/api/enterprise/skills/jobs/' + firstJob + '/stream');
    allowStream?.();
    const refusal = await streamRefused;
    expect([403, 404, 409]).toContain(refusal.status());
    expect(object(await refusal.json()).detail).toBe('FEEDBACK_CANDIDATE_UNAVAILABLE');
    await cleared(manager, candidate.id);
    await manager.unroute('**/api/enterprise/skills/jobs/*');
    await manager.unroute('**/api/enterprise/skills/jobs/*/stream?*');
    release('withdraw-first'); release('owner-withdraw-analysis');
    const finished = await terminal(manager, 'withdraw-first', 'failed');
    await expect.poll(async () => (await state(owner)).feedback.find(row => row.id === originalMaterial.feedback_id)?.analysis_status).toBe('analyzed');
    const withdrawalLedger = await state(manager);
    const after = object(withdrawalLedger.candidates.find(row => row.id === candidate.id));
    expect(after.content_hash).toBe(before.content_hash);
    expect(after.revision).toBe(before.revision);
    expect(sdkRows(withdrawalLedger, 'withdraw-first')).toHaveLength(1);
    expect(finished.cache_has_complete).toBe(false);
    expect(withdrawalLedger.sdk.filter(row => row.case === 'owner-withdraw-analysis')).toHaveLength(2);
    await manager.reload();
    await cleared(manager, candidate.id);
    await unchanged(manager, published.skill_version_id, 2);
    await audit(info, 'h3-source-withdraw-real-get200-stream-denial-private-clear-and-owner-legal-repair', {
      passed: true, candidate_id: candidate.id, job_id: firstJob, material_id: originalMaterial.id,
      real_get200: get200, real_stream_status: refusal.status(), before, after, ledger: withdrawalLedger });
    manager = await idle(manager);

    let material = await share(owner, sourceSession);
    owner = await idle(owner);
    let next = await openCandidate(manager, material);
    let beforeNext = object((await state(manager)).candidates.find(row => row.id === next.id));
    const preJob = await rewrite(manager, 'withdraw-before-guard', 'before_guard');
    await entered('withdraw-before-guard', 'guard');
    await withdraw(owner, material);
    owner = await idle(owner);
    release('withdraw-before-guard', 'guard');
    await terminal(manager, 'withdraw-before-guard', 'failed');
    let ledger = await state(manager);
    let afterNext = object(ledger.candidates.find(row => row.id === next.id));
    expect(sdkRows(ledger, 'withdraw-before-guard')).toHaveLength(0);
    expect(afterNext.content_hash).toBe(beforeNext.content_hash);
    expect(afterNext.revision).toBe(beforeNext.revision);
    await audit(info, 'h3-source-withdraw-before-original-guard-sdk-zero', { passed: true,
      candidate_id: next.id, job_id: preJob, material_id: material.id, before: beforeNext, after: afterNext, ledger });
    manager = await idle(manager);

    material = await share(owner, sourceSession);
    owner = await idle(owner);
    next = await openCandidate(manager, material);
    beforeNext = object((await state(manager)).candidates.find(row => row.id === next.id));
    const settleJob = await rewrite(manager, 'withdraw-before-settle', 'before_settle');
    await entered('withdraw-before-settle', 'settle');
    expect(sdkRows(await state(manager), 'withdraw-before-settle')).toHaveLength(2);
    await withdraw(owner, material);
    owner = await idle(owner);
    release('withdraw-before-settle', 'settle');
    await terminal(manager, 'withdraw-before-settle', 'failed');
    ledger = await state(manager);
    afterNext = object(ledger.candidates.find(row => row.id === next.id));
    expect(afterNext.content_hash).toBe(beforeNext.content_hash);
    expect(afterNext.revision).toBe(beforeNext.revision);
    expect(sdkRows(ledger, 'withdraw-before-settle')).toHaveLength(2);
    await unchanged(manager, published.skill_version_id, 2);
    await audit(info, 'h3-source-withdraw-after-two-sdk-original-final-cas-rejects', { passed: true,
      candidate_id: next.id, job_id: settleJob, material_id: material.id,
      before: beforeNext, after: afterNext, ledger });
  } finally {
    allowMetadata?.(); allowStream?.(); release('withdraw-first'); release('owner-withdraw-analysis');
    release('withdraw-before-guard', 'guard'); release('withdraw-before-settle', 'settle');
    await context.close();
  }
});

test('JRN07 authority: real member UI revocation denies late candidate; legitimate ordinary DTA owner feedback cannot create SOP candidate', async ({ page, browser }, info) => {
  let owner = await login(page);
  const context = await browser.newContext({ baseURL: info.project.use.baseURL });
  let manager = await login(await context.newPage(), true);
  try {
    const material = await share(owner, sourceSession);
    owner = await idle(owner);
    const candidate = await openCandidate(manager, material);
    const before = object((await state(manager)).candidates.find(row => row.id === candidate.id));
    const job = await rewrite(manager, 'authority-late', 'hold_first');
    await entered('authority-late');
    await downgradeManager(owner);
    owner = await idle(owner);
    release('authority-late');
    await terminal(manager, 'authority-late', 'failed');
    await cleared(manager, candidate.id);
    await manager.reload();
    await cleared(manager, candidate.id);
    const denied = await authenticatedRead(manager, candidatePath(candidate.id));
    expect([403, 404, 409]).toContain(denied.status);
    const authorityLedger = await state(manager);
    const after = object(authorityLedger.candidates.find(row => row.id === candidate.id));
    expect(after.content_hash).toBe(before.content_hash);
    expect(after.revision).toBe(before.revision);
    expect(sdkRows(authorityLedger, 'authority-late')).toHaveLength(1);
    expect(authorityLedger.publication_count).toBe(2);
    await audit(info, 'h3-real-role-revocation-next-sdk-write-publication-zero-private-cleared', {
      passed: true, candidate_id: candidate.id, job_id: job, material_id: material.id,
      current_read_status: denied.status, before, after, ledger: authorityLedger });
    manager = await idle(manager);

    const countBefore = (await state(owner)).business_calls.length;
    await draft(owner, true);
    const dtaSession = await send(owner, 'F2-DTA编号对照：解释employee_id本人编号与sponsor_id关联人编号的区别，不调用工具。');
    const dta = await instance(owner, dtaSession, 'succeeded');
    expect(dta.kind).toBe('dynamic_task');
    expect(dta.skill_version_id).toBeNull();
    await expect(owner.locator('body')).toContainText('employee_id');
    arm('feedback', 'ordinary-dta-feedback', 'normal');
    const rated = responseFor(owner, '/feedback', 'POST');
    await owner.getByRole('button', { name: '点踩', exact: true }).last().click();
    expect((await rated).status()).toBe(200);
    await expect.poll(async () => (await state(owner)).feedback.find(row => row.session_id === dtaSession)?.analysis_status).toBe('analyzed');
    const dtaMaterial = await share(owner, dtaSession);
    expect(dtaMaterial.material.source).toMatchObject({ status: 'verified', branch: 'dynamic_task',
      execution_id: dta.id, skill_id: null, skill_version_id: null, definition_checksum: null });
    await owner.goto(FEEDBACK);
    const materials = owner.getByRole('region', { name: '反馈改进材料' });
    await materials.getByRole('heading', { name: '员工已分享材料' }).locator('..').locator('article')
      .filter({ hasText: dtaMaterial.feedback_id }).getByRole('button', { name: '查看共享材料' }).click();
    await expect(materials).toContainText('dynamic_task');
    await expect(materials.getByRole('button', { name: '在当前编辑器改进 SOP' })).toHaveCount(0);
    const dtaLedger = await state(owner);
    const result = object(dtaLedger.results.find(row => row.execution_id === dta.id));
    const plan = object(dtaLedger.plans.find(row => row.id === dta.current_plan_revision_id));
    expect(result).toMatchObject({ status: 'verified', verification_passed: true, describes_both_fields: true });
    expect(plan.steps).toHaveLength(1);
    expect(object((plan.steps as unknown[])[0]).kind).toBe('answer');
    expect(dtaLedger.proposals.filter(row => row.execution_id === dta.id)).toHaveLength(1);
    const resultRead = await authenticatedRead(owner, `/api/executions/${String(dta.id)}/result?tenant_id=tenant_demo`);
    expect(resultRead.status).toBe(200);
    expect(object(resultRead.body).id).toBe(result.id);
    expect(dtaLedger.business_calls).toHaveLength(countBefore);
    expect(dtaLedger.operations.filter(row => row.instance_id === dta.id)).toHaveLength(0);
    await unchanged(owner, published.skill_version_id, 2);
    await audit(info, 'h3-legitimate-ordinary-dta-owner-feedback-no-sop-publication', { passed: true,
      session_id: dtaSession, instance_id: dta.id, material_id: dtaMaterial.id,
      source: dtaMaterial.material.source, business_dispatches_added: 0,
      result_id: result.id, plan_revision_id: plan.id, result_read_status: resultRead.status, ledger: dtaLedger });
    await audit(info, 'h3-complete-journey-group-terminal', { passed: true, ledger: dtaLedger,
      publication_successors: 1, real_provider_quality: false, actual_ui_and_composer: true });
  } finally { release('authority-late'); await context.close(); }
});
