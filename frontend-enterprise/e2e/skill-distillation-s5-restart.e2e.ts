/**
 * @Time       : 2026/08/13
 * @Author     : zhanglp8181
 * @File       : skill-distillation-s5-restart.e2e.ts
 * @CallChain  : Playwright → 全栈进程 A → SIGTERM → 全栈进程 B → publication Signal
 * @Description: 验证 Skill 提案在整个服务重启后仍能由所有者审批、发布并绑定原分身。
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { expect, test, type Page } from '@playwright/test';

const PORT = 5151;
const ORIGIN = `http://127.0.0.1:${PORT}`;
const RUNTIME_DIR = join(tmpdir(), 'gongge-skill-s5-restart-e2e');
const SERVER_SCRIPT = resolve('e2e/start_fullstack_server.py');
const PYTHON = resolve('../backend/.venv/bin/python');

type ServerHandle = {
  process: ChildProcessWithoutNullStreams;
  pid: number;
  output: string[];
};

async function waitForHealth(handle: ServerHandle): Promise<void> {
  /** 等待新 PID 通过 HTTP 健康检查，并在提前退出时附带服务端诊断。 */

  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    if (handle.process.exitCode !== null) {
      throw new Error(`fullstack process exited early:\n${handle.output.slice(-40).join('')}`);
    }
    try {
      if ((await fetch(`${ORIGIN}/api/health`)).ok) return;
    } catch {
      // 新进程尚未监听；继续有界轮询。
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 250));
  }
  throw new Error(`fullstack health timed out:\n${handle.output.slice(-40).join('')}`);
}

async function startServer(reuse: boolean): Promise<ServerHandle> {
  /** 启动使用固定持久运行目录的正式单端口应用。 */

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
  /** 优先 SIGTERM 等待服务退出，只在有界超时后使用 SIGKILL。 */

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

async function loginMember(page: Page): Promise<void> {
  /** 登录提案所属数字员工的所有者。 */

  await page.goto(`${ORIGIN}/enterprise/dashboard`);
  const status = await page.evaluate(async () => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant_id: 'tenant_demo', username: 'member', password: 'member' }),
    });
    const body = await response.json();
    if (response.ok) {
      localStorage.setItem('gongge_auth', JSON.stringify(body));
      localStorage.setItem('gongge_onboarding_guide_seen', '1');
      localStorage.setItem('gongge_quick_start_guide_seen', '1');
      localStorage.setItem('gongge_enterprise_agent_scope', 'agent_e2e_member_employee');
    }
    return response.status;
  });
  expect(status).toBe(200);
}

test('S5 整个服务重启后从持久提案和 Attention 恢复发布', async ({ page }) => {
  /** 在不同 PID 间审批同一提案，证明恢复不依赖进程内 Agent 或缓存。 */

  test.setTimeout(180_000);
  await rm(RUNTIME_DIR, { recursive: true, force: true });
  let server: ServerHandle | undefined;
  try {
    server = await startServer(false);
    const firstPid = server.pid;
    await loginMember(page);
    const executionId = await page.evaluate(async () => {
      const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
      const headers = { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' };
      const session = await fetch('/api/chat/sessions', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          agent_id: 'agent_e2e_member_employee',
          title: 'S5 真实进程重启恢复',
          origin: 'owned',
        }),
      }).then((response) => response.json()) as { id: string };
      await fetch('/api/chat/stream', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          session_id: session.id,
          agent_id: 'agent_e2e_member_employee',
          client_turn_id: 'turn_s5_process_restart',
          message: 'S5创建Skill：跨进程恢复并提交退款证据复核方法',
          channel: 'web',
        }),
      }).then((response) => response.text());
      const events = await fetch(
        `/api/chat/sessions/${session.id}/events?tenant_id=tenant_demo`,
        { headers },
      ).then((response) => response.json()) as Array<{
        event_type: string;
        data?: Record<string, unknown>;
      }>;
      return String(
        events.find((event) => event.event_type === 'dynamic_task_delegated')?.data?.execution_id || '',
      );
    });
    expect(executionId).not.toBe('');
    await page.goto(`${ORIGIN}/enterprise/work-items`);
    await expect(page.getByRole('button', { name: /审核并发布当前分身提出的 Skill/ })).toBeVisible({
      timeout: 30_000,
    });

    await stopServer(server);
    server = await startServer(true);
    expect(server.pid).not.toBe(firstPid);

    await loginMember(page);
    await page.goto(`${ORIGIN}/enterprise/work-items`);
    await page.getByRole('button', { name: /审核并发布当前分身提出的 Skill/ }).click();
    await expect(page.getByLabel('待审核 Skill 提案')).toContainText('S5-PROPOSAL-GUIDANCE');
    await page.getByRole('dialog').getByRole('button', { name: '批准并发布' }).click();
    await expect.poll(async () => page.evaluate(async (id) => {
      const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
      return fetch(`/api/executions/${id}?tenant_id=tenant_demo`, {
        headers: { Authorization: `Bearer ${auth.token}` },
      }).then((response) => response.json());
    }, executionId), { timeout: 45_000 }).toMatchObject({ status: 'succeeded' });
    const skills = await page.evaluate(async () => {
      const auth = JSON.parse(localStorage.getItem('gongge_auth') || '{}') as { token?: string };
      return fetch(
        '/api/enterprise/general-skills?tenant_id=tenant_demo&agent_id=agent_e2e_member_employee',
        { headers: { Authorization: `Bearer ${auth.token}` } },
      ).then((response) => response.json()) as Promise<Array<{ name: string; status: string }>>;
    });
    expect(skills).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: 's5-refund-evidence-review', status: 'published' }),
    ]));
  } finally {
    if (server) await stopServer(server);
    await rm(RUNTIME_DIR, { recursive: true, force: true });
  }
});
