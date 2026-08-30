import { useEffect, useState } from 'react';

import { api } from '@/api/client';
import type { MemberPage, OrganizationMember } from '@/types/organization';

import { Input, Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui';

type RemoteMemberSelectProps = {
  tenantId: string;
  value: string;
  onValueChange: (value: string) => void;
  ariaLabel: string;
  placeholder?: string;
  orgUnitId?: string;
  includeDescendants?: boolean;
  excludeUserId?: string;
  allowNone?: boolean;
  noneLabel?: string;
  valueField?: 'employee_profile_id' | 'user_id';
};

export function RemoteMemberSelect({
  tenantId,
  value,
  onValueChange,
  ariaLabel,
  placeholder = '选择成员',
  orgUnitId,
  includeDescendants = false,
  excludeUserId,
  allowNone = false,
  noneLabel = '暂不指定',
  valueField = 'employee_profile_id',
}: RemoteMemberSelectProps) {
  const [keyword, setKeyword] = useState('');
  const [members, setMembers] = useState<OrganizationMember[]>([]);
  const [error, setError] = useState('');

  useEffect(() => {
    const timeout = window.setTimeout(async () => {
      const params = new URLSearchParams({
        tenant_id: tenantId,
        page: '1',
        page_size: '50',
        membership_status: 'active',
      });
      if (keyword.trim()) params.set('keyword', keyword.trim());
      if (orgUnitId) {
        params.set('org_unit_id', orgUnitId);
        params.set('include_descendants', String(includeDescendants));
      }
      try {
        const result = await api.get<MemberPage<OrganizationMember>>(
          `/api/auth/users/page?${params.toString()}`,
        );
        setMembers(result.items.filter(
          (member) => Boolean(member.employee_profile_id) && member.id !== excludeUserId,
        ));
        setError('');
      } catch (requestError) {
        setError(requestError instanceof Error ? requestError.message : '成员搜索失败');
      }
    }, keyword ? 250 : 0);
    return () => window.clearTimeout(timeout);
  }, [excludeUserId, includeDescendants, keyword, orgUnitId, tenantId]);

  return (
    <div className="grid gap-[6px]">
      <Input
        aria-label={`搜索${ariaLabel}`}
        value={keyword}
        placeholder="输入姓名、账号或工号搜索"
        onChange={(event) => setKeyword(event.target.value)}
      />
      <Select value={value} onValueChange={onValueChange}>
        <SelectTrigger aria-label={ariaLabel}>
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent>
          {allowNone ? <SelectItem value="__none__">{noneLabel}</SelectItem> : null}
          {members.map((member) => (
            <SelectItem
              key={member.employee_profile_id}
              value={(valueField === 'user_id' ? member.id : member.employee_profile_id) as string}
            >
              {member.employee_name || member.display_name || member.username}
              {member.employee_id ? ` · ${member.employee_id}` : ''}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {error ? <p className="gg-type-caption text-[#b42318]">{error}</p> : null}
    </div>
  );
}
