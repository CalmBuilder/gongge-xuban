import type { VerticalAbilityRead } from './index';

export type VerticalAbilityCredentialSource = 'database' | 'environment' | 'none';

export type VerticalAbilityHealthStatus =
  | 'unverified'
  | 'healthy'
  | 'degraded'
  | 'unhealthy';

export type VerticalAbilityProviderSetting = {
  provider: string;
  ability_ids: string[];
  ability_names: string[];
  provider_name: string;
  description: string;
  credential_label: string;
  credential_placeholder: string;
  credential_setup_text?: string | null;
  credential_setup_url?: string | null;
  credential_setup_link_label?: string | null;
  supports_verification: boolean;
  configured: boolean;
  source: VerticalAbilityCredentialSource;
  credential_masked?: string | null;
  revision: number;
  health_status: VerticalAbilityHealthStatus;
  health_error_code?: string | null;
  last_checked_at?: string | null;
  last_healthy_at?: string | null;
  updated_at?: string | null;
  verification_success?: boolean | null;
  message?: string | null;
};

export type VerticalAbilitySettingsRead = {
  abilities: VerticalAbilityConfigurationSummary[];
  providers: VerticalAbilityProviderSetting[];
};

export type VerticalAbilityConfigurationStatus = 'ready' | 'partial' | 'needs_configuration';

export type VerticalAbilityResourceType = 'platform_secret' | 'connection' | 'runtime' | 'public';

export type VerticalAbilityResourceStatus =
  | 'ready'
  | 'needs_configuration'
  | 'not_connected'
  | 'diagnostic';

export type VerticalAbilityConfigurationSummary = {
  id: string;
  name: string;
  category: string;
  icon_key: string;
  description: string;
  platform_tags: string[];
  version: string;
  checksum: string;
  configuration_status: VerticalAbilityConfigurationStatus;
  configuration_message: string;
  resource_count: number;
  ready_resource_count: number;
  provider_ids: string[];
};

export type VerticalAbilityResource = {
  resource_id: string;
  resource_type: VerticalAbilityResourceType;
  name: string;
  description: string;
  provider?: string | null;
  status: VerticalAbilityResourceStatus;
  status_message: string;
};

export type VerticalAbilityConfigurationDetail = {
  ability: VerticalAbilityRead;
  configuration_status: VerticalAbilityConfigurationStatus;
  configuration_message: string;
  resources: VerticalAbilityResource[];
  providers: VerticalAbilityProviderSetting[];
};

export type JinaCredentialRead = VerticalAbilityProviderSetting & {
  provider: 'jina';
};
