/**
 * @Time       : 2026/09/03 16:50
 * @Author     : zhanglp8181
 * @File       : WebsiteConnectionDialog.tsx
 * @CallChain  : 对话框能力包菜单 → 网站连接中心 → 用户连接 API → 平台适配器
 * @Description: 以卡片和平台专属认证方式维护全网情报员的网站连接，不收集通用用户名密码。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import { ApiError, api } from '@/api/client';
import ProductIcon, { type ProductIconName } from '@/components/ProductIcon';
import { Badge, Card, CardContent, CardHeader, CardTitle, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, Input, Label, Textarea } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';
import type {
  VerticalAbilityConnection,
  VerticalAbilityConnectionAuthMethod,
  VerticalAbilityConnectionCatalog,
  VerticalAbilityConnectionField,
  VerticalAbilityConnectionPlatform,
  VerticalAbilityConnectionTransport,
  VerticalAbilityRelayPairing,
  VerticalAbilityRelayPairingCreate,
} from '@/types/verticalAbilityConnections';


const CONNECTION_ENDPOINT = '/api/chat/vertical-ability-connections';
const RELAY_ENDPOINT = `${CONNECTION_ENDPOINT}/browser-relay`;

const METHOD_DESCRIPTIONS: Record<VerticalAbilityConnectionAuthMethod, string> = {
  api_token: '粘贴网站提供的访问 Token，仅授予读取所需范围。',
  cookie: '先在官方站点登录，再用 Cookie-Editor 主动导出同域 Cookie。',
  browser_session: '复用本机已经登录的 Chrome/Edge 会话，不把密码交给平台。',
  oauth: '跳转网站官方授权页，授权范围由网站控制。',
};

const CONNECTION_STATUS_LABELS: Record<VerticalAbilityConnectionPlatform['status'], string> = {
  not_connected: '未连接',
  pending: '待验证',
  connected: '已连接',
  needs_reauth: '需要重新连接',
  revoked: '已撤销',
};

const CONNECTION_STATUS_CLASSES: Record<VerticalAbilityConnectionPlatform['status'], string> = {
  not_connected: 'border-[#d9dee9] bg-[#f7f8fb] text-[#69738a]',
  pending: 'border-[#f3d28b] bg-[#fff8e8] text-[#8a4b00]',
  connected: 'border-[#b7e4c7] bg-[#f2fbf5] text-[#237a42]',
  needs_reauth: 'border-[#ffd0d0] bg-[#fff6f6] text-[#b42318]',
  revoked: 'border-[#d9dee9] bg-[#f7f8fb] text-[#69738a]',
};

const PLATFORM_ICONS: Record<string, ProductIconName> = {
  github: 'code',
  bilibili: 'desktop',
  facebook: 'user',
  instagram: 'image',
  linkedin: 'user',
  reddit: 'spark',
  twitter: 'chat',
  xiaohongshu: 'spark',
  xueqiu: 'database',
};

type WebsiteConnectionDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  highlightPlatformId?: string;
};

export default function WebsiteConnectionDialog({
  open,
  onOpenChange,
  highlightPlatformId,
}: WebsiteConnectionDialogProps) {
  const [catalog, setCatalog] = useState<VerticalAbilityConnectionCatalog | null>(null);
  const [selectedPlatformId, setSelectedPlatformId] = useState(highlightPlatformId || 'github');
  const [authMethod, setAuthMethod] = useState<VerticalAbilityConnectionAuthMethod>('api_token');
  const [browserTransport, setBrowserTransport] = useState<VerticalAbilityConnectionTransport>(defaultBrowserTransport());
  const [accountLabel, setAccountLabel] = useState('默认账号');
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [relayPairing, setRelayPairing] = useState<VerticalAbilityRelayPairing | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadCatalog = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await api.get<VerticalAbilityConnectionCatalog>(CONNECTION_ENDPOINT);
      setCatalog(next);
      setSelectedPlatformId((current) => {
        if (highlightPlatformId && next.platforms.some((item) => item.platform_id === highlightPlatformId)) {
          return highlightPlatformId;
        }
        return next.platforms.some((item) => item.platform_id === current)
          ? current
          : next.platforms[0]?.platform_id || '';
      });
    } catch (nextError) {
      const message = displayError(nextError, '网站连接目录加载失败');
      setError(message);
      notify.error(message);
    } finally {
      setLoading(false);
    }
  }, [highlightPlatformId]);

  useEffect(() => {
    if (open) void loadCatalog();
  }, [loadCatalog, open]);

  const activePlatform = useMemo(
    () => catalog?.platforms.find((item) => item.platform_id === selectedPlatformId) || null,
    [catalog?.platforms, selectedPlatformId],
  );
  const activeConnection = useMemo(
    () => catalog?.connections.find((item) => item.platform_id === selectedPlatformId) || null,
    [catalog?.connections, selectedPlatformId],
  );
  const expectedRevision = activeConnection?.status === 'revoked' ? 0 : activeConnection?.revision || 0;
  const methodFields = activePlatform?.fields_by_method[authMethod] || [];
  const busy = loading || saving || verifying || revoking;

  useEffect(() => {
    if (!activePlatform) return;
    const nextMethod = activeConnection?.auth_method && activePlatform.auth_methods.includes(activeConnection.auth_method)
      ? activeConnection.auth_method
      : activePlatform.auth_methods[0];
    if (nextMethod) setAuthMethod(nextMethod);
    setAccountLabel(activeConnection?.account_label || '默认账号');
    const availableTransports = activePlatform.browser_transports.map((item) => item.transport);
    const preferredTransport = defaultBrowserTransport();
    setBrowserTransport(
      activeConnection?.transport
      || (availableTransports.includes(preferredTransport) ? preferredTransport : availableTransports[0])
      || preferredTransport,
    );
    setCredentials({});
  }, [activeConnection, activePlatform]);

  useEffect(() => {
    const sessionId = relayPairing?.session_id;
    const pairingStatus = relayPairing?.status;
    if (!open || !sessionId || pairingStatus === 'connected' || pairingStatus === 'revoked' || pairingStatus === 'expired') {
      return undefined;
    }
    let disposed = false;
    const poll = async () => {
      try {
        const next = await api.get<VerticalAbilityRelayPairing>(`${RELAY_ENDPOINT}/pairings/${encodeURIComponent(sessionId)}`);
        if (disposed) return;
        setRelayPairing((current) => ({
          ...next,
          pairing_token: next.pairing_token || (current?.session_id === next.session_id ? current.pairing_token : null),
          pairing_token_available: Boolean(next.pairing_token || (current?.session_id === next.session_id && current.pairing_token)),
        }));
        if (next.status === 'connected') {
          await loadCatalog();
          notify.success(`${next.account_label} · ${transportLabel(next.transport)}已连接`);
        }
      } catch (nextError) {
        if (!disposed) setError(displayError(nextError, 'Relay 配对状态读取失败'));
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [loadCatalog, open, relayPairing?.session_id, relayPairing?.status]);

  const refreshAfterMutation = async () => {
    await loadCatalog();
  };

  const handleSave = async () => {
    if (!activePlatform) return;
    const missing = methodFields.find((field) => field.required && !credentials[field.key]?.trim());
    if (missing) {
      notify.warning(`请填写${missing.label}`);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.post<VerticalAbilityConnection>(CONNECTION_ENDPOINT, {
        platform_id: activePlatform.platform_id,
        account_label: accountLabel.trim() || '默认账号',
        auth_method: authMethod,
        credentials: compactCredentials(credentials),
        expected_revision: expectedRevision,
      });
      await refreshAfterMutation();
      notify.success(`${activePlatform.platform_name} 连接信息已保存，请继续验证连接`);
    } catch (nextError) {
      const message = displayError(nextError, '网站连接保存失败');
      setError(message);
      notify.error(message);
    } finally {
      setSaving(false);
    }
  };

  const handleBrowserConnect = async () => {
    if (!activePlatform) return;
    setSaving(true);
    setError(null);
    try {
      const payload: VerticalAbilityRelayPairingCreate = {
        platform_id: activePlatform.platform_id,
        account_label: accountLabel.trim() || '默认账号',
        expected_revision: expectedRevision,
        transport: browserTransport,
      };
      const next = await api.post<VerticalAbilityRelayPairing>(`${RELAY_ENDPOINT}/pairings`, payload);
      setRelayPairing(next);
      notify.success(browserTransport === 'browser_relay'
        ? '已生成 Relay 配对令牌，请在浏览器所在宿主机启动连接器'
        : '已生成本机配对令牌，请在部署本平台的设备上启动连接器');
    } catch (nextError) {
      const message = displayError(nextError, '浏览器会话连接失败');
      setError(message);
      notify.error(message);
    } finally {
      setSaving(false);
    }
  };

  const handleCopyPairingToken = async () => {
    const token = relayPairing?.pairing_token;
    if (!token) return;
    await copyText(token, '配对令牌已复制');
  };

  const handleVerify = async () => {
    if (!activeConnection) {
      notify.warning('请先保存连接信息或连接已登录浏览器');
      return;
    }
    setVerifying(true);
    setError(null);
    try {
      const next = await api.post<VerticalAbilityConnection>(
        `${CONNECTION_ENDPOINT}/${encodeURIComponent(activeConnection.id)}/verify`,
      );
      await refreshAfterMutation();
      notify.success(next.message || `${activeConnection.platform_name} 连接验证成功`);
    } catch (nextError) {
      const message = displayError(nextError, '网站连接验证失败');
      setError(message);
      notify.error(message);
    } finally {
      setVerifying(false);
    }
  };

  const handleRevoke = async () => {
    if (!activeConnection) return;
    setRevoking(true);
    setError(null);
    try {
      await api.delete<VerticalAbilityConnection>(
        `${CONNECTION_ENDPOINT}/${encodeURIComponent(activeConnection.id)}`,
      );
      await refreshAfterMutation();
      notify.success(`${activeConnection.platform_name} 连接已撤销`);
    } catch (nextError) {
      const message = displayError(nextError, '撤销网站连接失败');
      setError(message);
      notify.error(message);
    } finally {
      setRevoking(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent aria-describedby={undefined} className="max-h-[92vh] overflow-y-auto p-0 sm:max-w-[980px]">
        <DialogHeader className="border-b border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] px-[22px] py-[20px] pr-[52px]">
          <DialogTitle className="flex items-center gap-[9px] text-[var(--gg-text-primary)]">
            <span className="grid size-[34px] place-items-center rounded-[11px] bg-[var(--gg-interaction-soft)] text-[var(--gg-interaction)]">
              <ProductIcon name="globe" size={18} />
            </span>
            网站连接中心
          </DialogTitle>
          <DialogDescription className="max-w-[760px] text-[var(--gg-text-secondary)]">
            为全网情报员选择需要的网站连接。不同平台的认证方式不同：请先在官方站点登录，再使用 Token、Cookie-Editor 或已登录浏览器会话；平台不会代填通用用户名和密码。
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-[18px] p-[18px] lg:grid-cols-[minmax(280px,0.88fr)_minmax(0,1.45fr)]">
          <section aria-label="网站平台列表" className="min-w-0">
            <div className="mb-[9px] flex items-center justify-between gap-[8px]">
              <div>
                <h2 className="gg-type-card-title font-semibold text-[var(--gg-text-primary)]">选择网站</h2>
                <p className="mt-[3px] gg-type-caption text-[var(--gg-text-muted)]">连接只归属于当前登录用户</p>
              </div>
              {catalog ? <span className="gg-type-caption text-[var(--gg-text-muted)]">{catalog.platforms.length} 个平台</span> : null}
            </div>
            {loading && !catalog ? (
              <div className="grid min-h-[260px] place-items-center rounded-[14px] border border-dashed border-[var(--gg-line)] gg-type-meta text-[var(--gg-text-muted)]">正在加载网站目录…</div>
            ) : error && !catalog ? (
              <div className="grid gap-[10px] rounded-[14px] border border-[#ffd0d0] bg-[#fff6f6] p-[14px] text-[#b42318]" role="alert">
                <p className="gg-type-meta">{error}</p>
                <UIButton type="button" variant="outline" onClick={() => void loadCatalog()} disabled={busy} className="w-fit bg-white">重新加载</UIButton>
              </div>
            ) : (
              <div className="grid gap-[8px] sm:grid-cols-2 lg:grid-cols-1">
                {catalog?.platforms.map((platform) => (
                  <PlatformCard
                    key={platform.platform_id}
                    platform={platform}
                    selected={platform.platform_id === selectedPlatformId}
                    onSelect={() => setSelectedPlatformId(platform.platform_id)}
                  />
                ))}
              </div>
            )}
          </section>

          <section aria-label="网站连接详情" className="min-w-0">
            {activePlatform ? (
              <Card className="overflow-visible border border-[var(--gg-line)] bg-[var(--gg-surface)] shadow-none">
                <CardHeader className="border-b border-[var(--gg-line)] px-[18px] py-[16px]">
                  <div className="flex items-start justify-between gap-[12px]">
                    <div className="flex min-w-0 items-start gap-[10px]">
                      <span className="grid size-[38px] shrink-0 place-items-center rounded-[12px] bg-[var(--gg-interaction-soft)] text-[var(--gg-interaction)]">
                        <ProductIcon name={platformIcon(activePlatform.platform_id)} size={19} />
                      </span>
                      <div className="min-w-0">
                        <CardTitle className="gg-type-card-title font-semibold text-[var(--gg-text-primary)]">{activePlatform.platform_name}</CardTitle>
                        <p className="mt-[3px] gg-type-caption text-[var(--gg-text-secondary)]">{activePlatform.description}</p>
                      </div>
                    </div>
                    <Badge className={cn('shrink-0 rounded-full border px-[8px] gg-type-caption font-medium', CONNECTION_STATUS_CLASSES[activePlatform.status])}>
                      {CONNECTION_STATUS_LABELS[activePlatform.status]}
                    </Badge>
                  </div>
                  <p className="mt-[10px] rounded-[10px] bg-[var(--gg-surface-subtle)] px-[11px] py-[9px] gg-type-caption text-[var(--gg-text-muted)]">{activePlatform.status_message}</p>
                  {activePlatform.setup_url && activePlatform.setup_link_label ? (
                    <a
                      href={activePlatform.setup_url}
                      target="_blank"
                      rel="noreferrer"
                      className="mt-[10px] inline-flex w-fit items-center gap-[5px] gg-type-meta font-medium text-[var(--gg-interaction)] underline underline-offset-2 hover:text-[var(--gg-cobalt)]"
                    >
                      {activePlatform.setup_link_label}
                      <ProductIcon name="arrow" size={13} />
                    </a>
                  ) : null}
                </CardHeader>

                <CardContent className="grid gap-[16px] px-[18px] py-[18px]">
                  <div className="rounded-[12px] border border-[#dfe6f5] bg-[#f7f9ff] px-[12px] py-[11px]" role="note">
                    <p className="gg-type-control font-semibold text-[#303746]">平台专属连接方式</p>
                    <p className="mt-[4px] gg-type-caption text-[#69738a]">{activePlatform.implementation_message}</p>
                  </div>

                  <div className="grid gap-[7px]">
                    <Label htmlFor="vertical-ability-connection-account-label">连接名称</Label>
                    <Input
                      id="vertical-ability-connection-account-label"
                      value={accountLabel}
                      onChange={(event) => setAccountLabel(event.target.value)}
                      placeholder="例如：我的工作账号"
                      maxLength={64}
                      disabled={busy}
                      className="h-[39px] rounded-[10px] border-[var(--gg-line)] bg-white"
                    />
                  </div>

                  {activePlatform.auth_methods.length > 0 ? (
                    <div className="grid gap-[8px]">
                      <Label>连接方式</Label>
                      <div className="grid gap-[7px] sm:grid-cols-2" role="radiogroup" aria-label={`${activePlatform.platform_name}连接方式`}>
                        {activePlatform.auth_methods.map((method) => (
                          <button
                            key={method}
                            type="button"
                            role="radio"
                            aria-checked={authMethod === method}
                            disabled={busy}
                            onClick={() => {
                              setAuthMethod(method);
                              setCredentials({});
                            }}
                            className={cn(
                              'grid gap-[3px] rounded-[10px] border px-[11px] py-[9px] text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-interaction)]/30',
                              authMethod === method
                                ? 'border-[var(--gg-interaction)] bg-[var(--gg-interaction-soft)]'
                                : 'border-[var(--gg-line)] bg-white hover:border-[#b9c6e8]',
                            )}
                          >
                            <span className="gg-type-control font-semibold text-[var(--gg-text-primary)]">{activePlatform.auth_method_labels[method] || method}</span>
                            <span className="gg-type-caption text-[var(--gg-text-muted)]">{METHOD_DESCRIPTIONS[method]}</span>
                          </button>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  {authMethod === 'browser_session' ? (
                    <div className="grid gap-[10px] rounded-[12px] border border-[#dfe6f5] bg-[#fbfcff] p-[13px]">
                      <div>
                        <p className="gg-type-control font-semibold text-[var(--gg-text-primary)]">连接已登录浏览器</p>
                        <p className="mt-[4px] gg-type-caption text-[var(--gg-text-muted)]">请先在 Chrome/Edge 打开 {activePlatform.platform_name} 并完成登录，再选择浏览器所在位置。平台不会读取浏览器密码，也不会代替你登录。</p>
                      </div>
                      {activePlatform.browser_transports.length > 0 ? (
                        <div className="grid gap-[7px]" role="radiogroup" aria-label="浏览器连接位置">
                          {activePlatform.browser_transports.map((option) => (
                            <button
                              key={option.transport}
                              type="button"
                              role="radio"
                              aria-checked={browserTransport === option.transport}
                              disabled={busy}
                              onClick={() => setBrowserTransport(option.transport)}
                              className={cn(
                                'grid gap-[3px] rounded-[10px] border px-[11px] py-[9px] text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-interaction)]/30',
                                browserTransport === option.transport
                                  ? 'border-[var(--gg-interaction)] bg-[var(--gg-interaction-soft)]'
                                  : 'border-[var(--gg-line)] bg-white hover:border-[#b9c6e8]',
                              )}
                            >
                              <span className="gg-type-control font-semibold text-[var(--gg-text-primary)]">{option.label}</span>
                              <span className="gg-type-caption text-[var(--gg-text-muted)]">{option.description}</span>
                            </button>
                          ))}
                        </div>
                      ) : null}
                      <UIButton type="button" onClick={() => void handleBrowserConnect()} disabled={busy} className="w-fit gap-[7px] bg-[var(--gg-cobalt)] text-white hover:bg-[#244bc7]">
                        <ProductIcon name="desktop" size={15} />
                        {saving ? '连接中…' : browserTransport === 'browser_relay' ? '生成宿主机配对' : '连接本机浏览器'}
                      </UIButton>
                      {relayPairing?.transport === browserTransport && relayPairing.platform_id === activePlatform.platform_id ? (
                        <div className="grid gap-[9px] rounded-[10px] border border-[#f3d28b] bg-[#fffaf0] p-[11px]" role="status">
                          <div>
                            <p className="gg-type-control font-semibold text-[#6b4300]">{browserTransport === 'browser_relay' ? '宿主机浏览器 Relay' : '本机浏览器安全配对'}</p>
                            <p className="mt-[3px] gg-type-caption text-[#7b5b1a]">{relayPairing.message} 关闭窗口不会取消这次配对。</p>
                          </div>
                          {relayPairing.pairing_token ? (
                            <div className="flex items-center gap-[7px]">
                              <code className="min-w-0 flex-1 select-all break-all rounded-[8px] border border-[#ead7a8] bg-white px-[9px] py-[8px] font-mono gg-type-caption text-[#4b3a16]">{relayPairing.pairing_token}</code>
                              <UIButton type="button" variant="outline" onClick={() => void handleCopyPairingToken()} disabled={busy} className="shrink-0 bg-white px-[10px]">复制</UIButton>
                            </div>
                          ) : null}
                          <div className="grid gap-[9px] rounded-[10px] border border-[#dfe6f5] bg-[#fbfcff] p-[11px]">
                            <div>
                              <p className="gg-type-control font-semibold text-[var(--gg-text-primary)]">启动设备连接器</p>
                              <p className="mt-[3px] gg-type-caption text-[var(--gg-text-muted)]">
                                连接器是命令行程序，不要直接双击后关闭窗口。请在对应系统的终端执行一条命令，输入上面的令牌后保持终端运行。
                              </p>
                              <p className="mt-[3px] gg-type-caption text-[#7b5b1a]">
                                {browserTransport === 'browser_relay'
                                  ? '这是远程部署：把命令中的服务地址换成 VM 的实际地址，不能使用 127.0.0.1。'
                                  : '这是本机部署：连接器、浏览器和平台服务在同一台设备，服务地址使用 127.0.0.1。'}
                              </p>
                            </div>
                            <RelayCommandBlock
                              label="Windows（安装包）"
                              hint="在共格·序伴安装目录打开 PowerShell；连接器和 OpenCLI 已随安装包提供。"
                              command={relayConnectorCommand(browserTransport, 'windows')}
                            />
                            <RelayCommandBlock
                              label="Linux（Python）"
                              hint="在项目根目录执行；需要已安装 Python 3 和 OpenCLI。"
                              command={relayConnectorCommand(browserTransport, 'linux')}
                            />
                          </div>
                          <p className="gg-type-caption text-[#7b5b1a]">
                            浏览器扩展仍需手动安装并保持登录；上方命令执行后，连接状态变为“已连接”即可使用这台设备的浏览器会话。
                          </p>
                          <Badge className="w-fit rounded-full border border-[#ead7a8] bg-white px-[8px] gg-type-caption text-[#7b5b1a]">{relayPairing.status === 'connected' ? '已连接' : relayPairing.status === 'disconnected' ? '连接器离线' : '等待连接器'}</Badge>
                        </div>
                      ) : null}
                    </div>
                  ) : methodFields.length > 0 ? (
                    <div className="grid gap-[13px]">
                      {methodFields.map((field) => (
                        <ConnectionField
                          key={field.key}
                          field={field}
                          value={credentials[field.key] || ''}
                          disabled={busy}
                          onChange={(value) => setCredentials((current) => ({ ...current, [field.key]: value }))}
                        />
                      ))}
                    </div>
                  ) : (
                    <div className="rounded-[12px] border border-dashed border-[var(--gg-line)] px-[13px] py-[12px] gg-type-meta text-[var(--gg-text-muted)]">当前连接方式不需要手工填写凭据。</div>
                  )}

                  {error ? <p className="rounded-[10px] bg-[#fff6f6] px-[11px] py-[9px] gg-type-caption text-[#b42318]" role="alert">{error}</p> : null}

                  <div className="flex flex-wrap items-center justify-between gap-[8px] border-t border-[var(--gg-line)] pt-[14px]">
                    <div className="min-w-0">
                      <p className="gg-type-meta font-medium text-[var(--gg-text-primary)]">{activeConnection?.account_identity || '尚未验证账号身份'}</p>
                      <p className="mt-[2px] gg-type-caption text-[var(--gg-text-muted)]">{activeConnection ? `修订 ${activeConnection.revision} · ${transportLabel(activeConnection.transport)} · ${activeConnection.message}` : '保存后需要验证，验证通过的连接才会进入运行时。'}</p>
                    </div>
                    <div className="flex flex-wrap justify-end gap-[7px]">
                      {activeConnection ? <UIButton type="button" variant="outline" onClick={() => void handleRevoke()} disabled={busy} className="h-[34px] border-[#f0bcbc] bg-white px-[11px] text-[#b42318] hover:bg-[#fffafa]">{revoking ? '撤销中…' : '撤销连接'}</UIButton> : null}
                      {activeConnection ? <UIButton type="button" variant="outline" onClick={() => void handleVerify()} disabled={busy} className="h-[34px] gap-[5px] border-[var(--gg-line)] bg-white px-[11px]">{verifying ? '验证中…' : '验证连接'}</UIButton> : null}
                      {authMethod !== 'browser_session' ? <UIButton type="button" onClick={() => void handleSave()} disabled={busy} className="h-[34px] bg-[var(--gg-cobalt)] px-[13px] text-white hover:bg-[#244bc7]">{saving ? '保存中…' : '保存连接'}</UIButton> : null}
                    </div>
                  </div>
                </CardContent>
              </Card>
            ) : (
              <div className="grid min-h-[360px] place-items-center rounded-[14px] border border-dashed border-[var(--gg-line)] gg-type-meta text-[var(--gg-text-muted)]">选择一个网站查看连接方式</div>
            )}
          </section>
        </div>

        <DialogFooter className="border-t border-[var(--gg-line)] px-[18px] py-[12px]">
          <p className="mr-auto max-w-[620px] gg-type-caption text-[var(--gg-text-muted)]">关闭窗口不会取消能力包；下次使用需要登录态的平台时，系统会再次检查对应连接。</p>
          <UIButton type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>完成</UIButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PlatformCard({
  platform,
  selected,
  onSelect,
}: {
  platform: VerticalAbilityConnectionPlatform;
  selected: boolean;
  onSelect: () => void;
}) {
  /** 渲染网站连接中心的单个平台卡片，卡片只改变详情焦点。 */

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        'grid min-w-0 gap-[8px] rounded-[12px] border px-[11px] py-[11px] text-left transition-[border-color,background-color,box-shadow] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-interaction)]/30',
        selected
          ? 'border-[var(--gg-interaction)] bg-[var(--gg-interaction-soft)] shadow-[0_5px_14px_rgba(61,91,214,0.09)]'
          : 'border-[var(--gg-line)] bg-white hover:border-[#b9c6e8] hover:bg-[#fbfcff]',
      )}
    >
      <span className="flex items-center justify-between gap-[8px]">
        <span className="flex min-w-0 items-center gap-[8px]">
          <span className="grid size-[29px] shrink-0 place-items-center rounded-[9px] bg-[var(--gg-surface-subtle)] text-[var(--gg-interaction)]">
            <ProductIcon name={platformIcon(platform.platform_id)} size={15} />
          </span>
          <span className="truncate gg-type-control font-semibold text-[var(--gg-text-primary)]">{platform.platform_name}</span>
        </span>
        <Badge className={cn('shrink-0 rounded-full border px-[7px] gg-type-caption font-medium', CONNECTION_STATUS_CLASSES[platform.status])}>
          {CONNECTION_STATUS_LABELS[platform.status]}
        </Badge>
      </span>
      <span className="line-clamp-2 gg-type-caption text-[var(--gg-text-muted)]">{platform.description}</span>
    </button>
  );
}

function ConnectionField({
  field,
  value,
  disabled,
  onChange,
}: {
  field: VerticalAbilityConnectionField;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  /** 渲染平台注册表声明的单个敏感字段，不在组件中硬编码网站凭据键。 */

  const fieldId = `vertical-connection-${field.key.replace(/[^a-zA-Z0-9_-]/g, '-')}`;
  return (
    <div className="grid gap-[6px]">
      <Label htmlFor={fieldId}>{field.label}{field.required ? <span className="ml-[3px] text-[#b42318]" aria-hidden="true">*</span> : null}</Label>
      {field.key === 'cookie' ? (
        <Textarea
          id={fieldId}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={field.placeholder}
          autoComplete="off"
          disabled={disabled}
          rows={3}
          className="min-h-[76px] resize-y rounded-[10px] border-[var(--gg-line)] bg-white font-mono gg-type-meta"
        />
      ) : (
        <Input
          id={fieldId}
          type={field.secret ? 'password' : 'text'}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={field.placeholder}
          autoComplete="new-password"
          disabled={disabled}
          className="h-[39px] rounded-[10px] border-[var(--gg-line)] bg-white font-mono"
        />
      )}
      <p className="gg-type-caption text-[var(--gg-text-muted)]">{field.description}</p>
    </div>
  );
}

function RelayCommandBlock({
  label,
  hint,
  command,
}: {
  label: string;
  hint: string;
  command: string;
}) {
  /** 展示一个可复制的跨平台 Relay 启动命令，令牌只从终端输入。 */

  return (
    <div className="grid gap-[7px] rounded-[9px] border border-[var(--gg-line)] bg-white p-[9px]">
      <div className="flex items-start justify-between gap-[8px]">
        <div className="min-w-0">
          <p className="gg-type-control font-semibold text-[var(--gg-text-primary)]">{label}</p>
          <p className="mt-[2px] gg-type-caption text-[var(--gg-text-muted)]">{hint}</p>
        </div>
        <UIButton
          type="button"
          variant="outline"
          onClick={() => void copyText(command, `${label}命令已复制`)}
          className="shrink-0 bg-white px-[9px]"
          aria-label={`复制${label}命令`}
        >
          复制命令
        </UIButton>
      </div>
      <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-[8px] bg-[#f5f7fc] px-[9px] py-[8px] font-mono text-[11px] leading-[1.55] text-[#303746]"><code>{command}</code></pre>
    </div>
  );
}

function compactCredentials(values: Record<string, string>): Record<string, string> {
  /** 清除空字段，避免把空字符串当成一份新的平台凭据保存。 */

  return Object.fromEntries(Object.entries(values).filter(([, value]) => value.trim()));
}

function platformIcon(platformId: string): ProductIconName {
  /** 将平台目录键收敛到现有产品图标集合，未知平台仍然可以展示。 */

  const icon = PLATFORM_ICONS[platformId];
  return icon || 'globe';
}

function defaultBrowserTransport(): VerticalAbilityConnectionTransport {
  /** 根据页面是否运行在本机回环地址选择初始建议，用户始终可以手动切换。 */

  if (typeof window !== 'undefined') {
    const hostname = window.location.hostname;
    if (hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]' || hostname === '::1') {
      return 'local_loopback';
    }
  }
  return 'browser_relay';
}

function transportLabel(transport: VerticalAbilityConnectionTransport): string {
  /** 将稳定传输键转换为连接详情中的简短用户文案。 */

  return transport === 'browser_relay' ? '宿主机 Relay' : '本机连接';
}

function relayConnectorCommand(
  transport: VerticalAbilityConnectionTransport,
  platform: 'windows' | 'linux',
): string {
  /** 生成页面展示的本机/远程 Relay 命令，不把真实配对令牌落入页面静态文本。 */

  const serverUrl = transport === 'local_loopback'
    ? 'http://127.0.0.1:5137'
    : 'http://<VM或服务器地址>:5137';
  if (platform === 'windows') {
    return [
      '$token = Read-Host "粘贴页面中的配对令牌"',
      `.\\browser-relay-connector.exe --server-url "${serverUrl}" --pairing-token $token --opencli-path ".\\runtime\\opencli\\opencli.exe"`,
    ].join('\n');
  }
  return [
    'read -r -s -p "粘贴页面中的配对令牌: " PAIRING_TOKEN; echo',
    'python3 backend/browser_relay_connector.py \\',
    `  --server-url "${serverUrl}" \\`,
    '  --pairing-token "$PAIRING_TOKEN" \\',
    '  --opencli-path "$(command -v opencli)"',
  ].join('\n');
}

async function copyText(value: string, successMessage: string): Promise<void> {
  /** 统一处理令牌和命令的复制反馈，复制失败时允许用户手动选择文本。 */

  try {
    await navigator.clipboard.writeText(value);
    notify.success(successMessage);
  } catch {
    notify.warning('浏览器未允许复制，请手动选择文本');
  }
}

function displayError(error: unknown, fallback: string): string {
  /** 把连接 API 的结构化错误转换成不暴露凭据的用户提示。 */

  if (error instanceof ApiError && error.message) return error.message;
  return error instanceof Error && error.message ? error.message : fallback;
}
