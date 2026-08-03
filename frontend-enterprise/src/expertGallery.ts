import {
  employeeDisplayName,
  employeeDisplayNameWithCreator,
  employeeProfile,
  expertCategory,
  expertSearchText,
  expertSourceCode,
  expertSourceLabel,
  expertSubcategory,
} from './employee';
import type { AgentProfileRead } from './types';

export type ExpertCountOption = {
  value: string;
  label: string;
  count: number;
};

export type ExpertGalleryFilters = {
  source: string;
  department: string;
  direction: string;
  keyword: string;
};

function countOptions(
  rows: AgentProfileRead[],
  valueOf: (row: AgentProfileRead) => string,
  labelOf: (row: AgentProfileRead) => string = valueOf,
): ExpertCountOption[] {
  const options = new Map<string, ExpertCountOption>();
  rows.forEach((row) => {
    const value = valueOf(row);
    if (!value) return;
    const current = options.get(value);
    options.set(value, {
      value,
      label: current?.label || labelOf(row) || value,
      count: (current?.count || 0) + 1,
    });
  });
  return [...options.values()].sort((left, right) => (
    right.count - left.count || left.label.localeCompare(right.label, 'zh-CN')
  ));
}

export function expertSourceOptions(rows: AgentProfileRead[]): ExpertCountOption[] {
  return countOptions(rows, expertSourceCode, expertSourceLabel);
}

export function expertDepartmentOptions(
  rows: AgentProfileRead[],
  source: string,
): ExpertCountOption[] {
  return countOptions(
    rows.filter((row) => !source || expertSourceCode(row) === source),
    expertCategory,
  );
}

export function expertDirectionOptions(
  rows: AgentProfileRead[],
  source: string,
  department: string,
): ExpertCountOption[] {
  return countOptions(
    rows
      .filter((row) => !source || expertSourceCode(row) === source)
      .filter((row) => !department || expertCategory(row) === department),
    expertSubcategory,
  );
}

export function filterExpertEmployees(
  rows: AgentProfileRead[],
  filters: ExpertGalleryFilters,
): AgentProfileRead[] {
  const keyword = filters.keyword.trim().toLowerCase();
  return rows.filter((row) => {
    if (filters.source && expertSourceCode(row) !== filters.source) return false;
    if (filters.department && expertCategory(row) !== filters.department) return false;
    if (filters.direction && expertSubcategory(row) !== filters.direction) return false;
    if (!keyword) return true;
    const profile = employeeProfile(row);
    return [
      employeeDisplayName(row),
      employeeDisplayNameWithCreator(row),
      profile.roleName,
      row.description || '',
      profile.workStyles.join(' '),
      profile.expertiseTags.join(' '),
      expertSearchText(row),
    ].some((value) => value.toLowerCase().includes(keyword));
  });
}
