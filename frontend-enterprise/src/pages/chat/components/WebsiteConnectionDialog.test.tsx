import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { VerticalAbilityConnectionCatalog } from '@/types/verticalAbilityConnections';
import WebsiteConnectionDialog from './WebsiteConnectionDialog';

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('@/api/client', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/client')>(), api: client,
}));
vi.mock('@/components/ui/app-toast', () => ({
  notify: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}));

const catalog: VerticalAbilityConnectionCatalog = {
  platforms: [{
    platform_id: 'xiaohongshu', platform_name: '小红书', description: '读取笔记和评论',
    capability_ids: ['intelligence.xiaohongshu_read'], auth_methods: ['browser_session'],
    auth_method_labels: { browser_session: '浏览器会话' }, fields_by_method: {},
    browser_transports: [
      { transport: 'local_loopback', label: '本机连接', description: '浏览器与后端在同机' },
      { transport: 'browser_relay', label: '宿主机连接', description: '后端在远端' },
    ],
    implementation_status: 'available', implementation_message: '用户确认设备后读取',
    status: 'not_connected', status_message: '尚未连接', connection_ids: [],
  }],
  connections: [],
};

beforeEach(() => { vi.clearAllMocks(); });

describe('用户确认浏览器设备', () => {
  it.each(['local_loopback', 'browser_relay'] as const)('%s 创建本人设备配对而不是借服务端浏览器', async (transport) => {
    const user = userEvent.setup();
    const pairing = {
      session_id: 'pairing_fixture', platform_id: 'xiaohongshu', account_label: '默认账号',
      transport, status: 'pending', pairing_token: 'fixture-only-pairing-token',
      pairing_token_available: true, message: '请在自己的设备确认',
    };
    client.get.mockImplementation(async (path: string) => path.includes('/pairings/') ? pairing : catalog);
    client.post.mockResolvedValue(pairing);
    render(<I18nProvider><WebsiteConnectionDialog open onOpenChange={vi.fn()} highlightPlatformId="xiaohongshu" /></I18nProvider>);
    await user.click(await screen.findByRole('radio', {
      name: transport === 'local_loopback' ? /本机连接/ : /宿主机连接/,
    }));
    await user.click(screen.getByRole('button', {
      name: transport === 'local_loopback' ? '连接本机浏览器' : '生成宿主机配对',
    }));
    await waitFor(() => expect(client.post).toHaveBeenCalledWith(
      '/api/chat/vertical-ability-connections/browser-relay/pairings', {
        platform_id: 'xiaohongshu', account_label: '默认账号', expected_revision: 0, transport,
      },
    ));
    expect(await screen.findByText('fixture-only-pairing-token')).toBeVisible();
    expect(screen.getByText(/browser-relay-connector.exe/)).toBeVisible();
    expect(JSON.stringify(localStorage)).not.toContain('fixture-only-pairing-token');
    expect(screen.queryByLabelText('密码')).not.toBeInTheDocument();
    const otherMode = transport === 'local_loopback' ? /宿主机连接/ : /本机连接/;
    await user.click(screen.getByRole('radio', { name: otherMode }));
    expect(screen.queryByText('fixture-only-pairing-token')).not.toBeInTheDocument();
  });
});
