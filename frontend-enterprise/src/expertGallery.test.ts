import { expect, it } from 'vitest';

import type { AgentProfileRead } from './types';
import {
  expertDepartmentOptions,
  expertDirectionOptions,
  expertSourceOptions,
  filterExpertEmployees,
} from './expertGallery';

function expert(
  id: string,
  category: string,
  direction: string,
  source = 'agency-agents',
  tags: string[] = [],
): AgentProfileRead {
  return {
    id,
    tenant_id: 'tenant',
    name: id,
    description: `${id} 简介`,
    is_overall: false,
    status: 'active',
    resources: [],
    created_at: '',
    updated_at: '',
    metadata: {
      employee_type: 'expert',
      expert_source_code: source,
      expert_category: category,
      expert_subcategory: direction,
      expert_tags: tags,
      role_name: `${category}岗位`,
    },
  };
}

const rows = [
  expert('数据工程师', '工程研发', '数据与数据库', 'agency-agents', ['SQL']),
  expert('前端工程师', '工程研发', '前端与客户端', 'agency-agents', ['React']),
  expert('内容策略师', '市场营销', '内容与社交', 'agency-agents', ['内容']),
  expert('外部顾问', '专业服务', '', 'partner-experts', ['咨询']),
];

it('counts and stably sorts sources and departments', () => {
  expect(expertSourceOptions(rows)).toEqual([
    { value: 'agency-agents', label: 'Agency Agents', count: 3 },
    { value: 'partner-experts', label: 'partner-experts', count: 1 },
  ]);
  expect(expertDepartmentOptions(rows, '')).toEqual([
    { value: '工程研发', label: '工程研发', count: 2 },
    { value: '市场营销', label: '市场营销', count: 1 },
    { value: '专业服务', label: '专业服务', count: 1 },
  ]);
});

it('limits directions by source and department', () => {
  expect(expertDirectionOptions(rows, 'agency-agents', '工程研发')).toEqual([
    { value: '前端与客户端', label: '前端与客户端', count: 1 },
    { value: '数据与数据库', label: '数据与数据库', count: 1 },
  ]);
});

it('combines source department direction and keyword filters', () => {
  expect(filterExpertEmployees(rows, {
    source: 'agency-agents',
    department: '工程研发',
    direction: '数据与数据库',
    keyword: 'SQL',
  }).map((item) => item.name)).toEqual(['数据工程师']);
});

it('keeps experts without a direction in the all-directions view', () => {
  expect(filterExpertEmployees(rows, {
    source: 'partner-experts', department: '专业服务', direction: '', keyword: '',
  }).map((item) => item.name)).toEqual(['外部顾问']);
});
