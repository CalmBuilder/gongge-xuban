/**
 * @Time       : 2026/08/13
 * @Author     : zhanglp8181
 * @File       : skill-distillation-s4-restart.e2e.ts
 * @CallChain  : Playwright → 全栈进程 A → SIGTERM → 全栈进程 B → 持久 Attention/Signal
 * @Description: 验证整个服务进程重启后，Skill 动态任务从同一 SQLite 账本恢复并完成受管交付。
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { expect, test, type Page } from '@playwright/test';

const PORT = 5150;
const ORIGIN = `http://127.0.0.1:${PORT}`;
const RUNTIME_DIR = join(tmpdir(), 'gongge-skill-s4-restart-e2e');
const SERVER_SCRIPT = resolve('e2e/start_fullstack_server.py');
const PYTHON = resolve('../backend/.venv/bin/python');
const SKILL_NAME = 's4-code-guidance';
const SKILL_MARKDOWN = [
  '---',
  `name: ${SKILL_NAME}`,
  'description: S4 代码交付必须先读、审批写入、隔离回归、审批提交并形成证据。',
  'allowed-tools:',
  '  - workspace.refund.read',
  '  - workspace.refund.apply-set',
  '  - workspace.refund.check',
  '  - workspace.refund.commit',
  '---',
  '# S4 restart-safe code delivery guidance',
  'S4-CODE-FULL-GUIDANCE：代码交付必须使用受管工作区工具，不得执行 Skill 包脚本。',
  '',
].join('\n');

type ServerHandle = {
  process: ChildProcessWithoutNullStreams;
  pid: number;
  output: string[];
};

async function waitForHealth(handle: ServerHandle): Promise<void> {
  /** 等待指定子进程真正通过 HTTP 健康检查，提前退出时返回诊断尾部。 */

  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    if (handle.process.exitCode !== null) {
      throw new Error(`fullstack process exited early:\n${handle.output.slice(-40).join('')}`);
    }
    try {
      const response = await fetch(`${ORIGIN}/api/health`);
      if (response.ok) return;
    } catch {
      // 进程尚未监听；继续有界轮询。
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 250));
  }
  throw new Error(`fullstack health timed out:\n${handle.output.slice(-40).join('')}`);
}

async function startServer(reuse: boolean): Promise<ServerHandle> {
  /** 用独立 PID 启动正式单端口应用，重启阶段只切换运行目录复用开关。 */

  const child = spawn(PYTHON, [SERVER_SCRIPT], {
    cwd: resolve('.'),
    env: {
      ...process.env,
      FULLSTACK_E2E_PORT: String(PORT),
      FULLSTACK_E2E_RUNTIME_DIR: RUNTIME_DIR,
      FULLSTACK_E2E_REUSE_RUNTIME: reuse ? '1' : '0',
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  if (!child.pid) throw new Error('fullstack process did not expose a PID');
  const output: string[] = [];
  child.stdout.on('data', (chunk: Buffer) => output.push(chunk.toString()));
  child.stderr.on('data', (chunk: Buffer) => output.push(chunk.toString()));
  const handle = { process: child, pid: child.pid, output };
  await waitForHealth(handle);
  return handle;
}

async function stopServer(handle: ServerHandle): Promise<void> {
  /** 发送 SIGTERM 并等待 PID 完全退出，超时才升级为 SIGKILL。 */

  if (handle.process.exitCode !== null) return;
  handle.process.kill('SIGTERM');
  const exited = new Promise<void>((resolveExit) => handle.process.once('exit', () => resolveExit()));
  const timeout = new Promise<'timeout'>((resolveTimeout) => {
    setTimeout(() => resolveTimeout('timeout'), 10_000);
  });
  if (await Promise.race([exited, timeout]) === 'timeout') {
    handle.process.kill('SIGKILL');
    await new Promise<void>((resolveExit) => handle.process.once('exit', () => resolveExit()));
  }
}

async function login(page: Page, username: 'member' | 'admin'): Promise<void> {
  /** 经真实认证 API 登录并保留同源浏览器状态。 */

  await page.goto(`${ORIGIN}/enterprise/dashboard`);
  const status = await page.evaluate(async (name) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: name, password: name }),
    });
    const body = await response.json();
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
      localStorage.setItem('gongge_enterprise_agent_scope', 'agent_e2e_member_employee');
    }
    return response.status;
  }, username);
  expect(status).toBe(200);
}

async function importGuidance(page: Page): Promise<void> {
  /** 从正式管理 UI 导入固定代码指导 Skill。 */

  await page.goto(`${ORIGIN}/enterprise/general-skills`);
  await page.getByRole('button', { name: /新增/ }).click();
  await page.getByRole('menuitem', { name: '安全导入 Skill' }).click();
  const dialog = page.getByRole('dialog', { name: '安全导入 Skill 包' });
  await dialog.locator('input[type="file"]').setInputFiles({
    name: 'SKILL.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from(SKILL_MARKDOWN),
  });
  await dialog.getByRole('button', { name: '生成安全预览' }).click();
  await expect(dialog.getByText(SKILL_NAME, { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '固定版本并绑定' }).click();
  await expect(dialog).not.toBeVisible();
}

test('S4 整个服务进程重启后从持久 Attention 和 Signal 恢复完成代码交付', async ({ page }) => {
  /** 在不同 PID 间完成同一 Execution，证明恢复不依赖进程内 Agent、锁或缓存。 */

  test.setTimeout(240_000);
  await rm(RUNTIME_DIR, { recursive: true, force: true });
  let server: ServerHandle | undefined;
  try {
    server = await startServer(false);
    const firstPid = server.pid;
    await login(page, 'member');
    await importGuidance(page);
    const started = await page.evaluate(async () => {
      const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
      const sessionResponse = await fetch('/api/chat/sessions', {
        method: 'POST',
        headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          agent_id: 'agent_e2e_member_employee',
          title: 'S4 真实进程重启恢复',
          origin: 'owned',
        }),
      });
      const session = await sessionResponse.json() as { id: string };
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          session_id: session.id,
          agent_id: 'agent_e2e_member_employee',
          client_turn_id: 'turn_s4_process_restart',
          message: 'S4代码：在真实进程重启后继续完成高金额退款审批变更',
          channel: 'web',
        }),
      });
      const body = await response.text();
      return {
        status: response.status,
        sessionId: session.id,
        executionId: body.match(/"execution_id":\s*"([^"]+)"/)?.[1] || '',
      };
    });
    expect(started.status).toBe(200);
    expect(started.executionId).not.toBe('');
    await login(page, 'admin');
    await page.goto(`${ORIGIN}/enterprise/work-items`);
    await expect(page.getByRole('button', { name: /批准受管代码工作区执行检查/ }).first()).toBeVisible({
      timeout: 30_000,
    });

    await stopServer(server);
    server = await startServer(true);
    expect(server.pid).not.toBe(firstPid);

    await login(page, 'admin');
    for (const title of [
      '批准受管代码工作区执行检查',
      '批准受管代码工作区变更',
      '批准受管代码工作区执行检查',
      '批准受管代码工作区执行检查',
      '批准受管代码工作区变更',
    ]) {
      await page.goto(`${ORIGIN}/enterprise/work-items`);
      const card = page.getByRole('button', { name: new RegExp(title) }).first();
      await expect(card).toBeVisible({ timeout: 30_000 });
      await card.click();
      await page.getByRole('dialog').getByRole('button', { name: '仅批准本次操作' }).click();
    }

    await login(page, 'member');
    await expect.poll(async () => page.evaluate(async (executionId) => {
      const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
      const response = await fetch(`/api/executions/${executionId}?tenant_id=tenant_demo`, {
        headers: { Authorization: `Bearer ${auth.token}` },
      });
      return response.json();
    }, started.executionId), { timeout: 45_000 }).toMatchObject({
      status: 'succeeded',
      usage: { tool_calls: 6 },
    });
    await page.goto(`${ORIGIN}/workspace/chat/${started.sessionId}`);
    await expect(page.getByRole('main').getByText(/S4-CODE-DELIVERY-SUCCESS/)).toBeVisible({
      timeout: 30_000,
    });
  } finally {
    if (server) await stopServer(server);
    await rm(RUNTIME_DIR, { recursive: true, force: true });
  }
});
