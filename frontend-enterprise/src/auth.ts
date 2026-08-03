import { BRAND_STORAGE_KEYS } from './lib/brand-storage';

export type EnterpriseAuthUser = {
  id: string;
  tenant_id: string;
  username: string;
  display_name?: string;
  role: 'admin' | 'member';
  membership_status: 'active' | 'suspended' | 'left';
  member_category_code: string;
  joined_at?: string;
  left_at?: string;
  employee_id?: string;
  employee_name?: string;
  department_id?: string;
  employee_status?: string;
  business_role_codes?: string[];
  governance_permission_codes?: string[];
};

export type EnterpriseAuthSession = {
  token: string;
  user: EnterpriseAuthUser;
};

export type EnterpriseContext = {
  tenant: {
    id: string;
    name: string;
  };
  member: EnterpriseAuthUser;
  is_administrator: boolean;
};

export const ENTERPRISE_AUTH_STORAGE_KEY = BRAND_STORAGE_KEYS.authSession;

export function getEnterpriseAuthSession(): EnterpriseAuthSession | null {
  return readStoredSession(ENTERPRISE_AUTH_STORAGE_KEY);
}

export function setEnterpriseAuthSession(session: EnterpriseAuthSession): void {
  window.localStorage.setItem(ENTERPRISE_AUTH_STORAGE_KEY, JSON.stringify(session));
}

export function clearEnterpriseAuthSession(): void {
  window.localStorage.removeItem(ENTERPRISE_AUTH_STORAGE_KEY);
}

function readStoredSession(key: string): EnterpriseAuthSession | null {
  const raw = window.localStorage.getItem(key);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as EnterpriseAuthSession;
    if (!parsed.token || !parsed.user?.id) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function isEnterpriseAdmin(user?: EnterpriseAuthUser | null): boolean {
  return user?.role === 'admin';
}

export function hasGovernancePermission(
  user: EnterpriseAuthUser | null | undefined,
  permissionCode: string,
): boolean {
  return user?.governance_permission_codes?.includes(permissionCode) === true;
}

export function isGalleryEmployee(
  agent?: { published_to_gallery?: boolean; metadata?: Record<string, unknown> } | null,
): boolean {
  return agent?.published_to_gallery ?? agent?.metadata?.published_to_gallery === true;
}

export function isEmployeeOwnedBy(
  agent: {
    owner_user_id?: string;
    owned_by_current_user?: boolean;
    metadata?: Record<string, unknown>;
  },
  user?: EnterpriseAuthUser | null,
): boolean {
  if (!user) return false;
  if (agent.owned_by_current_user !== undefined) return agent.owned_by_current_user;
  if (agent.owner_user_id) return agent.owner_user_id === user.id;
  const metadata = agent.metadata || {};
  const ownerUserId = metadata.owner_user_id;
  return ownerUserId === user.id;
}
