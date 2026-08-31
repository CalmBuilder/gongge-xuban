import type { ChatAttachmentRead, ChatMessage } from '@/types';

export type SessionSlot = {
  serverMessages: ChatMessage[];
  realtimeMessages: ChatMessage[];
};

export type StreamSlot = {
  loading: boolean;
  phase: string;
  timer: number | null;
  accumulated: string;
  turnId: string | null;
  cancelledTurnId: string | null;
  abortController: AbortController | null;
  relayRecoveryStartedAt: number | null;
  relayRecoveryTurnId: string | null;
};

export type TraceSkill = {
  skillId: string;
  name?: string;
  stepId?: string;
  state?: string;
};

export type TraceTool = {
  toolId: string;
  toolCallId?: string;
  toolName: string;
  rawToolName?: string;
  success?: boolean;
  isError?: boolean;
  content?: unknown;
};

export type CotTraceIconName = 'advance' | 'execute' | 'generated' | 'judge' | 'loading' | 'select' | 'tool';

export type TraceLine = {
  id: string;
  kind: 'thinking' | 'decision' | 'skill' | 'tool' | 'code' | 'knowledge';
  text: string;
  detail?: string;
  code?: string;
  language?: string;
  output?: string;
  outputLanguage?: string;
  outputTitle?: string;
  state: 'running' | 'completed' | 'failed';
  collapsible?: boolean;
  icon?: CotTraceIconName;
  placeholder?: boolean;
  provisional?: boolean;
};

export type TurnTrace = {
  lines: TraceLine[];
  startedAt: number;
  completedAt?: number;
};

export type ComposerAttachment = ChatAttachmentRead & {
  uploadStatus: 'uploading' | 'ready' | 'error';
  uploadKey: string;
};

export type ComposerInteractionMode = 'normal' | 'scheduled_task';

/** 对话页本轮执行引擎；auto 保持既有 SOP/普通能力自动路由。 */
export type ChatExecutionEngine = 'auto' | 'dynamic_task';

export type GeneralSkillInstallIntentRead = {
  id: string;
  session_id: string;
  agent_id: string;
  source_kind: string;
  source_reference_redacted?: string;
  source_revision?: string;
  status: 'preparing' | 'awaiting_owner_confirmation' | 'installing' | 'installed' | 'failed' | 'cancelled' | 'expired' | 'stale';
  import_job_id: string;
  raw_checksum?: string;
  normalized_checksum?: string;
  preview_checksum?: string;
  candidates: Array<{
    candidate_id: string;
    name: string;
    description: string;
    risk_findings: string[];
    resources: Array<Record<string, unknown>>;
  }>;
  installed_revision_ids: string[];
  error_code?: string;
  row_version: number;
  created_at: string;
  updated_at: string;
};
export type DraftScheduleType = 'once' | 'daily' | 'weekly' | 'monthly';

export function createEmptySlot(): SessionSlot {
  return { serverMessages: [], realtimeMessages: [] };
}

export function createStreamSlot(): StreamSlot {
  return {
    loading: false,
    phase: '',
    timer: null,
    accumulated: '',
    turnId: null,
    cancelledTurnId: null,
    abortController: null,
    relayRecoveryStartedAt: null,
    relayRecoveryTurnId: null,
  };
}

export function createTurnTrace(): TurnTrace {
  return { lines: [], startedAt: Date.now() };
}
