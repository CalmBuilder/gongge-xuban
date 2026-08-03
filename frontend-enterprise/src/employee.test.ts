import { expect, it } from 'vitest';

import {
  employeeAvatarImage,
  expertCapabilityType,
  expertCategory,
  expertReadiness,
  expertSearchText,
  expertSourceLabel,
  expertSubcategory,
  expertTags,
  expertUpstreamUrl,
  expertUnresolvedRequirements,
  employeeProfile,
  isExpertEmployee,
  normalizeProductDisplayText,
} from './employee';
import type { AgentProfileRead } from './types';

it('preserves employee display text', () => {
  expect(normalizeProductDisplayText('售后协调员')).toBe('售后协调员');
});

it('normalizes expert metadata without affecting ordinary employees', () => {
  const expert = {
    id: 'expert', tenant_id: 'tenant', name: '前端开发专家', is_overall: false,
    status: 'active', description: '构建界面', resources: [], created_at: '', updated_at: '',
    metadata: {
      employee_type: 'expert', expert_source_code: 'agency-agents',
      expert_category: '工程研发', expert_subcategory: '前端与客户端', expert_tags: ['React'],
      expert_name_original: 'Frontend Developer',
      expert_capability_manifest: {
        schema_version: '1', capability_type: 'P2', readiness: 'partial',
        required_capabilities: ['prompt_reasoning', 'web_search'],
        resolved_capabilities: ['prompt_reasoning'], unresolved_requirements: ['web_search'],
        orchestration_required: false, core_execution_requires_external_capability: true,
        evidence: ['tools: WebSearch'],
      },
    },
  } satisfies AgentProfileRead;
  expect(isExpertEmployee(expert)).toBe(true);
  expect(expertSourceLabel(expert)).toBe('Agency Agents');
  expect(expertCategory(expert)).toBe('工程研发');
  expect(expertSubcategory(expert)).toBe('前端与客户端');
  expect(expertTags(expert)).toEqual(['React']);
  expect(expertSearchText(expert)).toContain('Frontend Developer React');
  expect(expertCapabilityType(expert)).toBe('P2');
  expect(expertReadiness(expert)).toBe('partial');
  expect(expertUnresolvedRequirements(expert)).toEqual(['web_search']);
  expect(expertUpstreamUrl({
    ...expert,
    metadata: {
      ...expert.metadata,
      upstream_url: 'https://github.com/msitarzewski/agency-agents/blob/abc/engineering/frontend.md',
    },
  })).toContain('/msitarzewski/agency-agents/blob/abc/');
  expect(expertUpstreamUrl({
    ...expert,
    metadata: { ...expert.metadata, upstream_url: 'https://example.com/fake' },
  })).toBe('');
  expect(isExpertEmployee({ ...expert, metadata: {} })).toBe(false);
  expect(expertSubcategory({ ...expert, metadata: {} })).toBe('');
  expect(employeeProfile(expert).roleName).toBe('工程研发');
  const expertProfile = employeeProfile({
    ...expert,
    metadata: { ...expert.metadata, upstream_path: 'engineering/frontend-developer.md' },
  });
  expect(employeeAvatarImage(expertProfile)).toContain('gongge-expert-engineering-');
  expect(employeeAvatarImage({
    ...expertProfile,
    avatarKind: 'upload',
    avatarImage: '/uploads/custom-expert.png',
  })).toBe('/uploads/custom-expert.png');
});

it('treats malformed expert capability metadata as partial without inventing requirements', () => {
  const expert = {
    id: 'expert', tenant_id: 'tenant', name: '专家', is_overall: false, status: 'active',
    resources: [], created_at: '', updated_at: '',
    metadata: { employee_type: 'expert', expert_capability_manifest: { readiness: 'unknown' } },
  } satisfies AgentProfileRead;
  expect(expertCapabilityType(expert)).toBe('P0');
  expect(expertReadiness(expert)).toBe('partial');
  expect(expertUnresolvedRequirements(expert)).toEqual([]);
});
