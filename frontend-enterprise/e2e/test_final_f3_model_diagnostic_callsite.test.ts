/**
 * @Time       : 2026/10/14
 * @Author     : zhanglp8181
 * @File       : test_final_f3_model_diagnostic_callsite.test.ts
 * @CallChain  : Vitest → 实际verifyModel页面调用点 → 连接诊断失败派发边界
 * @Description: 固定调用点失败时不进入页面回调或派发动态模型预检。
 */
import { describe, expect as vitestExpect, it, vi } from 'vitest';
import type { Page, TestInfo } from '@playwright/test';
import type { BusinessCase, Initial } from './final-f3-jrn08-helpers';

vi.mock('@playwright/test', () => ({
  expect(actual: unknown) {
    return {
      toBe(expected: unknown) {
        if (actual !== expected) throw new Error('F3_CALLSITE_EXPECTATION_FAILED');
      },
      toMatch(expected: RegExp) {
        if (typeof actual !== 'string' || !expected.test(actual)) {
          throw new Error('F3_CALLSITE_EXPECTATION_FAILED');
        }
      },
      toHaveCount(expected: number) {
        const candidate = typeof actual === 'object' && actual !== null
          ? (actual as { count?: unknown }).count : undefined;
        if (candidate !== expected) throw new Error('F3_CALLSITE_EXPECTATION_FAILED');
      },
      toBeVisible() {
        const candidate = typeof actual === 'object' && actual !== null
          ? (actual as { visible?: unknown }).visible : undefined;
        if (candidate !== true) throw new Error('F3_CALLSITE_EXPECTATION_FAILED');
      },
    };
  },
}));

function businessCase(message: string): BusinessCase {
  /** 为真实调用点构造仅供测试类型检查的合成业务标签。 */
  return {
    message,
    confirmation_message: `${message}确认`,
    expected_business: {
      namespace: 'synthetic', object_id: 'model-diagnostic', product_name: 'SYNTHETIC',
      quantity: 1, customer: 'synthetic', due_date: '2026-10-15',
    },
  };
}

function initialValue(): Initial {
  /** 填满验收输入类型，但只给verifyModel读取的本人、租户和模型字段赋合成值。 */
  const owner = {
    credentials: { username: 'synthetic-owner', password: 'synthetic-password' },
    expected: {
      id: 'synthetic-owner-id', tenant_id: 'synthetic-tenant', username: 'synthetic-owner',
      role: 'admin' as const, membership_status: 'active' as const,
    },
  };
  const reviewer = {
    credentials: { username: 'synthetic-reviewer', password: 'synthetic-password' },
    expected: {
      id: 'synthetic-reviewer-id', tenant_id: 'synthetic-tenant', username: 'synthetic-reviewer',
      role: 'admin' as const, membership_status: 'active' as const,
    },
  };
  const agent = { id: 'synthetic-agent', tenant_id: 'synthetic-tenant', name: '合成员工', gallery_label: '合成员工' };
  const cases = {
    normal: businessCase('normal'),
    cancel: businessCase('cancel'),
    safe_decline: businessCase('safe-decline'),
    unknown: businessCase('unknown'),
  };
  return {
    owner, reviewer, agents: { SOP: agent, DTA: agent },
    model: { id: 'synthetic-model-id', name: '合成模型' },
    cases: { SOP: cases, DTA: cases }, knowledge: { id: 'synthetic-knowledge' },
    sop_knowledge: { id: 'synthetic-sop-knowledge' },
    date_labels: { timezone: 'UTC', expected_due_date: '2026-10-15' },
  };
}

describe('verifyModel真实调用点失败门控', () => {
  it('全量回归复用冻结预检，独立记忆诊断仍执行现场预检', async () => {
    /** 防止同一全量运行生成未关联的重复诊断身份，同时保留单案诊断覆盖。 */
    const { shouldRunMemoryModelPreflight } = await import('./final-f3-memory-helpers');
    vitestExpect(shouldRunMemoryModelPreflight(true, true)).toBe(false);
    vitestExpect(shouldRunMemoryModelPreflight(true, false)).toBe(true);
    vitestExpect(shouldRunMemoryModelPreflight(false, false)).toBe(false);
  });

  it('HTTP 200且success=false时不触达对话回调或动态预检POST', async () => {
    /** 驱动导出的verifyModel本体，确保失败诊断在实际页面派发点前短路。 */
    const { login, verifyModel } = await import('./final-f3-jrn08-helpers');
    let dialogAccessCount = 0;
    let currentUrl = 'http://localhost/';
    const genericLocator = {
      isVisible: vi.fn(async () => false),
      click: vi.fn(async () => undefined),
      fill: vi.fn(async () => undefined),
      waitFor: vi.fn(async () => undefined),
    };
    const modelButton = { click: vi.fn(async () => undefined) };
    const modelRow = {
      count: 1,
      getByRole: vi.fn(() => modelButton),
    };
    const loginResponse = {
      url: () => 'http://localhost/api/auth/login',
      request: () => ({ url: () => 'http://localhost/api/auth/login', method: () => 'POST' }),
      status: () => 200,
      json: vi.fn(async () => ({ token: 'synthetic-token', user: initialValue().owner.expected })),
    };
    const contextResponse = {
      url: () => 'http://localhost/api/auth/context',
      request: () => ({ url: () => 'http://localhost/api/auth/context', method: () => 'GET' }),
      status: () => 200,
      json: vi.fn(async () => ({ tenant: { id: 'synthetic-tenant' },
        member: initialValue().owner.expected, is_administrator: true })),
    };
    const diagnosticResponse = {
      url: () => 'http://localhost/api/enterprise/model-configs/synthetic-model-id/test',
      request: () => ({
        url: () => 'http://localhost/api/enterprise/model-configs/synthetic-model-id/test',
        method: () => 'POST',
      }),
      status: () => 200,
      headers: () => ({
        'content-length': '128',
        'x-f3-diagnostic-invocation-sha256': 'a'.repeat(64),
        'x-f3-diagnostic-config-fingerprint-sha256': 'b'.repeat(64),
      }),
      json: vi.fn(async () => ({ success: false, http_status: 503, error_code: 'PROVIDER_UNAVAILABLE' })),
    };
    const dynamicPreflightPost = vi.fn(async () => ({
      status: () => 200,
      headers: () => ({
        'content-length': '32',
        'x-f3-diagnostic-invocation-sha256': 'c'.repeat(64),
        'x-f3-diagnostic-config-fingerprint-sha256': 'b'.repeat(64),
      }),
      json: vi.fn(async () => ({ success: true, status: 'ready', checksum: 'c'.repeat(64) })),
      dispose: vi.fn(async () => undefined),
    }));
    const responses = [loginResponse, contextResponse, diagnosticResponse];
    const pageStub = {
      goto: vi.fn(async (path: string) => { currentUrl = new URL(path, currentUrl).toString(); }),
      url: vi.fn(() => currentUrl),
      getByRole: vi.fn((role: string, options?: { name?: string | RegExp }) => {
        if (role === 'row') return { filter: vi.fn(() => modelRow) };
        if (role === 'dialog' && options?.name === '模型连接诊断') {
          dialogAccessCount += 1;
          return { visible: true,
            waitFor: vi.fn(async () => undefined),
            isVisible: vi.fn(async () => true),
            getByRole: vi.fn(() => ({ click: vi.fn(async () => undefined) })) };
        }
        return genericLocator;
      }),
      getByLabel: vi.fn(() => genericLocator),
      waitForResponse: vi.fn(async (predicate: (response: unknown) => boolean) => {
        const match = responses.find(response => predicate(response));
        if (!match) throw new Error('EXPECTED_ORIGINAL_API_RESPONSE');
        return match;
      }),
      request: { post: dynamicPreflightPost },
    } as unknown as Page;
    const infoStub = { attach: vi.fn(async () => undefined) } as unknown as TestInfo;
    const initial = initialValue();

    await login(pageStub, initial.owner);
    await vitestExpect(verifyModel(pageStub, initial, infoStub)).rejects.toThrow();
    vitestExpect(dynamicPreflightPost).not.toHaveBeenCalled();
    vitestExpect(dialogAccessCount).toBe(0);
  });
});
