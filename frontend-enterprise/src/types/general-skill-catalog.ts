/**
 * @Time       : 2026/08/29 16:45
 * @Author     : zhanglp8181
 * @File       : general-skill-catalog.ts
 * @CallChain  : 内置 Skill 目录 API → 前端目录/详情页面
 * @Description: 定义平台内置 Skill 候选目录、固定快照和外部来源导入的共享类型。
 */

export type BuiltinSkillCatalogItem = {
  id: string;
  slug: string;
  name: string;
  description: string;
  category: string;
  stability: 'stable' | 'beta' | 'misc';
  risk_level: 'low' | 'medium' | 'high';
  risk_findings: string[];
  invocation_policy: 'model_allowed' | 'user_only';
  runtime_mode: 'guidance_only' | 'sandboxed';
  source_kind: string;
  review_status: string;
  status: 'draft' | 'published' | 'rejected' | 'archived';
  source_repository: string;
  source_revision: string;
  source_path: string;
  source_license: string;
  source_package_checksum: string;
  source_normalized_checksum: string;
  content_checksum: string;
  manifest_checksum: string;
  revision_id: string | null;
  revision_number: number | null;
  revision_status: string | null;
  resource_count: number;
  row_version: number;
  revision_row_version: number | null;
  updated_at: string;
  name_zh?: string | null;
  description_zh?: string | null;
  localization_status?: string | null;
  localization_source_content_checksum?: string | null;
  localization_checksum?: string | null;
};

export type BuiltinSkillResource = {
  relative_path: string;
  content_checksum: string;
  size: number;
  media_type: string;
  is_text: boolean;
};

export type BuiltinSkillCatalogBindingSummary = {
  binding_id: string;
  agent_id: string;
  agent_name: string;
  governance_form: string;
  status: string;
  revision_policy: string;
  pinned_revision_id: string | null;
  invocation_policy: 'model_allowed' | 'user_only';
  row_version: number;
};

export type BuiltinSkillCatalogDetail = BuiltinSkillCatalogItem & {
  skill_markdown: string;
  explanation_markdown_zh?: string | null;
  parsed_metadata: Record<string, unknown>;
  allowed_tools: string[];
  argument_hint: string | null;
  metadata: Record<string, unknown>;
  resources: BuiltinSkillResource[];
  bindings: BuiltinSkillCatalogBindingSummary[];
};

export type BuiltinSkillCatalogFacets = {
  category: Record<string, number>;
  source_kind: Record<string, number>;
  stability: Record<string, number>;
  risk_level: Record<string, number>;
  invocation_policy: Record<string, number>;
  status: Record<string, number>;
};

export type BuiltinSkillCatalogPage = {
  items: BuiltinSkillCatalogItem[];
  total: number;
  page: number;
  page_size: number;
  facets: BuiltinSkillCatalogFacets;
};

export type BuiltinSkillCatalogImportResult = {
  command_id: string;
  replayed: boolean;
  created_count: number;
  existing_count: number;
  skill_count: number;
  source_repository: string;
  source_revision: string;
  source_license: string;
  source_package_checksum: string;
  source_normalized_checksum: string;
  items: Array<Record<string, unknown>>;
};

export type ExternalSkillCatalogSourceKind = 'github' | 'https' | 'skillhub';

export type ExternalSkillCatalogImportRequest = {
  tenant_id: string;
  command_id: string;
  source_kind: ExternalSkillCatalogSourceKind;
  source_url: string;
  source_license: string;
  revision: string | null;
  source_subpath: string | null;
};

export type ExternalSkillCatalogImportResult = BuiltinSkillCatalogImportResult & {
  source_kind: ExternalSkillCatalogSourceKind;
  source_url: string;
};

export type BuiltinSkillCatalogReviewDecision = 'approve' | 'reject';

export type BuiltinSkillCatalogReviewItem = {
  skill_id: string;
  decision: BuiltinSkillCatalogReviewDecision;
  expected_skill_row_version: number;
  expected_revision_row_version: number;
  review_note: string | null;
};

export type BuiltinSkillCatalogReviewRequest = {
  tenant_id: string;
  command_id: string;
  items: BuiltinSkillCatalogReviewItem[];
};

export type BuiltinSkillCatalogReviewResult = {
  command_id: string;
  replayed: boolean;
  approved_count: number;
  rejected_count: number;
  items: Array<Record<string, unknown>>;
};

export type BuiltinSkillCatalogBindingRequest = {
  tenant_id: string;
  skill_id: string;
  agent_id: string;
  mode: 'install' | 'bind';
  revision_policy: 'pinned' | 'follow_latest';
  pinned_revision_id: string | null;
  invocation_policy: 'model_allowed' | 'user_only';
};

export type BuiltinSkillCatalogBindingResult = {
  action: 'created' | 'updated' | 'unchanged';
  mode: 'install' | 'bind';
  binding_id: string;
  agent_id: string;
  skill_id: string;
  status: string;
  revision_policy: string;
  pinned_revision_id: string | null;
  invocation_policy: string;
  row_version: number;
};

export type BuiltinSkillCatalogLifecycleAction = 'archive' | 'revoke';

export type BuiltinSkillCatalogLifecycleRequest = {
  tenant_id: string;
  command_id: string;
  skill_id: string;
  action: BuiltinSkillCatalogLifecycleAction;
  expected_skill_row_version: number;
  expected_revision_row_version: number;
  reason: string;
};

export type BuiltinSkillCatalogLifecycleResult = {
  command_id: string;
  replayed: boolean;
  action: BuiltinSkillCatalogLifecycleAction;
  skill_id: string;
  slug: string;
  skill_status: 'draft' | 'published' | 'rejected' | 'archived';
  revision_id: string;
  revision_status: string;
  skill_row_version: number;
  revision_row_version: number;
  deactivated_binding_count: number;
};
