export type OrganizationUnit = {
  id: string;
  tenant_id: string;
  parent_id: string | null;
  code: string;
  name: string;
  unit_type_code: string;
  tree_path: string;
  depth: number;
  sort_order: number;
  is_root: boolean;
  status: 'active' | 'inactive';
};

export type OrganizationUnitNode = OrganizationUnit & {
  has_children: boolean;
};

export type OrganizationSearchResult = OrganizationUnitNode & {
  path: Array<{ id: string; name: string }>;
};

export type OrganizationSummary = {
  org_unit_id: string;
  direct_member_count: number;
  subtree_member_count: number;
  direct_child_count: number;
  current_leader_count: number;
};

export type MemberPage<T> = {
  items: T[];
  total: number;
  page: number;
  page_size: number;
};

export type Position = {
  id: string;
  tenant_id: string;
  org_unit_id: string;
  code: string;
  name: string;
  position_type_code: string;
  reports_to_position_id: string | null;
  headcount_limit: number | null;
  responsibility: string | null;
  status: 'active' | 'inactive';
};

export type OrganizationAssignment = {
  id: string;
  tenant_id: string;
  employee_profile_id: string;
  org_unit_id: string;
  assignment_type: string;
  is_primary: boolean;
  effective_from: string;
  effective_until: string | null;
  status: 'active' | 'inactive';
  user_id?: string;
  username?: string;
  display_name?: string;
  employee_id?: string;
  employee_name?: string;
};

export type PositionAssignment = {
  id: string;
  tenant_id: string;
  employee_profile_id: string;
  position_id: string;
  assignment_type: string;
  is_primary: boolean;
  effective_from: string;
  effective_until: string | null;
  status: 'active' | 'inactive';
};

export type PositionRoleBinding = {
  id: string;
  tenant_id: string;
  position_id: string;
  business_role_id: string;
  business_role_code: string;
  business_role_name: string;
  scope_mode: 'position_org';
  granted_by_user_id: string | null;
  status: 'active' | 'inactive';
  effective_from: string | null;
  effective_until: string | null;
};

export type OrganizationMember = {
  id: string;
  username: string;
  display_name?: string;
  employee_profile_id?: string;
  employee_id?: string;
  employee_name?: string;
  membership_status: 'active' | 'suspended' | 'left';
};

export type BusinessRoleOption = {
  id: string;
  role_code: string;
  name: string;
  status: 'active' | 'inactive';
};

export type CodeOption = {
  code: string;
  name: string;
  status: 'active' | 'inactive';
};

export type OrganizationLeaderAssignment = {
  id: string;
  tenant_id: string;
  org_unit_id: string;
  employee_profile_id: string;
  position_assignment_id: string | null;
  leader_type_code: string;
  effective_from: string;
  effective_until: string | null;
  status: 'active' | 'inactive';
  source_kind: 'manual';
  created_by_user_id: string | null;
};

export type OrganizationLeaderType = CodeOption & {
  sort_order: number;
};

export type BusinessCodeSet = {
  code: string;
  name: string;
  description: string | null;
  status: 'active' | 'inactive';
  allow_custom_items: boolean;
};

export type BusinessCodeItem = CodeOption & {
  description: string | null;
  is_builtin: boolean;
  sort_order: number;
  revision: number;
};
