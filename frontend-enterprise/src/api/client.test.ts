import { afterEach, expect, it, vi } from 'vitest';

import {
  clearEnterpriseAuthSession,
  setEnterpriseAuthSession,
  type EnterpriseAuthSession,
} from '../auth';
import { api, getRequestTenantId, LOGIN_TENANT_ID, streamPost } from './client';

afterEach(() => {
  clearEnterpriseAuthSession();
  vi.restoreAllMocks();
});

it('keeps the bearer token when a caller adds an idempotency header', async () => {
  setEnterpriseAuthSession({
    token: 'token-a',
    user: {
      id: 'user-a',
      tenant_id: 'tenant-a',
      username: 'member',
      role: 'member',
      membership_status: 'active',
      member_category_code: 'employee',
    },
  });
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
  );

  await api.postWithHeaders('/test', { value: 1 }, { 'Idempotency-Key': 'request-001' });

  expect(fetchMock).toHaveBeenCalledWith('/test', expect.objectContaining({
    headers: expect.objectContaining({
      Authorization: 'Bearer token-a',
      'Idempotency-Key': 'request-001',
    }),
  }));
});

it('reads the request tenant from the authenticated member instead of the login default', () => {
  const session: EnterpriseAuthSession = {
    token: 'signed-token',
    user: {
      id: 'user_1',
      tenant_id: 'tenant_from_auth_context',
      username: 'member',
      role: 'member',
      membership_status: 'active',
      member_category_code: 'employee',
    },
  };
  setEnterpriseAuthSession(session);
  expect(getRequestTenantId()).toBe('tenant_from_auth_context');
  expect(getRequestTenantId()).not.toBe(LOGIN_TENANT_ID);
});

it('rejects business requests without an authenticated enterprise context', () => {
  expect(() => getRequestTenantId()).toThrow('当前登录会话缺少企业上下文');
});

it('keeps UTF-8 and SSE events intact when network chunks split characters and delimiters', async () => {
  const utf8Content = `${String.fromCodePoint(0x4e2d, 0x6587)}🙂${String.fromCodePoint(0x5f00, 0x53d1, 0x8005, 0x5de5, 0x5177)}`;
  const body = [
    `event: stream_delta\ndata: ${JSON.stringify({ content: `${utf8Content}\n` })}\n\n`,
    `event: complete\ndata: ${JSON.stringify({ ok: true })}`,
  ].join('');
  const encoder = new TextEncoder();
  const encoded = encoder.encode(body);
  const emojiStart = body.indexOf('🙂');
  const delimiterStart = body.indexOf('\n\n');
  const cutOffsets = [
    encoder.encode(body.slice(0, emojiStart)).length + 1,
    encoder.encode(body.slice(0, delimiterStart + 1)).length,
    encoded.length - 1,
  ].sort((left, right) => left - right);
  const chunks: Uint8Array[] = [];
  let offset = 0;
  for (const end of cutOffsets) {
    chunks.push(encoded.slice(offset, end));
    offset = end;
  }
  chunks.push(encoded.slice(offset));

  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(new ReadableStream<Uint8Array>({
      start(controller) {
        chunks.forEach((chunk) => controller.enqueue(chunk));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
  );
  const events: Array<{ event: string; data: Record<string, unknown> }> = [];

  await streamPost('/stream-test', {}, (event) => events.push(event));

  expect(fetchMock).toHaveBeenCalledWith('/stream-test', expect.objectContaining({ method: 'POST' }));
  expect(events).toEqual([
    { event: 'stream_delta', data: { content: `${utf8Content}\n` } },
    { event: 'complete', data: { ok: true } },
  ]);
});
