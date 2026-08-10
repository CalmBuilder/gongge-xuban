/**
 * @Time       : 2026/08/11
 * @Author     : zhanglp8181
 * @File       : scheduled-dynamic-task.fullstack.e2e.ts
 * @CallChain  : Playwright Chromium → Schedule API/worker → AgentLoop → DynamicTaskAgent/Attention
 * @Description: 验证管理端创建和触发 Schedule 后，统一动态 Execution 进入可恢复等待态并可回看会话。
 */

import { expect, test, type Page } from '@playwright/test';

type ScheduleRun = {
  id: string;
  execution_id?: string;
  session_id?: string;
  source_kind: string;
  source_ref: string;
  status: string;
};

async function login(page: Page) {
  /** 通过真实认证 API 建立管理员会话，并固定本场景的数字员工范围。 */

  await page.goto('/enterprise/scheduled-tasks');
  const status = await page.evaluate(async () => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'admin', password: 'admin' }),
    });
    const body = await response.json();
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_enterprise_agent_scope', 'agent_e2e_employee');
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
    }
    return response.status;
  });
  expect(status).toBe(200);
}

async function newestRun(page: Page, taskId: string): Promise<ScheduleRun | undefined> {
  /** 经真实鉴权分页 API 读取本任务最新运行，不访问测试数据库或内部 Agent。 */

  return page.evaluate(async (id) => {
    const session = JSON.parse(localStorage.getItem('gongge_auth') || '{}');
    const response = await fetch(
      `/api/enterprise/scheduled-tasks/runs/page?tenant_id=tenant_demo&task_id=${id}&page=1&page_size=10`,
      { headers: { Authorization: `Bearer ${session.token}` } },
    );
    if (!response.ok) throw new Error(`run page failed: ${response.status}`);
    const body = await response.json();
    return body.items[0];
  }, taskId);
}

async function hasScheduleAttention(page: Page, executionId: string): Promise<boolean> {
  /** 轮询统一 Attention API，证明后台 Signal 已实际推进到人工等待点。 */

  return page.evaluate(async (id) => {
    const session = JSON.parse(localStorage.getItem('gongge_auth') || '{}');
    const response = await fetch(
      '/api/attention-items?tenant_id=tenant_demo&view=active&page=1&page_size=100',
      { headers: { Authorization: `Bearer ${session.token}` } },
    );
    if (!response.ok) throw new Error(`attention page failed: ${response.status}`);
    const body = await response.json();
    return body.items.some((item: { execution_id?: string; title?: string }) => (
      item.execution_id === id && item.title === '确认合同范围'
    ));
  }, executionId);
}

test('真实 Chromium 调度动态任务形成独立执行并进入统一待处理', async ({ page }) => {
  const title = 'E2E 合同范围巡检';
  await login(page);
  await page.goto('/enterprise/scheduled-tasks');
  await page.getByRole('button', { name: '新增任务' }).click();

  await page.getByLabel('任务名称').fill(title);
  await page.getByLabel('每次执行时交给员工的任务').fill(
    '请先确认本次合同巡检范围，再生成对应的风险结果。',
  );
  const createResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/enterprise/scheduled-tasks'
    && response.request().method() === 'POST'
  ));
  await page.getByRole('button', { name: '保存', exact: true }).click();
  const createdResponse = await createResponse;
  expect(createdResponse.status()).toBe(200);
  const task = await createdResponse.json();
  expect(task.agent_id).toBe('agent_e2e_employee');
  await expect(page).toHaveURL(new RegExp(`/scheduled-tasks/${task.id}/edit$`));

  await page.getByRole('button', { name: '返回定时任务' }).click();
  await page.reload();
  const taskSection = page.getByRole('region', { name: '任务列表' });
  await expect(taskSection.getByRole('row')).toHaveCount(11);
  const taskRow = taskSection.getByRole('row').filter({ hasText: title });
  if (await taskRow.count() === 0) {
    const nextPage = taskSection.getByRole('button', { name: '下一页' });
    await nextPage.click();
    await expect(nextPage).toBeDisabled();
  }
  await expect(taskRow).toBeVisible();
  await taskRow.getByRole('button', { name: '操作' }).click();
  const runResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname.endsWith(`/scheduled-tasks/${task.id}/run-now`)
    && response.request().method() === 'POST'
  ));
  await page.getByRole('menuitem', { name: '立即执行' }).click();
  const startedResponse = await runResponse;
  expect(startedResponse.status()).toBe(200);
  const started = await startedResponse.json() as ScheduleRun;
  expect(started.session_id).toBeTruthy();
  expect(started.source_kind).toBe('manual');
  expect(started.source_ref).toContain(`scheduled-task:${task.id}:manual:`);

  await expect.poll(
    () => newestRun(page, task.id),
    { timeout: 30_000, intervals: [250, 500, 1_000] },
  ).toMatchObject({
    id: started.id,
    source_kind: 'manual',
    status: 'waiting',
    execution_id: expect.stringMatching(/^sopinst_/),
  });
  const settledRun = await newestRun(page, task.id);
  expect(settledRun?.execution_id).toBeTruthy();
  await expect.poll(
    () => hasScheduleAttention(page, settledRun?.execution_id || ''),
    { timeout: 30_000, intervals: [250, 500, 1_000] },
  ).toBe(true);

  await page.reload();
  const runSection = page.getByRole('region', { name: '执行记录' });
  await runSection.getByRole('tab', { name: '待完成' }).click();
  await expect(runSection.locator('span:visible', { hasText: '等待处理' }).first()).toBeVisible();
  await expect(
    runSection.locator('button:visible', { hasText: '查看会话' }).first(),
  ).toBeEnabled();

  await page.goto('/enterprise/work-items');
  await expect(page.getByRole('button', { name: /确认合同范围/ })).toBeVisible();
  await page.getByRole('button', { name: /确认合同范围/ }).click();
  await expect(page.getByRole('dialog')).toContainText('请选择本次需要巡检的合同范围');
});
