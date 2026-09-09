/**
 * @Time       : 2026/09/03 10:00
 * @Author     : zhanglp8181
 * @File       : VerticalAbilitySettingsPage.tsx
 * @CallChain  : 企业端能力包配置入口 → 能力包卡片首页/详情 → Provider 配置 API
 * @Description: 提供数据驱动的能力包配置首页和按能力包分组的依赖详情页。
 */

import { useCallback, useEffect, useState } from 'react';
import {
  Activity,
  ArrowRight,
  CheckCircle2,
  Database,
  ExternalLink,
  Globe2,
  KeyRound,
  Link2,
  RefreshCw,
  ServerCog,
  ShieldCheck,
  TerminalSquare,
  TriangleAlert,
} from 'lucide-react';
import { Link, useParams } from 'react-router-dom';

import { api, ApiError } from '@/api/client';
import type { EnterpriseAuthUser } from '@/auth';
import AppHeader from '@/components/AppHeader';
import { EnterpriseCatalogHero } from '@/components/EnterpriseCatalogHeader';
import { CatalogGrid } from '@/components/enterprise/CatalogGrid';
import { DetailSection, DetailSurface } from '@/components/enterprise/DetailSurface';
import { PageHeader } from '@/components/enterprise/PageHeader';
import { PageShell } from '@/components/enterprise/PageShell';
import ProductIcon, { type ProductIconName } from '@/components/ProductIcon';
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Input, Label } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import {
  DETAIL_PANEL_CLASS,
  RESOURCE_CARD_CLASS,
  RESOURCE_CARD_DESCRIPTION_CLASS,
  RESOURCE_CARD_FOOTER_CLASS,
  RESOURCE_CARD_ICON_SLOT_CLASS,
} from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';
import { EnterpriseRoute } from '@/enums/routes';
import type {
  VerticalAbilityConfigurationDetail,
  VerticalAbilityConfigurationStatus,
  VerticalAbilityConfigurationSummary,
  VerticalAbilityHealthStatus,
  VerticalAbilityProviderSetting,
  VerticalAbilityResource,
  VerticalAbilityResourceStatus,
  VerticalAbilityResourceType,
  VerticalAbilitySettingsRead,
} from '@/types/verticalAbilitySettings';


const SETTINGS_ENDPOINT = '/api/enterprise/vertical-ability-settings';

const HEALTH_LABELS: Record<VerticalAbilityHealthStatus, string> = {
  unverified: '尚未验证',
  healthy: '连接正常',
  degraded: '连接不稳定',
  unhealthy: '连接异常',
};

const HEALTH_CLASSES: Record<VerticalAbilityHealthStatus, string> = {
  unverified: 'border-[#d9dee9] bg-[#f7f8fb] text-[#69738a]',
  healthy: 'border-[#b7e4c7] bg-[#f2fbf5] text-[#237a42]',
  degraded: 'border-[#f3d28b] bg-[#fff8e8] text-[#8a4b00]',
  unhealthy: 'border-[#ffd0d0] bg-[#fff6f6] text-[#b42318]',
};

const CONFIGURATION_STATUS_LABELS: Record<VerticalAbilityConfigurationStatus, string> = {
  ready: '依赖已满足',
  partial: '部分配置',
  needs_configuration: '需要配置',
};

const CONFIGURATION_STATUS_CLASSES: Record<VerticalAbilityConfigurationStatus, string> = {
  ready: 'border-[#b7e4c7] bg-[#f2fbf5] text-[#237a42]',
  partial: 'border-[#f3d28b] bg-[#fff8e8] text-[#8a4b00]',
  needs_configuration: 'border-[#f3d28b] bg-[#fff8e8] text-[#8a4b00]',
};

const RESOURCE_TYPE_LABELS: Record<VerticalAbilityResourceType, string> = {
  platform_secret: '平台服务',
  connection: '连接资源',
  runtime: '运行环境',
  public: '公共渠道',
};

const RESOURCE_TYPE_DESCRIPTIONS: Record<VerticalAbilityResourceType, string> = {
  platform_secret: '需要管理员维护的服务凭据。',
  connection: '需要在平台连接中心完成授权的资源。',
  runtime: '由平台部署环境提供的运行时能力。',
  public: '平台内置的公开数据渠道，不需要额外配置。',
};

const RESOURCE_STATUS_LABELS: Record<VerticalAbilityResourceStatus, string> = {
  ready: '可用',
  needs_configuration: '待配置',
  not_connected: '未连接',
  diagnostic: '运行时提供',
};

const RESOURCE_STATUS_CLASSES: Record<VerticalAbilityResourceStatus, string> = {
  ready: 'border-[#b7e4c7] bg-[#f2fbf5] text-[#237a42]',
  needs_configuration: 'border-[#f3d28b] bg-[#fff8e8] text-[#8a4b00]',
  not_connected: 'border-[#d9dee9] bg-[#f7f8fb] text-[#69738a]',
  diagnostic: 'border-[#cbd8ff] bg-[#f4f6ff] text-[var(--gg-cobalt)]',
};

const RESOURCE_TYPE_ICONS: Record<VerticalAbilityResourceType, typeof KeyRound> = {
  platform_secret: KeyRound,
  connection: Link2,
  runtime: TerminalSquare,
  public: Globe2,
};

const PRODUCT_ICON_NAMES = new Set<ProductIconName>([
  'arrow',
  'branch',
  'calendar',
  'chat',
  'check',
  'cloud',
  'database',
  'desktop',
  'file',
  'globe',
  'grid',
  'info',
  'model',
  'refresh',
  'search',
  'spark',
  'tool',
  'user',
  'warning',
]);

function providerEndpoint(provider: string): string {
  /** 根据服务端目录中的稳定 Provider 标识构造配置卡片 API 地址。 */

  return `${SETTINGS_ENDPOINT}/${encodeURIComponent(provider)}`;
}

function providerFieldId(provider: string): string {
  /** 将 Provider 标识转换成稳定且合法的表单字段 ID。 */

  return `vertical-provider-${provider.replace(/[^a-zA-Z0-9_-]/g, '-')}-secret`;
}

function abilityNames(setting: VerticalAbilityProviderSetting): string {
  /** 合并卡片关联的能力包名称，兼容一个 Provider 服务多个能力包。 */

  return setting.ability_names.join('、') || setting.ability_ids.join('、') || '平台能力包';
}

function displayError(error: unknown, fallback: string): string {
  /** 将 API 或网络异常转换成卡片页面可读的错误摘要。 */

  return error instanceof Error && error.message ? error.message : fallback;
}

function abilityIconName(iconKey: string): ProductIconName {
  /** 将后端图标键收敛到产品图标集合，未知键使用 globe 保证卡片仍可渲染。 */

  return PRODUCT_ICON_NAMES.has(iconKey as ProductIconName) ? iconKey as ProductIconName : 'globe';
}

type ProviderCredentialCardProps = {
  setting: VerticalAbilityProviderSetting;
  secretValue: string;
  saving: boolean;
  verifying: boolean;
  onSecretChange: (provider: string, value: string) => void;
  onSave: (setting: VerticalAbilityProviderSetting) => void;
  onVerify: (setting: VerticalAbilityProviderSetting) => void;
};

function ProviderCredentialCard({
  setting,
  secretValue,
  saving,
  verifying,
  onSecretChange,
  onSave,
  onVerify,
}: ProviderCredentialCardProps) {
  /** 渲染一张与能力包首页解耦的 Provider 凭据卡片。 */

  const healthStatus = setting.health_status;
  const fieldId = providerFieldId(setting.provider);
  const relatedAbilities = abilityNames(setting);

  return (
    <Card
      className="overflow-visible border border-[#e5e9f2] bg-white shadow-[0_10px_28px_rgba(31,48,86,0.06)]"
      data-testid={`vertical-provider-card-${setting.provider}`}
    >
      <CardHeader className="border-b border-[#edf0f5] px-[20px] py-[18px]">
        <div className="flex items-start justify-between gap-[12px]">
          <div className="flex min-w-0 items-start gap-[11px]">
            <span className="grid size-[40px] shrink-0 place-items-center rounded-[12px] bg-[#edf2ff] text-[var(--gg-cobalt)]">
              <KeyRound className="size-[19px]" aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <CardTitle className="gg-type-card-title font-semibold text-[#18181a]">{setting.provider_name}</CardTitle>
              <p className="mt-[3px] truncate gg-type-caption text-[#757f9c]">服务于：{relatedAbilities}</p>
            </div>
          </div>
          <Badge className={cn(
            'h-[25px] shrink-0 rounded-full border px-[9px] gg-type-caption font-medium',
            setting.configured
              ? 'border-[#b7e4c7] bg-[#f2fbf5] text-[#237a42]'
              : 'border-[#f3d28b] bg-[#fff8e8] text-[#8a4b00]',
          )}>
            {setting.configured ? '已配置' : '待配置'}
          </Badge>
        </div>
        <CardDescription className="mt-[12px] text-[#757f9c]">{setting.description}</CardDescription>
        {setting.credential_setup_text && setting.credential_setup_url && setting.credential_setup_link_label ? (
          <p className="mt-[9px] gg-type-meta text-[#69738a]">
            {setting.credential_setup_text}{' '}
            <a
              href={setting.credential_setup_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-[4px] font-medium text-[var(--gg-cobalt)] underline underline-offset-2 hover:text-[#244bc7]"
            >
              {setting.credential_setup_link_label}
              <ExternalLink className="size-[12px]" aria-hidden="true" />
            </a>
          </p>
        ) : null}
      </CardHeader>

      <CardContent className="grid gap-[18px] px-[20px] py-[19px]">
        <div className="rounded-[12px] border border-[#e6eaf2] bg-[#fafbfe] p-[13px]">
          <div className="flex items-start gap-[9px]">
            <ShieldCheck className="mt-[2px] size-[16px] shrink-0 text-[var(--gg-cobalt)]" aria-hidden="true" />
            <div className="grid gap-[4px]">
              <p className="gg-type-control font-semibold text-[#303746]">凭据只在服务端使用</p>
              <p className="gg-type-caption text-[#757f9c]">
                保存后只保留加密密文和脱敏状态，页面不会回显完整凭据或写入浏览器本地存储。
              </p>
            </div>
          </div>
        </div>

        <div className="grid gap-[7px]">
          <Label htmlFor={fieldId}>{setting.credential_label}</Label>
          <Input
            id={fieldId}
            type="password"
            value={secretValue}
            onChange={(event) => onSecretChange(setting.provider, event.target.value)}
            placeholder={setting.credential_placeholder}
            autoComplete="new-password"
            disabled={saving || verifying}
            className="h-[40px] rounded-[10px] border-[#dfe4ee] bg-white px-[12px] font-mono focus-visible:border-[var(--gg-cobalt)] focus-visible:ring-[var(--gg-cobalt)]/15"
          />
          <p className="gg-type-caption text-[#9299aa]">输入新值即可替换当前 Provider 凭据。</p>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-[10px] rounded-[11px] border border-[#edf0f5] px-[12px] py-[11px]">
          <div className="flex min-w-0 items-center gap-[8px]">
            <Activity className="size-[16px] shrink-0 text-[#757f9c]" aria-hidden="true" />
            <div className="min-w-0">
              <p className="gg-type-meta font-medium text-[#464c5e]">当前状态：{HEALTH_LABELS[healthStatus]}</p>
              <p className="mt-[2px] truncate gg-type-caption text-[#9299aa]">
                {setting.credential_masked ? `摘要：${setting.credential_masked} · ` : ''}
                修订 {setting.revision}
              </p>
            </div>
          </div>
          <Badge className={cn('rounded-full border px-[8px] gg-type-caption font-medium', HEALTH_CLASSES[healthStatus])}>
            {HEALTH_LABELS[healthStatus]}
          </Badge>
        </div>

        {setting.health_error_code ? (
          <div className="flex items-start gap-[8px] rounded-[10px] border border-[#ffd0d0] bg-[#fff6f6] px-[12px] py-[10px] text-[#b42318]">
            <TriangleAlert className="mt-[1px] size-[15px] shrink-0" aria-hidden="true" />
            <p className="gg-type-caption">最近一次验证：{setting.health_error_code}</p>
          </div>
        ) : null}

        {setting.message ? (
          <p className={cn(
            'rounded-[10px] px-[12px] py-[9px] gg-type-caption',
            setting.verification_success === false
              ? 'bg-[#fff6f6] text-[#b42318]'
              : 'bg-[#f2fbf5] text-[#237a42]',
          )}>
            {setting.message}
          </p>
        ) : null}

        <div className="flex flex-wrap justify-end gap-[8px]">
          {setting.supports_verification ? (
            <UIButton
              type="button"
              variant="outline"
              onClick={() => onVerify(setting)}
              disabled={saving || verifying || !setting.configured}
              className="h-[35px] gap-[6px] rounded-[9px] border-[#dfe4ee] bg-white px-[14px] gg-type-control text-[#464c5e] hover:bg-[#f7f8fb]"
            >
              <RefreshCw className={cn('size-[14px]', verifying && 'animate-spin')} aria-hidden="true" />
              {verifying ? '验证中…' : '验证连接'}
            </UIButton>
          ) : null}
          <UIButton
            type="button"
            onClick={() => onSave(setting)}
            disabled={saving || verifying || !secretValue.trim()}
            className="h-[35px] gap-[6px] rounded-[9px] bg-[var(--gg-cobalt)] px-[16px] gg-type-control font-semibold text-white hover:bg-[#244bc7]"
          >
            <CheckCircle2 className="size-[14px]" aria-hidden="true" />
            {saving ? '保存中…' : '保存凭据'}
          </UIButton>
        </div>
      </CardContent>
    </Card>
  );
}

function ConfigurationStatusBadge({ status }: { status: VerticalAbilityConfigurationStatus }) {
  /** 统一渲染能力包和资源的配置状态徽章。 */

  return (
    <Badge className={cn(
      'rounded-full border px-[9px] gg-type-caption font-medium',
      CONFIGURATION_STATUS_CLASSES[status],
    )}>
      {CONFIGURATION_STATUS_LABELS[status]}
    </Badge>
  );
}

function AbilityPackageCard({ ability }: { ability: VerticalAbilityConfigurationSummary }) {
  /** 渲染能力包首页卡片，卡片只负责进入详情，不承载 Provider 表单。 */

  return (
    <Link
      to={`${EnterpriseRoute.VerticalAbilitySettings}/${ability.id}`}
      className={cn(RESOURCE_CARD_CLASS, 'border-[var(--gg-line)] no-underline')}
      data-testid={`vertical-ability-card-${ability.id}`}
      aria-label={`查看${ability.name}配置详情`}
    >
      <div className="flex items-start justify-between gap-[10px]">
        <span className={cn(RESOURCE_CARD_ICON_SLOT_CLASS, 'bg-[var(--gg-interaction-soft)]')}>
          <ProductIcon name={abilityIconName(ability.icon_key)} size={24} />
        </span>
        <ConfigurationStatusBadge status={ability.configuration_status} />
      </div>
      <div className="mt-[16px] flex min-w-0 items-center gap-[7px]">
        <span className="gg-type-caption rounded-[var(--gg-radius-control)] bg-[var(--gg-interaction-soft)] px-[8px] py-[4px] text-[var(--gg-interaction)]">
          {ability.category}
        </span>
        <span className="gg-type-caption text-[var(--gg-text-muted)]">v{ability.version}</span>
      </div>
      <h2 className="mt-[9px] gg-type-card-title font-semibold text-[var(--gg-text-primary)]">{ability.name}</h2>
      <p className={cn(RESOURCE_CARD_DESCRIPTION_CLASS, 'text-[var(--gg-text-secondary)]')}>
        {ability.description}
      </p>
      <div className="mt-[10px] flex min-h-[30px] flex-wrap items-center gap-[6px]">
        {ability.platform_tags.slice(0, 5).map((tag) => (
          <span key={tag} className="gg-type-caption rounded-full border border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] px-[8px] py-[4px] text-[var(--gg-text-muted)]">
            {tag}
          </span>
        ))}
      </div>
      <div className={cn(RESOURCE_CARD_FOOTER_CLASS, 'mt-[14px] justify-between')}>
        <span className="gg-type-caption min-w-0 truncate text-[var(--gg-text-muted)]">
          已就绪 {ability.ready_resource_count}/{ability.resource_count} 项依赖
        </span>
        <ArrowRight className="size-[15px] shrink-0 text-[var(--gg-interaction)]" aria-hidden="true" />
      </div>
    </Link>
  );
}

function ErrorNotice({ message, onRetry }: { message: string; onRetry: () => void }) {
  /** 统一呈现配置 API 错误并提供可重复的加载动作。 */

  return (
    <div role="alert" className="mb-[16px] flex items-center justify-between gap-[12px] rounded-[12px] border border-[#ffd0d0] bg-[#fff6f6] px-[14px] py-[12px] text-[#b42318]">
      <div className="flex min-w-0 items-start gap-[8px]">
        <TriangleAlert className="mt-[1px] size-[16px] shrink-0" aria-hidden="true" />
        <div className="min-w-0">
          <p className="gg-type-control font-medium">配置中心暂时无法加载</p>
          <p className="mt-[2px] truncate gg-type-caption">{message}</p>
        </div>
      </div>
      <UIButton
        type="button"
        variant="outline"
        onClick={onRetry}
        className="h-[32px] shrink-0 rounded-[8px] border-[#f0bcbc] bg-white px-[11px] gg-type-caption text-[#b42318] hover:bg-[#fffafa]"
      >
        重新加载
      </UIButton>
    </div>
  );
}

function CatalogLoading({ label }: { label: string }) {
  /** 渲染首页和详情页共用的加载状态，保持内容区域尺寸稳定。 */

  return (
    <Card className="border border-[#e5e9f2] bg-white shadow-[0_10px_28px_rgba(31,48,86,0.06)]">
      <CardContent className="flex min-h-[220px] items-center justify-center text-[#9299aa]">
        <div className="flex items-center gap-[8px] gg-type-meta">
          <RefreshCw className="size-[15px] animate-spin" aria-hidden="true" />
          {label}
        </div>
      </CardContent>
    </Card>
  );
}

function VerticalAbilitySettingsCatalogPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  /** 加载并展示能力包配置首页，Provider 只在具体能力包详情中出现。 */

  const [settings, setSettings] = useState<VerticalAbilitySettingsRead | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setSettings(await api.get<VerticalAbilitySettingsRead>(SETTINGS_ENDPOINT));
    } catch (error) {
      const message = displayError(error, '加载能力包配置失败');
      setSettings(null);
      setLoadError(message);
      notify.error(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const abilities = settings?.abilities || [];

  return (
    <PageShell template="catalog" data-testid="vertical-ability-settings-page">
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        title="能力包配置"
        description="查看平台内置垂直能力包及其依赖配置；能力包对所有登录用户开放。"
      />

      <section className="mt-[20px] overflow-hidden rounded-[var(--gg-radius-panel)] border border-[var(--gg-line)] bg-[var(--gg-surface)] shadow-[var(--gg-shadow-card)]">
        <EnterpriseCatalogHero
          icon={Globe2}
          title="垂直能力包"
          description="每个能力包是一套深度定制的专业工作流。点击卡片进入详情，维护它所依赖的平台服务和公共渠道。"
        />
        <div className="flex flex-wrap items-center justify-between gap-[10px] border-t border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] px-[22px] py-[13px]">
          <p className="gg-type-meta text-[var(--gg-text-muted)]" role="status">
            {loading ? '正在读取能力包目录…' : `共 ${abilities.length} 个能力包`}
          </p>
          <p className="gg-type-caption text-[var(--gg-text-muted)]">配置权限与能力使用权限分离，登录用户均可选择已发布能力包。</p>
        </div>

        <div className="p-[22px]">
          {loadError ? <ErrorNotice message={loadError} onRetry={() => void load()} /> : null}
          {loading && !settings ? (
            <CatalogLoading label="正在加载能力包目录…" />
          ) : abilities.length > 0 ? (
            <CatalogGrid family="resource" data-testid="vertical-ability-catalog-grid">
              {abilities.map((ability) => <AbilityPackageCard key={ability.id} ability={ability} />)}
            </CatalogGrid>
          ) : (
            <div className="rounded-[14px] border border-dashed border-[#dfe4ee] bg-[#fbfcff] px-[18px] py-[32px] text-center gg-type-control text-[var(--gg-slate)]">
              当前版本暂无已发布的能力包。
            </div>
          )}
        </div>
      </section>

      <div className="mt-[16px] grid gap-[12px] md:grid-cols-2">
        <div className={cn(DETAIL_PANEL_CLASS, 'bg-[var(--gg-surface-subtle)]')}>
          <div className="flex items-start gap-[9px]">
            <Database className="mt-[2px] size-[16px] shrink-0 text-[var(--gg-interaction)]" aria-hidden="true" />
            <div>
              <p className="gg-type-control font-semibold text-[var(--gg-text-primary)]">配置中心的组织方式</p>
              <p className="mt-[6px] gg-type-meta text-[var(--gg-text-secondary)]">首页按能力包展示，详情按资源类型展示；后续新增能力包只需补充目录和依赖声明。</p>
            </div>
          </div>
        </div>
        <div className={cn(DETAIL_PANEL_CLASS, 'bg-[var(--gg-surface-subtle)]')}>
          <div className="flex items-start gap-[9px]">
            <ServerCog className="mt-[2px] size-[16px] shrink-0 text-[var(--gg-interaction)]" aria-hidden="true" />
            <div>
              <p className="gg-type-control font-semibold text-[var(--gg-text-primary)]">网络与运行时</p>
              <p className="mt-[6px] gg-type-meta text-[var(--gg-text-secondary)]">DNS、出网安全和 Provider 状态由平台运行时处理，页面只展示配置与诊断结果。</p>
            </div>
          </div>
        </div>
      </div>
    </PageShell>
  );
}

function ResourceStatusCard({ resource }: { resource: VerticalAbilityResource }) {
  /** 展示不需要表单的公共、连接和运行时资源状态。 */

  const ResourceIcon = RESOURCE_TYPE_ICONS[resource.resource_type];
  return (
    <Card className="border border-[var(--gg-line)] bg-[var(--gg-surface)] shadow-none" data-testid={`vertical-resource-card-${resource.resource_id}`}>
      <CardHeader className="px-[16px] py-[15px]">
        <div className="flex items-start justify-between gap-[10px]">
          <div className="flex min-w-0 items-start gap-[10px]">
            <span className="grid size-[36px] shrink-0 place-items-center rounded-[11px] bg-[var(--gg-interaction-soft)] text-[var(--gg-interaction)]">
              <ResourceIcon className="size-[17px]" aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <CardTitle className="gg-type-card-title font-semibold">{resource.name}</CardTitle>
              <p className="mt-[3px] gg-type-caption text-[var(--gg-text-muted)]">{RESOURCE_TYPE_LABELS[resource.resource_type]}</p>
            </div>
          </div>
          <Badge className={cn('rounded-full border px-[8px] gg-type-caption font-medium', RESOURCE_STATUS_CLASSES[resource.status])}>
            {RESOURCE_STATUS_LABELS[resource.status]}
          </Badge>
        </div>
        <CardDescription className="mt-[11px] text-[var(--gg-text-secondary)]">{resource.description}</CardDescription>
      </CardHeader>
      <CardContent className="px-[16px] pb-[16px] pt-0">
        <p className="rounded-[10px] bg-[var(--gg-surface-subtle)] px-[10px] py-[9px] gg-type-caption text-[var(--gg-text-muted)]">
          {resource.status_message}
        </p>
      </CardContent>
    </Card>
  );
}

function ResourceGroup({
  resourceType,
  resources,
}: {
  resourceType: VerticalAbilityResourceType;
  resources: VerticalAbilityResource[];
}) {
  /** 按资源类型分组展示详情，避免未来新增资源时修改页面结构。 */

  if (resources.length === 0) return null;
  return (
    <DetailSection
      title={RESOURCE_TYPE_LABELS[resourceType]}
      data-testid={`vertical-resource-group-${resourceType}`}
    >
      <p className="mb-[11px] gg-type-caption text-[var(--gg-text-muted)]">{RESOURCE_TYPE_DESCRIPTIONS[resourceType]}</p>
      <div className="grid gap-[12px] md:grid-cols-2">
        {resources.map((resource) => <ResourceStatusCard key={resource.resource_id} resource={resource} />)}
      </div>
    </DetailSection>
  );
}

function VerticalAbilitySettingsDetailPage({
  abilityId,
  currentUser,
  onLogout,
}: {
  abilityId: string;
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  /** 加载单个能力包详情，并在其平台服务分组内维护凭据。 */

  const [detail, setDetail] = useState<VerticalAbilityConfigurationDetail | null>(null);
  const [secretValues, setSecretValues] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [savingProvider, setSavingProvider] = useState<string | null>(null);
  const [verifyingProvider, setVerifyingProvider] = useState<string | null>(null);

  const loadDetail = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const next = await api.get<VerticalAbilityConfigurationDetail>(
        `${SETTINGS_ENDPOINT}/abilities/${encodeURIComponent(abilityId)}`,
      );
      setDetail(next);
    } catch (error) {
      const message = displayError(error, '加载能力包详情失败');
      setDetail(null);
      setLoadError(message);
      notify.error(message);
    } finally {
      setLoading(false);
    }
  }, [abilityId]);

  useEffect(() => {
    void loadDetail();
  }, [loadDetail]);

  const replaceProvider = useCallback((next: VerticalAbilityProviderSetting) => {
    /** 用服务端最新状态替换详情页中的对应 Provider。 */

    setDetail((current) => current
      ? { ...current, providers: current.providers.map((item) => item.provider === next.provider ? next : item) }
      : current);
  }, []);

  const save = useCallback(async (setting: VerticalAbilityProviderSetting) => {
    /** 保存单张 Provider 卡片的密钥并刷新能力包资源状态。 */

    const value = (secretValues[setting.provider] || '').trim();
    if (!value) {
      notify.error(`请输入 ${setting.credential_label}`);
      return;
    }
    setSavingProvider(setting.provider);
    try {
      const next = await api.put<VerticalAbilityProviderSetting>(
        providerEndpoint(setting.provider),
        { credential: value, expected_revision: setting.revision },
      );
      replaceProvider(next);
      setSecretValues((current) => {
        const nextValues = { ...current };
        delete nextValues[setting.provider];
        return nextValues;
      });
      await loadDetail();
      notify.success(`${setting.credential_label} 已保存，后续对话可直接使用${abilityNames(setting)}`);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await loadDetail();
      }
      notify.error(displayError(error, `保存 ${setting.credential_label} 失败`));
    } finally {
      setSavingProvider(null);
    }
  }, [loadDetail, replaceProvider, secretValues]);

  const verify = useCallback(async (setting: VerticalAbilityProviderSetting) => {
    /** 验证单张 Provider 卡片的连接并刷新其健康状态。 */

    setVerifyingProvider(setting.provider);
    try {
      const next = await api.post<VerticalAbilityProviderSetting>(
        `${providerEndpoint(setting.provider)}/verify`,
      );
      replaceProvider(next);
      if (next.verification_success) {
        notify.success(next.message || `${setting.provider_name} 连接验证成功`);
      } else {
        notify.warning(next.message || `${setting.provider_name} 连接验证失败`);
      }
    } catch (error) {
      notify.error(displayError(error, `验证 ${setting.provider_name} 连接失败`));
    } finally {
      setVerifyingProvider(null);
    }
  }, [replaceProvider]);

  const resourceGroups = detail ? (['platform_secret', 'connection', 'runtime', 'public'] as const).map((resourceType) => ({
    resourceType,
    resources: detail.resources.filter((resource) => resource.resource_type === resourceType),
  })) : [];

  return (
    <PageShell template="detail" data-testid="vertical-ability-settings-detail-page" aria-busy={loading}>
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        left={(
          <PageHeader
            size="section"
            backTo={EnterpriseRoute.VerticalAbilitySettings}
            backLabel="返回能力包配置"
            title={detail?.ability.name || '能力包详情'}
            description="查看能力包依赖，并维护对应的平台服务配置"
          />
        )}
      />

      {loadError ? <div className="mt-[20px]"><ErrorNotice message={loadError} onRetry={() => void loadDetail()} /></div> : null}
      {loading && !detail ? (
        <div className="mt-[20px]"><CatalogLoading label="正在加载能力包详情…" /></div>
      ) : detail ? (
        <DetailSurface container="page" className="mt-[20px] grid gap-[16px] lg:grid-cols-[minmax(0,1.45fr)_minmax(280px,0.65fr)]">
          <main className="min-w-0 overflow-hidden rounded-[var(--gg-radius-panel)] border border-[var(--gg-line)] bg-[var(--gg-surface)] shadow-[var(--gg-shadow-card)]">
            <div className="border-b border-[var(--gg-line)] px-[22px] py-[22px]">
              <div className="flex flex-wrap items-start justify-between gap-[14px]">
                <div className="flex min-w-0 items-start gap-[12px]">
                  <span className={cn(RESOURCE_CARD_ICON_SLOT_CLASS, 'bg-[var(--gg-interaction-soft)]')}>
                    <ProductIcon name={abilityIconName(detail.ability.icon_key)} size={25} />
                  </span>
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-[8px]">
                      <h1 className="gg-type-section-title">{detail.ability.name}</h1>
                      <ConfigurationStatusBadge status={detail.configuration_status} />
                    </div>
                    <p className="mt-[7px] gg-type-body text-[var(--gg-text-secondary)]">{detail.ability.description}</p>
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-[7px]">
                  <span className="gg-type-caption rounded-full bg-[var(--gg-interaction-soft)] px-[9px] py-[5px] text-[var(--gg-interaction)]">{detail.ability.category}</span>
                  <span className="gg-type-caption rounded-full border border-[var(--gg-line)] px-[9px] py-[5px] text-[var(--gg-text-muted)]">v{detail.ability.version}</span>
                </div>
              </div>
              <p className="mt-[13px] rounded-[11px] border border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] px-[12px] py-[10px] gg-type-caption text-[var(--gg-text-muted)]">
                {detail.configuration_message}
              </p>
            </div>

            <div className="grid gap-[24px] px-[22px] py-[21px]">
              {resourceGroups.map(({ resourceType, resources }) => (
                <ResourceGroup key={resourceType} resourceType={resourceType} resources={resources} />
              ))}
              {detail.providers.length > 0 ? (
                <DetailSection title="平台服务凭据" data-testid="vertical-provider-group">
                  <p className="mb-[12px] gg-type-caption text-[var(--gg-text-muted)]">仅管理员在这里维护 Provider 凭据；配置完成后，所有登录用户都可以在对话框选择该能力包。</p>
                  <div className="grid gap-[16px]">
                    {detail.providers.map((setting) => (
                      <ProviderCredentialCard
                        key={setting.provider}
                        setting={setting}
                        secretValue={secretValues[setting.provider] || ''}
                        saving={savingProvider === setting.provider}
                        verifying={verifyingProvider === setting.provider}
                        onSecretChange={(provider, value) => setSecretValues((current) => ({ ...current, [provider]: value }))}
                        onSave={(item) => void save(item)}
                        onVerify={(item) => void verify(item)}
                      />
                    ))}
                  </div>
                </DetailSection>
              ) : null}
            </div>
          </main>

          <aside className="grid content-start gap-[12px]">
            <section className={DETAIL_PANEL_CLASS}>
              <h2 className="gg-type-card-title">平台范围</h2>
              <div className="mt-[10px] flex flex-wrap gap-[6px]">
                {detail.ability.platform_tags.length > 0 ? detail.ability.platform_tags.map((tag) => (
                  <span key={tag} className="gg-type-caption rounded-full border border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] px-[8px] py-[5px] text-[var(--gg-text-muted)]">{tag}</span>
                )) : <span className="gg-type-caption text-[var(--gg-text-muted)]">由能力包详情声明</span>}
              </div>
            </section>
            <section className={DETAIL_PANEL_CLASS}>
              <h2 className="gg-type-card-title">版本与校验</h2>
              <dl className="mt-[10px] grid gap-[9px]">
                <div className="flex items-start justify-between gap-[10px]">
                  <dt className="gg-type-caption text-[var(--gg-text-muted)]">能力包版本</dt>
                  <dd className="gg-type-caption font-medium text-[var(--gg-text-primary)]">v{detail.ability.version}</dd>
                </div>
                <div className="grid gap-[4px]">
                  <dt className="gg-type-caption text-[var(--gg-text-muted)]">Checksum</dt>
                  <dd className="break-all font-mono gg-type-caption text-[var(--gg-text-secondary)]">{detail.ability.checksum}</dd>
                </div>
              </dl>
            </section>
            <section className={DETAIL_PANEL_CLASS}>
              <h2 className="gg-type-card-title">外部数据说明</h2>
              <p className="mt-[8px] gg-type-meta text-[var(--gg-text-secondary)]">{detail.ability.external_data_notice}</p>
            </section>
          </aside>
        </DetailSurface>
      ) : (
        <div className="mt-[20px] rounded-[var(--gg-radius-card)] border border-dashed border-[var(--gg-line)] bg-[var(--gg-surface)] p-[28px] text-center gg-type-body">
          <p>能力包不存在或当前账号无法查看。</p>
          <Link className="mt-[12px] inline-flex items-center gap-[5px] text-[var(--gg-interaction)] underline underline-offset-2" to={EnterpriseRoute.VerticalAbilitySettings}>
            返回能力包配置
            <ArrowRight className="size-[14px]" aria-hidden="true" />
          </Link>
        </div>
      )}
    </PageShell>
  );
}

export default function VerticalAbilitySettingsPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  /** 根据路由参数选择能力包首页或具体能力包详情页。 */

  const { abilityId } = useParams<{ abilityId?: string }>();
  return abilityId ? (
    <VerticalAbilitySettingsDetailPage
      abilityId={abilityId}
      currentUser={currentUser}
      onLogout={onLogout}
    />
  ) : (
    <VerticalAbilitySettingsCatalogPage currentUser={currentUser} onLogout={onLogout} />
  );
}
