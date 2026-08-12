/**
 * @Time       : 2026/08/11 23:58
 * @Author     : zhanglp8181
 * @File       : standing-approvals.ts
 * @CallChain  : ScheduledTaskStandingApprovalDialog → API client → FastAPI
 * @Description: 集中封装长期批准候选、规则创建/查询和 CAS 撤销请求。
 */

import { api, getRequestTenantId } from './client';
import { createClientId } from '@/lib/client-id';
import type {
  StandingApprovalCandidate,
  StandingApprovalRule,
} from '@/types/standing-approvals';

export function listStandingApprovalCandidates(
  scheduleId: string,
): Promise<StandingApprovalCandidate[]> {
  /** 读取当前管理人可为指定调度选择的精确企业微信会话。 */

  const params = new URLSearchParams({
    tenant_id: getRequestTenantId(),
    source_schedule_id: scheduleId,
  });
  return api.get(`/api/standing-approval-rules/candidates?${params.toString()}`);
}

export function listStandingApprovalRules(scheduleId: string): Promise<StandingApprovalRule[]> {
  /** 读取指定调度的活动和已撤销规则，支持治理追溯。 */

  const params = new URLSearchParams({
    tenant_id: getRequestTenantId(),
    source_schedule_id: scheduleId,
  });
  return api.get(`/api/standing-approval-rules?${params.toString()}`);
}

export function createStandingApprovalRule(input: {
  scheduleId: string;
  agentId: string;
  candidate: StandingApprovalCandidate;
  exactContent: string;
  validDays: number;
}): Promise<StandingApprovalRule> {
  /** 创建只允许精确正文和精确会话的有期限规则。 */

  const validFrom = new Date();
  const validTo = new Date(validFrom.getTime() + input.validDays * 24 * 60 * 60 * 1000);
  return api.post('/api/standing-approval-rules', {
    tenant_id: getRequestTenantId(),
    command_id: createClientId(),
    agent_id: input.agentId,
    source_schedule_id: input.scheduleId,
    profile_id: input.candidate.profile_id,
    thread_binding_id: input.candidate.thread_binding_id,
    tool_action: 'wecom.message_send',
    argument_constraints: { content: { equals: input.exactContent } },
    valid_from: validFrom.toISOString(),
    valid_to: validTo.toISOString(),
  });
}

export function revokeStandingApprovalRule(
  rule: Pick<StandingApprovalRule, 'id' | 'revision'>,
): Promise<StandingApprovalRule> {
  /** 以当前 revision 撤销规则；服务端不提供恢复或原地扩权。 */

  return api.post(`/api/standing-approval-rules/${rule.id}/revoke`, {
    tenant_id: getRequestTenantId(),
    command_id: createClientId(),
    expected_revision: rule.revision,
  });
}
