import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  ExperimentOutlined,
  GithubOutlined,
  PlusOutlined,
  TeamOutlined,
  UploadOutlined,
} from '../icons';
import type { ChangeEvent, DragEvent, HTMLAttributes, ReactNode } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { Ban, CircleCheck, Copy, FileArchive, ShieldCheck, Users } from 'lucide-react';
import { ContextMenu } from 'radix-ui';

import { api, streamPost, getRequestTenantId } from '../api/client';
import { isEnterpriseAdmin, type EnterpriseAuthUser } from '../auth';
import AppHeader from '@/components/AppHeader';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { ModelConfigDropdown } from '@/components/ModelConfigDropdown';
import { Paginator } from '@/components/Paginator';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Input,
  Select as UISelect,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';
import {
  MENU_CONTENT_CLASS,
  MENU_ITEM_CLASS,
  MENU_ITEM_DANGER_CLASS,
  MOBILE_CARD_CLASS,
  SELECT_TRIGGER_CLASS,
  formatDateTime,
} from '@/lib/enterprise-ui';
import { StatCard } from '@/components/StatCard';
import { SkillGovernanceDialog } from '@/components/general-skills/SkillGovernanceDialog';
import { ResourceImportDialog } from '@/components/ResourceImportDialog';
import PlazaResourceIcon from '@/components/openPlatform/PlazaResourceIcon';
import CodeBlock, { renderCodeTokens } from '../components/CodeBlock';
import IconAdd from '../assets/icons/add.svg?react';
import IconArrowRight from '../assets/icons/arrow-right.svg?react';
import IconFolder from '../assets/icons/cap-folder.svg?react';
import IconChevronDown from '../assets/icons/chevron-down.svg?react';
import IconPlay from '../assets/icons/play.svg?react';
import IconClear from '../assets/icons/field-clear.svg?react';
import IconEdit from '../assets/icons/edit.svg?react';
import IconMore from '../assets/icons/more.svg?react';
import IconRefresh from '../assets/icons/refresh.svg?react';
import IconProfileFile from '../assets/icons/profile-file.svg?react';
import IconSearch from '../assets/icons/search.svg?react';
import IconTrash from '../assets/icons/trash.svg?react';
import {
  canManageEmployeeAgent,
  openGalleryAgentId,
  openGalleryImportSourceOptions,
  resourceCreatorName,
  visibleEmployeeAgents,
} from '../employee';
import { useClientPagination } from '../hooks/useClientPagination';
import { StatusBadge } from './scheduled-tasks/StatusBadge';
import type { BadgeTone } from './scheduled-tasks/shared';
import type {
  AgentProfileRead,
  GeneralSkillImportJobRead,
  GeneralSkillSourceCredentialRead,
  GeneralSkillRead,
  GeneralSkillRunResponse,
  ModelConfigRead,
} from '../types';

const GENERAL_SKILL_PAGE_SIZE = 10;
const GENERAL_SKILL_RUN_MODEL_STORAGE_KEY = 'general-skill-run-model';
const GENERAL_SKILL_IMPORT_PROCESSING_STATUSES = new Set([
  'created',
  'fetching',
  'fetched',
  'normalizing',
  'normalized',
  'analyzing',
]);

const STATUS_BADGE: Record<GeneralSkillRead['status'], { tone: BadgeTone; text: string }> = {
  draft: { tone: 'blue', text: '草稿' },
  published: { tone: 'green', text: '已启用' },
  archived: { tone: 'gray', text: '已停用' },
};

const EMPTY_SKILL_MARKDOWN = `# 技能说明

在这里编写技能文档。名称、Slug 和描述由上方表单维护，系统不会从文档中自动抽取。`;

const SECTION_CARD_CLASS =
  'flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-[#FFF] p-[18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]';
const SECTION_CARD_TITLE_CLASS = 'text-[14px] font-medium text-[#18181a]';
const FIELD_LABEL_CLASS = 'text-[13px] font-medium text-[#18181a]';
const RETURN_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-5 text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6]! hover:bg-white! hover:text-[#18181a]! aria-expanded:border-[#cbd3e6]! aria-expanded:bg-white! aria-expanded:text-[#18181a]!';
const PRIMARY_BUTTON_CLASS =
  'h-8 gap-1 rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-5 text-[12px] font-semibold text-white hover:bg-[#244bc7]';
const DELETE_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-5 text-[12px] font-normal text-[#d20b0b] hover:border-[#f3b6b6]! hover:bg-[#fce7e7]! hover:text-[#d20b0b]! aria-expanded:border-[#f3b6b6]! aria-expanded:bg-[#fce7e7]! aria-expanded:text-[#d20b0b]!';
const EDITOR_ACTION_OUTLINE_CLASS = RETURN_BUTTON_CLASS;
const EDITOR_ACTION_PRIMARY_CLASS = PRIMARY_BUTTON_CLASS;
const HIDDEN_FILE_INPUT_CLASS =
  'pointer-events-none fixed size-px opacity-0 [inset:auto_auto_0_0]';
const SKILL_EDITOR_DRAG_ACTIVE_CLASS =
  'ring-1 ring-[color-mix(in_srgb,var(--gg-cobalt)_24%,transparent)] shadow-[0_-4px_16px_0_rgba(49,87,232,0.08)]';
const SKILL_DROP_HINT_CLASS =
  'pointer-events-none absolute inset-x-[18px] bottom-[18px] top-[46px] z-[6] flex items-center justify-center gap-3 rounded-[14px] border border-dashed border-[var(--gg-cobalt)] bg-white/90 text-[15px] font-semibold text-[var(--gg-cobalt)] shadow-sm backdrop-blur-sm';
const SKILL_FILE_EDITOR_CLASS =
  'grid min-h-[560px] flex-1 grid-cols-[minmax(180px,240px)_minmax(0,1fr)] overflow-hidden border-t border-[#e3e7f1] bg-[#fafafa]';
const SKILL_FILE_TREE_CLASS =
  'grid min-w-0 grid-rows-[auto_minmax(0,1fr)_auto] border-r border-[#e3e7f1] bg-white';
const SKILL_FILE_TREE_HEADER_CLASS =
  'flex min-h-[44px] items-center gap-2 border-b border-[#e3e7f1] bg-[#f6f6f6] px-[14px] text-[12px] font-medium text-[#757f9c]';
const SKILL_FILE_TREE_LIST_CLASS =
  'min-h-0 overflow-auto bg-white p-2';
const SKILL_FILE_TREE_ACTIONS_CLASS =
  'flex gap-2 border-t border-[#e3e7f1] bg-white p-[10px]';
const SKILL_FILE_PANE_CLASS =
  'grid min-w-0 grid-rows-[auto_minmax(0,1fr)]';
const SKILL_FILE_TAB_CLASS =
  'flex min-h-[44px] items-center gap-2 border-b border-[#e3e7f1] bg-[#f6f6f6] px-[14px] text-[12px] font-medium text-[#757f9c]';
const SKILL_CODE_EDITOR_CLASS =
  'relative min-h-0 overflow-hidden bg-[#fafafa] font-mono text-[13px] leading-[1.7] tab-[2] shadow-[inset_0_1px_0_#e3e7f1]';
const SKILL_CODE_HIGHLIGHT_CLASS =
  'pointer-events-none absolute inset-0 z-[1] m-0 overflow-hidden whitespace-pre p-[18px_20px] text-[#18181a] tab-[2]';
const SKILL_CODE_HIGHLIGHT_CODE_CLASS =
  'block w-max min-w-full font-[inherit] will-change-transform';
const SKILL_CODE_INPUT_CLASS =
  'absolute inset-0 z-[2] m-0 size-full min-h-0 resize-none overflow-auto rounded-none border-0 bg-transparent! p-[18px_20px] font-[inherit] leading-[inherit] tracking-normal whitespace-pre text-transparent caret-[#18181a] outline-none tab-[2] [scrollbar-gutter:stable] selection:bg-[rgba(0,120,215,0.24)] [-webkit-text-fill-color:transparent]';
const SKILL_RESULT_LAYOUT_CLASS = 'grid gap-5';
const SKILL_SECTION_LABEL_CLASS =
  'mb-2 text-[12px] font-semibold text-[#757f9c]';
const SKILL_REPLY_PANEL_CLASS =
  'rounded-xl border border-[#eceef1] bg-white p-[16px_18px]';
const SKILL_REPLY_TEXT_CLASS =
  'mb-0! text-[15px] leading-[1.8] text-[#18181a]';
const SKILL_TRACE_LIST_CLASS =
  'grid gap-[10px] rounded-xl border border-[#eceef1] bg-[#fbfcfd] p-[12px_14px]';
const SKILL_TRACE_ITEM_CLASS =
  'grid min-w-0 grid-cols-[12px_minmax(0,1fr)] gap-[10px]';
const SKILL_TRACE_ITEM_BODY_CLASS = 'min-w-0 max-w-full';
const SKILL_TRACE_DOT_CLASS =
  'mt-[9px] size-[7px] shrink-0 rounded-full bg-[#18181a]';
const SKILL_TRACE_TITLE_CLASS =
  'text-[13px] font-semibold text-[#18181a]';
const SKILL_TRACE_MESSAGE_CLASS =
  'mt-[2px] break-words text-[12px] leading-[1.55] text-[#757f9c]';
const SKILL_TRACE_CODE_DETAILS_CLASS =
  'group/gs-trace box-border w-full min-w-0 max-w-full overflow-hidden rounded-xl border border-[#eceef1] bg-white';
const SKILL_TRACE_CODE_SUMMARY_CLASS =
  'flex min-h-[38px] cursor-pointer list-none items-center gap-2 px-3 py-[9px] text-[12px] font-semibold text-[#18181a] select-none group-open/gs-trace:border-b group-open/gs-trace:border-[#eceef1] [&::-webkit-details-marker]:hidden';
const SKILL_CODE_BLOCK_CLASS =
  'm-0 max-h-[520px] max-w-full overflow-auto whitespace-pre border-0 p-[16px_18px] font-mono text-[12px] leading-[1.65]';
const SKILL_OUTPUT_STACK_CLASS = 'grid gap-[10px]';

function skillFileNodeClass(active: boolean) {
  return cn(
    'flex w-full min-w-0 cursor-pointer items-center gap-2 rounded-lg border-0 bg-transparent px-[10px] py-2 text-left text-[12px] text-[#757f9c] transition-[background,color,box-shadow] duration-150',
    'hover:bg-[#f6f6f6] hover:text-[#18181a]',
    active && 'bg-[#f6f6f6] text-[#18181a]',
  );
}

function TraceDisclosureLabel() {
  return (
    <span className="ml-auto text-[12px] font-medium text-[#757f9c]">
      <span className="group-open/gs-trace:hidden">展开</span>
      <span className="hidden group-open/gs-trace:inline">收起</span>
    </span>
  );
}

const ENTERPRISE_AGENT_STORAGE_KEY = 'gongge_enterprise_agent_scope';
const GENERAL_SKILL_IMPORT_JOB_STORAGE_PREFIX = 'gongge_general_skill_import_job';
const GENERAL_SKILL_RUN_TIMEOUT_MS = 120_000;
const FOLDER_INPUT_PROPS = {
  webkitdirectory: '',
  directory: '',
} as Record<string, string>;

type GeneralSkillFile = {
  path: string;
  content: string;
  size?: number;
  mime_type?: string;
};

type DroppedSkillFile = {
  file: File;
  path: string;
};

type GeneralSkillImportMode = 'plaza' | 'employee';
type SkillDependencyDecision = 'required' | 'optional' | 'ignored';
type SecureSkillImportSourceKind = 'upload' | 'folder' | 'github' | 'skillhub' | 'https';

type SkillFileSystemEntry = {
  name: string;
  fullPath: string;
  isFile: boolean;
  isDirectory: boolean;
};

type SkillFileEntry = SkillFileSystemEntry & {
  file: (success: (file: File) => void, failure?: (error: DOMException) => void) => void;
};

type SkillDirectoryEntry = SkillFileSystemEntry & {
  createReader: () => {
    readEntries: (
      success: (entries: SkillFileSystemEntry[]) => void,
      failure?: (error: DOMException) => void,
    ) => void;
  };
};

const PHASE_LABELS: Record<string, string> = {
  skill_loaded: '加载技能',
  planning: '生成执行方案',
  plan_created: '生成代码',
  attempt_started: '开始运行',
  running_code: '运行代码',
  stdout_chunk: '运行输出',
  stderr_chunk: '错误输出',
  code_finished: '读取运行结果',
  code_timeout: '运行超时',
  reflection_passed: '校验通过',
  reflection_retrying: '反思修复',
  reflection_stopped: '停止重试',
  repair_planning: '重新生成代码',
  repair_failed: '修复失败',
  plan_failed: '生成失败',
  replying: '生成回复',
  reply_created: '完成回复',
  reply_failed: '回复失败',
};

function formatJson(value: unknown): string {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}

function codeLanguage(value: string, fallback = 'text'): string {
  const trimmed = value.trim();
  if (!trimmed) return fallback;
  try {
    JSON.parse(trimmed);
    return 'json';
  } catch {
    return fallback;
  }
}

function isSkillPackageArchive(file: File): boolean {
  const name = file.name.toLowerCase();
  const type = file.type.toLowerCase();
  return name.endsWith('.zip') || type === 'application/zip' || type === 'application/x-zip-compressed';
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const value = String(reader.result || '');
      resolve(value.includes(',') ? value.split(',', 2)[1] : value);
    };
    reader.onerror = () => reject(reader.error || new Error('读取文件失败'));
    reader.readAsDataURL(file);
  });
}

function RunCodePanel({
  title,
  code,
  language,
  defaultOpen = false,
  className,
}: {
  title: string;
  code: string;
  language?: string;
  defaultOpen?: boolean;
  className?: string;
}) {
  return (
    <details className={cn(SKILL_TRACE_CODE_DETAILS_CLASS, 'mt-0', className)} open={defaultOpen}>
      <summary className={SKILL_TRACE_CODE_SUMMARY_CLASS}>
        {title}
        <TraceDisclosureLabel />
      </summary>
      <CodeBlock className={SKILL_CODE_BLOCK_CLASS} code={code} language={language || codeLanguage(code)} />
    </details>
  );
}

type GeneralSkillPageProps = {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
};

export function GeneralSkillNewPage(props: GeneralSkillPageProps = {}) {
  return <GeneralSkillEditorPage mode="new" {...props} />;
}

export function GeneralSkillEditPage(props: GeneralSkillPageProps = {}) {
  return <GeneralSkillEditorPage mode="edit" {...props} />;
}

export default function GeneralSkillsPage({ embedded = false, currentUser, onLogout }: { embedded?: boolean } & GeneralSkillPageProps) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [rows, setRows] = useState<GeneralSkillRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | GeneralSkillRead['status']>('all');
  const [agentId, setAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [isOverallAgent, setIsOverallAgent] = useState(true);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [clawhubModalOpen, setClawhubModalOpen] = useState(false);
  const [clawhubSource, setClawhubSource] = useState('');
  const [clawhubLoading, setClawhubLoading] = useState(false);
  const clawhubAbortRef = useRef<AbortController | null>(null);
  const [agentImportOpen, setAgentImportOpen] = useState(false);
  const [agentImportMode, setAgentImportMode] = useState<GeneralSkillImportMode>('plaza');
  const [agentImportLoading, setAgentImportLoading] = useState(false);
  const [agentImportAgents, setAgentImportAgents] = useState<AgentProfileRead[]>([]);
  const [agentImportSourceAgentId, setAgentImportSourceAgentId] = useState('');
  const [agentImportSourceSkills, setAgentImportSourceSkills] = useState<GeneralSkillRead[]>([]);
  const [agentImportSelectedSkillIds, setAgentImportSelectedSkillIds] = useState<string[]>([]);
  const [agentScopeLoaded, setAgentScopeLoaded] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<GeneralSkillRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [secureImportAvailable, setSecureImportAvailable] = useState(false);
  const [secureImportSourceKinds, setSecureImportSourceKinds] = useState<SecureSkillImportSourceKind[]>([]);
  const [secureImportOpen, setSecureImportOpen] = useState(false);
  const [secureImportSourceKind, setSecureImportSourceKind] = useState<SecureSkillImportSourceKind>('upload');
  const [secureImportFile, setSecureImportFile] = useState<File | null>(null);
  const [secureImportFolderFiles, setSecureImportFolderFiles] = useState<File[]>([]);
  const [secureImportSourceUrl, setSecureImportSourceUrl] = useState('');
  const [secureImportRevision, setSecureImportRevision] = useState('');
  const [secureImportSubpath, setSecureImportSubpath] = useState('skills');
  const [secureImportJob, setSecureImportJob] = useState<GeneralSkillImportJobRead | null>(null);
  const [secureImportSelectedIds, setSecureImportSelectedIds] = useState<string[]>([]);
  const [secureImportDependencyDecisions, setSecureImportDependencyDecisions] = useState<Record<string, SkillDependencyDecision>>({});
  const [secureImportRetryParentId, setSecureImportRetryParentId] = useState<string | null>(null);
  const [secureImportLoading, setSecureImportLoading] = useState(false);
  const [secureImportCredentials, setSecureImportCredentials] = useState<GeneralSkillSourceCredentialRead[]>([]);
  const [secureImportCredentialId, setSecureImportCredentialId] = useState('');
  const [secureImportCredentialName, setSecureImportCredentialName] = useState('');
  const [secureImportCredentialToken, setSecureImportCredentialToken] = useState('');
  const [secureImportCredentialLoading, setSecureImportCredentialLoading] = useState(false);
  const [secureImportTargetSkillId, setSecureImportTargetSkillId] = useState<string | null>(null);
  const [governanceTarget, setGovernanceTarget] = useState<GeneralSkillRead | null>(null);

  const pageTitle = isOverallAgent ? '技能广场' : '技能';
  const listLabel = isOverallAgent ? '技能广场列表' : '技能列表';
  const currentAgent = useMemo(() => agents.find((item) => item.id === agentId), [agents, agentId]);
  const canManageCurrentScope = currentAgent
    ? canManageEmployeeAgent(currentAgent, currentUser)
    : isEnterpriseAdmin(currentUser) && isOverallAgent;
  const secureImportStorageKey = [
    GENERAL_SKILL_IMPORT_JOB_STORAGE_PREFIX,
    currentUser?.tenant_id || 'unknown-tenant',
    currentUser?.id || 'unknown-user',
    agentId || 'no-agent',
  ].join(':');

  const load = () => {
    const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
    setLoading(true);
    return api
      .get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${getRequestTenantId()}${agentSuffix}`)
      .then(setRows)
      .catch((error) => notify.error(error.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  useEffect(() => {
    api
      .get<{ enabled: boolean; source_kinds: string[] }>('/api/enterprise/general-skill-import-jobs/capabilities')
      .then((capabilities) => {
        const sourceKinds = capabilities.source_kinds.filter(
          (kind): kind is SecureSkillImportSourceKind => ['upload', 'github', 'skillhub', 'https'].includes(kind),
        );
        setSecureImportSourceKinds(sourceKinds);
        setSecureImportAvailable(capabilities.enabled && sourceKinds.includes('upload'));
      })
      .catch(() => {
        setSecureImportSourceKinds([]);
        setSecureImportAvailable(false);
      });
  }, []);

  useEffect(() => {
    if (!secureImportAvailable || !agentId || isOverallAgent) return;
    const jobId = window.localStorage.getItem(secureImportStorageKey);
    if (!jobId) return;
    api
      .get<GeneralSkillImportJobRead>(
        `/api/enterprise/general-skill-import-jobs/${encodeURIComponent(jobId)}`,
      )
      .then((job) => {
        if (['installed', 'cancelled', 'expired'].includes(job.status)) {
          window.localStorage.removeItem(secureImportStorageKey);
          return;
        }
        setSecureImportJob(job);
        if (job.status === 'awaiting_approval') {
          setSecureImportSelectedIds(job.candidates.map((candidate) => candidate.candidate_id));
          setSecureImportDependencyDecisions(defaultDependencyDecisions(job));
        }
        setSecureImportOpen(true);
      })
      .catch(() => window.localStorage.removeItem(secureImportStorageKey));
  }, [agentId, isOverallAgent, secureImportAvailable, secureImportStorageKey]);

  useEffect(() => {
    const job = secureImportJob;
    if (!secureImportOpen || !job || !isSkillImportProcessing(job.status)) return;
    let disposed = false;
    let requestInFlight = false;
    const poll = async () => {
      if (requestInFlight) return;
      requestInFlight = true;
      try {
        const next = await api.get<GeneralSkillImportJobRead>(
          `/api/enterprise/general-skill-import-jobs/${encodeURIComponent(job.id)}`,
        );
        if (disposed) return;
        setSecureImportJob(next);
        if (next.status === 'awaiting_approval') {
          setSecureImportSelectedIds(next.candidates.map((candidate) => candidate.candidate_id));
          setSecureImportDependencyDecisions(defaultDependencyDecisions(next));
        }
        if (['installed', 'cancelled', 'expired'].includes(next.status)) {
          window.localStorage.removeItem(secureImportStorageKey);
        }
      } catch {
        // 短暂网络错误不丢弃持久作业；下一轮继续按作业 ID 恢复。
      } finally {
        requestInFlight = false;
      }
    };
    const timer = window.setInterval(() => void poll(), 400);
    void poll();
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [secureImportJob?.id, secureImportJob?.status, secureImportOpen, secureImportStorageKey]);

  useEffect(() => {
    api
      .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${getRequestTenantId()}`)
      .then((items) => {
        setAgents(items);
        setIsOverallAgent(Boolean(items.find((item) => item.id === agentId)?.is_overall ?? true));
        setAgentScopeLoaded(true);
      })
      .catch(() => {
        setIsOverallAgent(true);
        setAgentScopeLoaded(true);
      });
  }, [agentId]);

  useEffect(() => {
    if (searchParams.get('add') !== 'plaza') return;
    if (!agentScopeLoaded) return;
    const resourceId = searchParams.get('resourceId') || undefined;
    if (isOverallAgent) {
      notify.warning('请先选择一个数字员工，再从广场复制技能');
    } else {
      void requestAgentImport('plaza', resourceId);
    }
    const next = new URLSearchParams(searchParams);
    next.delete('add');
    next.delete('resourceId');
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentScopeLoaded, isOverallAgent, searchParams, setSearchParams]);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const detail = (event as CustomEvent<{ agentId?: string }>).detail;
      setAgentId(detail?.agentId || window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
    };
    window.addEventListener('gongge-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('gongge-enterprise-agent-scope-change', onScopeChange);
  }, []);

  const filteredRows = useMemo(() => {
    const keyword = searchText.trim().toLowerCase();
    return rows.filter((row) => {
      const matchesStatus = statusFilter === 'all' || row.status === statusFilter;
      const haystack = [
        row.name,
        row.slug,
        row.description,
        row.homepage,
        resourceCreatorName(row),
      ].filter(Boolean).join(' ').toLowerCase();
      return matchesStatus && (!keyword || haystack.includes(keyword));
    });
  }, [rows, searchText, statusFilter]);

  const pagination = useClientPagination(filteredRows, GENERAL_SKILL_PAGE_SIZE, `${searchText}|${statusFilter}`);

  const stats = useMemo(() => ({
    total: rows.length,
    published: rows.filter((row) => row.status === 'published').length,
    draft: rows.filter((row) => row.status === 'draft').length,
    archived: rows.filter((row) => row.status === 'archived').length,
  }), [rows]);

  async function setSkillPublished(row: GeneralSkillRead, published: boolean) {
    try {
      if (!isOverallAgent && row.binding_id && row.binding_row_version && row.revision_policy && row.invocation_policy) {
        await api.patch(
          `/api/enterprise/general-skill-governance/bindings/${encodeURIComponent(row.binding_id)}`,
          {
            agent_id: agentId,
            status: published ? 'active' : 'inactive',
            revision_policy: row.revision_policy,
            pinned_revision_id: row.revision_policy === 'pinned' ? row.pinned_revision_id : null,
            invocation_policy: row.invocation_policy,
            expected_row_version: row.binding_row_version,
          },
        );
        await load();
        notify.success(published ? '已启用技能' : '已停用技能');
        return;
      }
      const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      const next = await api.post<GeneralSkillRead>(
        `/api/enterprise/general-skills/${row.slug}/${published ? 'publish' : 'archive'}?tenant_id=${getRequestTenantId()}${agentSuffix}`,
      );
      setRows((current) => current.map((item) => (item.id === next.id ? next : item)));
      notify.success(published ? '已启用技能' : '已停用技能');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : published ? '启用失败' : '停用失败');
    }
  }

  async function confirmDeleteSkill() {
    const row = deleteTarget;
    if (!row) return;
    const branchMode = !isOverallAgent;
    setDeleting(true);
    try {
      if (branchMode && row.binding_id && row.binding_row_version && row.revision_policy && row.invocation_policy) {
        await api.patch(
          `/api/enterprise/general-skill-governance/bindings/${encodeURIComponent(row.binding_id)}`,
          {
            agent_id: agentId,
            status: 'inactive',
            revision_policy: row.revision_policy,
            pinned_revision_id: row.revision_policy === 'pinned' ? row.pinned_revision_id : null,
            invocation_policy: row.invocation_policy,
            expected_row_version: row.binding_row_version,
          },
        );
        setRows((current) => current.filter((item) => item.id !== row.id));
        notify.success('已移除技能');
        setDeleteTarget(null);
        return;
      }
      const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      await api.delete(`/api/enterprise/general-skills/${row.slug}?tenant_id=${getRequestTenantId()}${agentSuffix}`);
      setRows((current) => current.filter((item) => item.id !== row.id));
      notify.success(branchMode ? '已移除技能' : '已删除技能');
      setDeleteTarget(null);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : branchMode ? '移除失败' : '删除失败');
    } finally {
      setDeleting(false);
    }
  }

  function requestClawHubImport() {
    clawhubAbortRef.current?.abort();
    clawhubAbortRef.current = null;
    setClawhubLoading(false);
    setClawhubSource('');
    setClawhubModalOpen(true);
  }

  function cancelClawHubImport() {
    clawhubAbortRef.current?.abort();
    clawhubAbortRef.current = null;
    setClawhubLoading(false);
    setClawhubModalOpen(false);
  }

  async function requestAgentImport(mode: GeneralSkillImportMode, selectedResourceId?: string) {
    try {
      const agents = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${getRequestTenantId()}`);
      const firstSource = mode === 'plaza'
        ? openGalleryAgentId(agents)
        : visibleEmployeeAgents(agents, currentUser, { activeOnly: true, excludeAgentId: agentId })[0]?.id || '';
      setAgentImportMode(mode);
      setAgentImportAgents(agents);
      setAgentImportSourceAgentId(firstSource);
      setAgentImportSelectedSkillIds([]);
      setAgentImportOpen(true);
      if (firstSource) {
        const sourceRows = await loadAgentImportSourceSkills(firstSource);
        if (selectedResourceId && sourceRows.some((item) => item.id === selectedResourceId)) {
          setAgentImportSelectedSkillIds([selectedResourceId]);
        }
      } else {
        setAgentImportSourceSkills([]);
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工列表失败');
    }
  }

  async function loadAgentImportSourceSkills(sourceAgentId: string): Promise<GeneralSkillRead[]> {
    setAgentImportSourceSkills([]);
    setAgentImportSelectedSkillIds([]);
    if (!sourceAgentId) return [];
    try {
      const sourceRows = await api.get<GeneralSkillRead[]>(
        `/api/enterprise/general-skills?tenant_id=${getRequestTenantId()}&agent_id=${encodeURIComponent(sourceAgentId)}`,
      );
      const existingIds = new Set(rows.map((item) => item.id));
      const publishedRows = sourceRows.filter((item) => item.status === 'published' && !existingIds.has(item.id));
      setAgentImportSourceSkills(publishedRows);
      return publishedRows;
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载来源技能失败');
      return [];
    }
  }

  async function submitAgentImportSkills() {
    if (!agentId) {
      notify.warning('请先选择一个数字员工');
      return;
    }
    if (!agentImportSourceAgentId) {
      notify.warning(agentImportMode === 'plaza' ? '请选择开放广场' : '请选择复制来源');
      return;
    }
    if (!agentImportSelectedSkillIds.length) {
      notify.warning('请选择要复制的技能');
      return;
    }
    setAgentImportLoading(true);
    try {
      await api.post(`/api/enterprise/agents/${encodeURIComponent(agentId)}/resources/import`, {
        tenant_id: getRequestTenantId(),
        source_agent_id: agentImportSourceAgentId,
        resource_type: 'general_skill',
        resource_ids: agentImportSelectedSkillIds,
      });
      notify.success(`已复制 ${agentImportSelectedSkillIds.length} 个技能`);
      setAgentImportOpen(false);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '复制技能失败');
    } finally {
      setAgentImportLoading(false);
    }
  }

  async function importClawHubSource() {
    if (!clawhubSource.trim()) {
      notify.warning('请输入开源平台地址、GitHub 仓库或 SKILL.md 链接');
      return;
    }
    const controller = new AbortController();
    clawhubAbortRef.current?.abort();
    clawhubAbortRef.current = controller;
    setClawhubLoading(true);
    try {
      const row = await api.postWithSignal<GeneralSkillRead>('/api/enterprise/general-skills/import-skillhub', {
        tenant_id: getRequestTenantId(),
        agent_id: !isOverallAgent && agentId ? agentId : undefined,
        source: clawhubSource.trim(),
        status: 'published',
      }, controller.signal);
      if (controller.signal.aborted) return;
      notify.success(`已新增 ${row.name}`);
      setRows((current) => [row, ...current.filter((item) => item.id !== row.id && item.slug !== row.slug)]);
      setClawhubModalOpen(false);
      navigate(`/enterprise/general-skills/${encodeURIComponent(row.slug)}/edit`);
    } catch (error) {
      if (isAbortError(error)) {
        notify.info('已取消导入');
        return;
      }
      notify.error(error instanceof Error ? error.message : '从开源平台导入失败');
    } finally {
      if (clawhubAbortRef.current === controller) {
        clawhubAbortRef.current = null;
        setClawhubLoading(false);
      }
    }
  }

  function requestSecurePackageImport(targetSkillId: string | null = null) {
    setSecureImportSourceKind('upload');
    setSecureImportFile(null);
    setSecureImportFolderFiles([]);
    setSecureImportSourceUrl('');
    setSecureImportRevision('');
    setSecureImportSubpath('skills');
    setSecureImportJob(null);
    setSecureImportSelectedIds([]);
    setSecureImportDependencyDecisions({});
    setSecureImportRetryParentId(null);
    setSecureImportCredentialId('');
    setSecureImportCredentialName('');
    setSecureImportCredentialToken('');
    setSecureImportTargetSkillId(targetSkillId);
    setSecureImportOpen(true);
    void loadSecureImportCredentials();
  }

  async function loadSecureImportCredentials() {
    try {
      const credentials = await api.get<GeneralSkillSourceCredentialRead[]>(
        '/api/enterprise/general-skill-import-jobs/credentials',
      );
      setSecureImportCredentials(credentials);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载私有来源凭据失败');
    }
  }

  async function createSecureImportCredential() {
    if (!secureImportCredentialName.trim() || !secureImportCredentialToken) {
      notify.warning('请输入凭据名称和 Token');
      return;
    }
    let allowedHost: string | undefined;
    if (secureImportSourceKind === 'https') {
      try {
        allowedHost = new URL(secureImportSourceUrl).hostname;
      } catch {
        notify.warning('请先输入有效的 HTTPS 来源地址');
        return;
      }
    }
    if (!['github', 'https'].includes(secureImportSourceKind)) return;
    setSecureImportCredentialLoading(true);
    try {
      const credential = await api.post<GeneralSkillSourceCredentialRead>(
        '/api/enterprise/general-skill-import-jobs/credentials',
        {
          tenant_id: getRequestTenantId(),
          display_name: secureImportCredentialName.trim(),
          source_kind: secureImportSourceKind,
          allowed_host: allowedHost,
          token: secureImportCredentialToken,
        },
      );
      setSecureImportCredentials((current) => [...current, credential]);
      setSecureImportCredentialId(credential.id);
      setSecureImportCredentialToken('');
      notify.success('私有来源凭据已加密保存');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存私有来源凭据失败');
    } finally {
      setSecureImportCredentialToken('');
      setSecureImportCredentialLoading(false);
    }
  }

  async function rotateSecureImportCredential() {
    const credential = secureImportCredentials.find((item) => item.id === secureImportCredentialId);
    if (!credential || !secureImportCredentialToken) {
      notify.warning('请选择凭据并输入新的 Token');
      return;
    }
    setSecureImportCredentialLoading(true);
    try {
      const rotated = await api.post<GeneralSkillSourceCredentialRead>(
        `/api/enterprise/general-skill-import-jobs/credentials/${encodeURIComponent(credential.id)}/rotate`,
        { token: secureImportCredentialToken, expected_row_version: credential.row_version },
      );
      setSecureImportCredentials((current) => current.map((item) => (
        item.id === rotated.id ? rotated : item
      )));
      notify.success('凭据已轮换，排队作业将使用最新修订');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '轮换凭据失败');
    } finally {
      setSecureImportCredentialToken('');
      setSecureImportCredentialLoading(false);
    }
  }

  async function revokeSecureImportCredential() {
    const credential = secureImportCredentials.find((item) => item.id === secureImportCredentialId);
    if (!credential) return;
    setSecureImportCredentialLoading(true);
    try {
      const revoked = await api.post<GeneralSkillSourceCredentialRead>(
        `/api/enterprise/general-skill-import-jobs/credentials/${encodeURIComponent(credential.id)}/revoke`,
        { expected_row_version: credential.row_version },
      );
      setSecureImportCredentials((current) => current.map((item) => (
        item.id === revoked.id ? revoked : item
      )));
      setSecureImportCredentialId('');
      notify.success('凭据已撤销');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '撤销凭据失败');
    } finally {
      setSecureImportCredentialLoading(false);
    }
  }

  async function previewSecurePackage() {
    if (!agentId || isOverallAgent) {
      notify.warning('请先选择要获得该能力的数字员工');
      return;
    }
    if (secureImportSourceKind === 'upload' && !secureImportFile) {
      notify.warning('请选择 SKILL.md 或 ZIP Skill 包');
      return;
    }
    if (secureImportSourceKind === 'folder' && !secureImportFolderFiles.length) {
      notify.warning('请选择一个包含 SKILL.md 的文件夹');
      return;
    }
    if (!['upload', 'folder'].includes(secureImportSourceKind) && !secureImportSourceUrl.trim()) {
      notify.warning('请输入远程来源地址或 SkillHub slug');
      return;
    }
    if (secureImportSourceKind === 'github' && !/^[a-f0-9]{40}$/i.test(secureImportRevision.trim())) {
      notify.warning('请输入完整的 40 位 Git commit SHA');
      return;
    }
    if (secureImportSourceKind === 'github' && !secureImportSubpath.trim()) {
      notify.warning('请输入仓库内 Skill 目录');
      return;
    }
    setSecureImportLoading(true);
    try {
      const sourcePayload = secureImportSourceKind === 'upload' && secureImportFile
        ? {
            source_kind: 'upload',
            filename: secureImportFile.name,
            content_base64: await fileToBase64(secureImportFile),
          }
        : secureImportSourceKind === 'folder'
          ? {
              source_kind: 'upload',
              filename: secureImportFolderFiles[0]?.webkitRelativePath.split('/')[0] || 'folder-upload',
              files: await Promise.all(secureImportFolderFiles.map(async (file) => ({
                path: file.webkitRelativePath || file.name,
                content_base64: await fileToBase64(file),
              }))),
            }
          : {
            source_kind: secureImportSourceKind,
            source_url: secureImportSourceUrl.trim(),
            revision: secureImportSourceKind === 'github' ? secureImportRevision.trim() : undefined,
            source_subpath: secureImportSourceKind === 'github' ? secureImportSubpath.trim() : undefined,
          };
      const job = await api.postWithHeaders<GeneralSkillImportJobRead>(
        '/api/enterprise/general-skill-import-jobs',
        {
          tenant_id: getRequestTenantId(),
          target_agent_id: agentId,
          target_skill_id: secureImportTargetSkillId || undefined,
          retry_parent_job_id: secureImportRetryParentId || undefined,
          credential_reference: secureImportCredentialId || undefined,
          ...sourcePayload,
        },
        { 'Idempotency-Key': `skill-upload-${crypto.randomUUID()}` },
      );
      setSecureImportJob(job);
      setSecureImportSelectedIds(job.candidates.map((candidate) => candidate.candidate_id));
      setSecureImportDependencyDecisions(defaultDependencyDecisions(job));
      if (job.status === 'awaiting_approval' || isSkillImportProcessing(job.status)) {
        window.localStorage.setItem(secureImportStorageKey, job.id);
      }
      if (job.status === 'failed') {
        notify.error(job.error_detail_redacted || 'Skill 包未通过安全检查');
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '生成安全预览失败');
    } finally {
      setSecureImportLoading(false);
    }
  }

  async function confirmSecurePackage() {
    const job = secureImportJob;
    if (!job?.preview_checksum || !secureImportSelectedIds.length) {
      notify.warning('请至少选择一个已通过检查的 Skill');
      return;
    }
    setSecureImportLoading(true);
    try {
      const installed = await api.post<GeneralSkillImportJobRead>(
        `/api/enterprise/general-skill-import-jobs/${encodeURIComponent(job.id)}/confirm`,
        {
          preview_checksum: job.preview_checksum,
          candidate_ids: secureImportSelectedIds,
          dependency_decisions: job.candidates
            .filter((candidate) => secureImportSelectedIds.includes(candidate.candidate_id))
            .flatMap((candidate) => candidate.dependency_candidates.map((dependency) => ({
              dependency_candidate_id: dependency.dependency_candidate_id,
              dependency_kind: secureImportDependencyDecisions[dependency.dependency_candidate_id] || 'ignored',
            }))),
          expected_row_version: job.row_version,
        },
      );
      setSecureImportJob(installed);
      notify.success(`已为当前数字员工安装 ${installed.installed_revision_ids.length} 个 Skill`);
      window.localStorage.removeItem(secureImportStorageKey);
      setSecureImportOpen(false);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '确认安装失败');
    } finally {
      setSecureImportLoading(false);
    }
  }

  async function cancelSecurePackage() {
    const job = secureImportJob;
    if (job && !['installed', 'failed', 'cancelled', 'expired'].includes(job.status)) {
      setSecureImportLoading(true);
      try {
        await api.post(
          `/api/enterprise/general-skill-import-jobs/${encodeURIComponent(job.id)}/cancel`,
          { expected_row_version: job.row_version },
        );
      } catch (error) {
        notify.error(error instanceof Error ? error.message : '取消导入失败');
        setSecureImportLoading(false);
        return;
      }
    }
    setSecureImportLoading(false);
    setSecureImportCredentialToken('');
    window.localStorage.removeItem(secureImportStorageKey);
    setSecureImportOpen(false);
  }

  function renderActions(row: GeneralSkillRead) {
    const published = row.status === 'published';
    if (isOverallAgent && !canManageCurrentScope) {
      return null;
    }
    return (
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label="技能操作"
          className="ml-auto grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
        >
          <IconMore className="size-3.5" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
          <DropdownMenuItem
            className={MENU_ITEM_CLASS}
            onSelect={() => navigate(`/enterprise/general-skills/${encodeURIComponent(row.slug)}/edit`)}
          >
            <IconEdit />
            {isOverallAgent ? '编辑' : '编辑本地版本'}
          </DropdownMenuItem>
          {published ? (
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void setSkillPublished(row, false)}>
              <Ban />
              停用
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void setSkillPublished(row, true)}>
              <CircleCheck />
              启用
            </DropdownMenuItem>
          )}
          {!isOverallAgent && row.binding_id && row.revision_policy ? (
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => setGovernanceTarget(row)}>
              <ShieldCheck />
              版本与调用策略
            </DropdownMenuItem>
          ) : null}
          <DropdownMenuSeparator className="my-[2px] bg-[#eef0f4]" />
          <DropdownMenuItem
            variant="destructive"
            className={MENU_ITEM_DANGER_CLASS}
            onSelect={() => setDeleteTarget(row)}
          >
            <IconTrash />
            {isOverallAgent ? '删除' : '移除'}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    );
  }

  const columns: DataTableColumn<GeneralSkillRead>[] = [
    {
      key: 'name',
      title: '名称',
      width: 200,
      className: 'text-[#18181a]',
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate font-medium leading-[18px] text-[#18181a]" title={row.name}>
            {row.name}
          </span>
          <span className="truncate text-[#858b9c]" title={row.slug}>
            {row.slug}
          </span>
        </div>
      ),
    },
    {
      key: 'description',
      title: '描述',
      className: 'whitespace-normal',
      render: (row) => <span className="line-clamp-2 wrap-break-word">{row.description || '暂无描述'}</span>,
    },
    {
      key: 'files',
      title: '文件',
      width: 90,
      render: (row) => `${row.skill_files?.length || 1} 个`,
    },
    {
      key: 'creator',
      title: '创建者',
      width: 120,
      render: (row) => (
        <span className="block truncate text-[#858b9c]" title={resourceCreatorName(row)}>
          {resourceCreatorName(row) || '-'}
        </span>
      ),
    },
    {
      key: 'status',
      title: '状态',
      width: 100,
      render: (row) => {
        const preset = STATUS_BADGE[row.status] || { tone: 'gray' as BadgeTone, text: row.status };
        return <StatusBadge tone={preset.tone}>{preset.text}</StatusBadge>;
      },
    },
    {
      key: 'updated',
      title: '更新时间',
      width: 170,
      render: (row) => formatDateTime(row.updated_at),
    },
    {
      key: 'actions',
      title: '操作',
      width: 70,
      align: 'right',
      render: (row) => renderActions(row),
    },
  ];

  const renderMobileCard = (row: GeneralSkillRead) => {
    const preset = STATUS_BADGE[row.status] || { tone: 'gray' as BadgeTone, text: row.status };
    return (
      <article className={MOBILE_CARD_CLASS} key={row.id}>
        <div className="flex min-w-0 items-start justify-between gap-[10px]">
          <div className="min-w-0">
            <strong className="block truncate text-[14px] font-semibold text-[#18181a]">{row.name}</strong>
            <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">{row.slug}</span>
            <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">创建者：{resourceCreatorName(row) || '-'}</span>
          </div>
          {renderActions(row)}
        </div>
        {row.description && (
          <p className="mt-[8px] line-clamp-2 text-[12px] leading-[1.55] text-[#858b9c]">{row.description}</p>
        )}
        <div className="mt-[10px] flex items-center justify-between gap-[10px] text-[12px] text-[#858b9c]">
          <StatusBadge tone={preset.tone}>{preset.text}</StatusBadge>
          <span>{row.skill_files?.length || 1} 个文件 · {formatDateTime(row.updated_at)}</span>
        </div>
      </article>
    );
  };

  const listEmptyText = isOverallAgent
    ? canManageCurrentScope ? '暂无技能，点击「新增」创建一个吧' : '暂无技能'
    : '当前员工暂无技能';

  return (
    <div className={embedded ? undefined : 'min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]'}>
      {!embedded && (
        <>
          <AppHeader onLogout={onLogout} userName={currentUser?.username} title={pageTitle} />
          <div className="mt-[20px] mb-[16px] flex items-center justify-end gap-[12px]">
            <UIButton
              variant="outline"
              onClick={() => void load()}
              disabled={loading}
              className="h-[34px] gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
            >
              <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
              刷新
            </UIButton>
            {canManageCurrentScope && (
              <DropdownMenu>
                <DropdownMenuTrigger className="flex h-[34px] items-center gap-[4px] rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-[20px] text-[12px] font-semibold text-white outline-none transition-colors hover:bg-[#244bc7]">
                  <IconAdd className="size-[14px]" />
                  新增
                  <IconChevronDown className="size-[12px]" />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
                  <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => navigate('/enterprise/general-skills/new')}>
                    <IconAdd />
                    新建技能
                  </DropdownMenuItem>
                  {!isOverallAgent && (
                    <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void requestAgentImport('plaza')}>
                      <Copy />
                      从广场复制
                    </DropdownMenuItem>
                  )}
                  {!secureImportAvailable && (
                    <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => requestClawHubImport()}>
                      <GithubOutlined />
                      从开源平台导入
                    </DropdownMenuItem>
                  )}
                  {!isOverallAgent && secureImportAvailable && (
                    <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => requestSecurePackageImport()}>
                      <ShieldCheck />
                      安全导入 Skill
                    </DropdownMenuItem>
                  )}
                  {!isOverallAgent && (
                    <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void requestAgentImport('employee')}>
                      <Users />
                      从数字员工复制
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </div>
        </>
      )}

      <div className="flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-[#FFF] p-[18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-stretch gap-[20px]" aria-label="技能统计">
          <StatCard label="技能总数" value={stats.total} />
          <StatCard label="已启用" value={stats.published} tone="green" />
          <StatCard label="草稿" value={stats.draft} />
          <StatCard label="已停用" value={stats.archived} />
        </div>

        <div className="flex flex-col gap-[18px]">
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <PlazaResourceIcon kind="general-skills" size="compact" />
            <span className="text-[14px] font-normal leading-none">{listLabel}</span>
          </div>

          <div className="flex flex-wrap items-center gap-[16px]">
            <label className="flex h-[34px] w-[300px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[var(--gg-cobalt)] max-[900px]:w-full">
              <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
              <input
                value={searchText}
                placeholder="搜索技能名称、Slug、描述或主页"
                onChange={(event) => setSearchText(event.target.value)}
                className="h-full min-w-0 flex-1 bg-transparent text-[12px] text-[#17191f] outline-none placeholder:text-[#c0c6d4]"
              />
              {searchText && (
                <button
                  type="button"
                  aria-label="清除搜索"
                  onClick={() => setSearchText('')}
                  className="grid size-[16px] shrink-0 place-items-center text-[#c0c6d4] hover:text-[#858b9c]"
                >
                  <IconClear className="size-[14px]" />
                </button>
              )}
            </label>
            <UISelect value={statusFilter} onValueChange={(value) => setStatusFilter(value as 'all' | GeneralSkillRead['status'])}>
              <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, 'w-[130px]')} aria-label="状态筛选">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部状态</SelectItem>
                <SelectItem value="published">已启用</SelectItem>
                <SelectItem value="draft">草稿</SelectItem>
                <SelectItem value="archived">已停用</SelectItem>
              </SelectContent>
            </UISelect>
          </div>

          <div className="grid gap-[10px] md:hidden">
            {filteredRows.length ? (
              pagination.pagedItems.map(renderMobileCard)
            ) : (
              <div className="py-[40px] text-center text-[13px] text-[#858b9c]">{listEmptyText}</div>
            )}
          </div>

          <div className="hidden md:block">
            <DataTable
              aria-label="技能列表"
              columns={columns}
              data={pagination.pagedItems}
              rowKey={(row) => row.id}
              loading={loading}
              emptyText={listEmptyText}
            />
          </div>

          {filteredRows.length > 0 && (
            <Paginator
              aria-label="技能分页"
              className="mt-0 mb-[6px]"
              page={pagination.page}
              pageCount={pagination.pageCount}
              onChange={pagination.setPage}
            />
          )}
        </div>
      </div>

      <ClawHubDialog
        open={clawhubModalOpen}
        loading={clawhubLoading}
        source={clawhubSource}
        onSourceChange={setClawhubSource}
        onClose={cancelClawHubImport}
        onSubmit={() => void importClawHubSource()}
      />

      <SecureSkillImportDialog
        open={secureImportOpen}
        loading={secureImportLoading}
        availableSourceKinds={secureImportSourceKinds}
        sourceKind={secureImportSourceKind}
        file={secureImportFile}
        folderFiles={secureImportFolderFiles}
        sourceUrl={secureImportSourceUrl}
        revision={secureImportRevision}
        sourceSubpath={secureImportSubpath}
        credentials={secureImportCredentials}
        credentialId={secureImportCredentialId}
        credentialName={secureImportCredentialName}
        credentialToken={secureImportCredentialToken}
        credentialLoading={secureImportCredentialLoading}
        job={secureImportJob}
        selectedIds={secureImportSelectedIds}
        dependencyDecisions={secureImportDependencyDecisions}
        onFileChange={(file) => {
          setSecureImportFile(file);
          setSecureImportJob(null);
          setSecureImportSelectedIds([]);
        }}
        onFolderFilesChange={(files) => {
          setSecureImportFolderFiles(files);
          setSecureImportJob(null);
          setSecureImportSelectedIds([]);
        }}
        onSourceKindChange={(kind) => {
          setSecureImportSourceKind(kind);
          setSecureImportCredentialId('');
          setSecureImportCredentialToken('');
          setSecureImportJob(null);
          setSecureImportSelectedIds([]);
        }}
        onSourceUrlChange={(value) => {
          setSecureImportSourceUrl(value);
          const selectedCredential = secureImportCredentials.find(
            (item) => item.id === secureImportCredentialId,
          );
          if (secureImportSourceKind !== 'https' || !selectedCredential) return;
          try {
            if (new URL(value).hostname !== selectedCredential.allowed_host) {
              setSecureImportCredentialId('');
            }
          } catch {
            setSecureImportCredentialId('');
          }
        }}
        onRevisionChange={setSecureImportRevision}
        onSourceSubpathChange={setSecureImportSubpath}
        onCredentialIdChange={setSecureImportCredentialId}
        onCredentialNameChange={setSecureImportCredentialName}
        onCredentialTokenChange={setSecureImportCredentialToken}
        onCredentialCreate={() => void createSecureImportCredential()}
        onCredentialRotate={() => void rotateSecureImportCredential()}
        onCredentialRevoke={() => void revokeSecureImportCredential()}
        onSelectedIdsChange={setSecureImportSelectedIds}
        onDependencyDecisionChange={(candidateId, decision) => setSecureImportDependencyDecisions(
          (current) => ({ ...current, [candidateId]: decision }),
        )}
        onPreview={() => void previewSecurePackage()}
        onConfirm={() => void confirmSecurePackage()}
        onReset={() => {
          setSecureImportRetryParentId(secureImportJob?.id || null);
          setSecureImportJob(null);
          setSecureImportFile(null);
          setSecureImportFolderFiles([]);
          setSecureImportSelectedIds([]);
          setSecureImportDependencyDecisions({});
        }}
        onClose={() => void cancelSecurePackage()}
      />

      <ResourceImportDialog
        open={agentImportOpen}
        loading={agentImportLoading}
        icon={<PlazaResourceIcon kind="general-skills" size="compact" />}
        title={agentImportMode === 'plaza' ? '从广场复制技能' : '从数字员工复制技能'}
        sourcePlaceholder={agentImportMode === 'plaza' ? '选择开放广场' : '选择复制来源'}
        sources={agentImportMode === 'plaza'
          ? openGalleryImportSourceOptions(agentImportAgents, '开放广场')
          : visibleEmployeeAgents(agentImportAgents, currentUser, { activeOnly: true, excludeAgentId: agentId })
            .map((item) => ({ value: item.id, label: item.name }))}
        sourceId={agentImportSourceAgentId}
        itemsLabel="选择技能"
        items={agentImportSourceSkills.map((item) => ({
          id: item.id,
          label: (
            <>
              {item.name}
              <span className="text-[#858b9c]"> · {item.slug}</span>
            </>
          ),
        }))}
        selectedIds={agentImportSelectedSkillIds}
        emptyText="没有可复制的技能"
        note={
          agentImportMode === 'plaza'
            ? '从开放广场复制可用技能；不可复制内容不会出现在列表。'
            : '从数字员工复制可用技能；不可见内容不会出现在列表。'
        }
        onSourceChange={(value) => {
          setAgentImportSourceAgentId(value);
          void loadAgentImportSourceSkills(value);
        }}
        onSelectedChange={setAgentImportSelectedSkillIds}
        onClose={() => setAgentImportOpen(false)}
        onSubmit={() => void submitAgentImportSkills()}
      />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        loading={deleting}
        title={deleteTarget ? `${isOverallAgent ? '删除' : '移除'}技能「${deleteTarget.name}」？` : ''}
        description={
          isOverallAgent
            ? '删除后该技能不会再出现在技能广场中，此操作不可撤销。'
            : '这只会在当前数字员工中隐藏该技能；开放广场和其他数字员工仍然保留。'
        }
        confirmText={isOverallAgent ? '删除' : '移除'}
        onConfirm={() => void confirmDeleteSkill()}
      />

      <SkillGovernanceDialog
        row={governanceTarget}
        agentId={agentId}
        onClose={() => setGovernanceTarget(null)}
        onChanged={async () => {
          await load();
        }}
        onUpgrade={(skill) => {
          setGovernanceTarget(null);
          requestSecurePackageImport(skill.id);
        }}
      />
    </div>
  );
}

function ClawHubDialog({
  open,
  loading,
  source,
  onSourceChange,
  onClose,
  onSubmit,
}: {
  open: boolean;
  loading: boolean;
  source: string;
  onSourceChange: (value: string) => void;
  onClose: () => void;
  onSubmit: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[560px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <PlazaResourceIcon kind="general-skills" size="compact" />
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            从开源平台导入技能
          </DialogTitle>
        </div>

        <div className="flex flex-col gap-[12px] px-[12px]">
          <p className="text-[12px] leading-[1.6] text-[#858b9c]">
            支持开源平台地址、GitHub repo/tree/raw SKILL.md 或 owner/repo 形式。本地 zip 或 Markdown 文件请在编辑页使用「导入 &gt; 选择文件」。
          </p>
          <input
            value={source}
            onChange={(event) => onSourceChange(event.target.value)}
            placeholder="例如 alchaincyf/nuwa-skill 或 https://github.com/owner/repo/tree/main/skill"
            className="h-[34px] w-full rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#17191f] outline-none transition-colors placeholder:text-[#c0c6d4] focus:border-[var(--gg-cobalt)]"
          />
        </div>

        <div className="flex items-center justify-end gap-[8px] px-[12px]">
          <UIButton
            variant="outline"
            disabled={loading}
            onClick={onClose}
            className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
          >
            取消
          </UIButton>
          <UIButton
            disabled={loading}
            onClick={onSubmit}
            className="h-[36px] w-[80px] rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-[12px] text-[14px] font-semibold text-white hover:bg-[#244bc7]"
          >
            新增
          </UIButton>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function SecureSkillImportDialog({
  open,
  loading,
  availableSourceKinds = ['upload', 'github', 'skillhub', 'https'],
  sourceKind,
  file,
  folderFiles,
  sourceUrl,
  revision,
  sourceSubpath,
  credentials = [],
  credentialId = '',
  credentialName = '',
  credentialToken = '',
  credentialLoading = false,
  job,
  selectedIds,
  dependencyDecisions,
  onFileChange,
  onFolderFilesChange,
  onSourceKindChange,
  onSourceUrlChange,
  onRevisionChange,
  onSourceSubpathChange,
  onCredentialIdChange,
  onCredentialNameChange,
  onCredentialTokenChange,
  onCredentialCreate,
  onCredentialRotate,
  onCredentialRevoke,
  onSelectedIdsChange,
  onDependencyDecisionChange,
  onPreview,
  onConfirm,
  onReset,
  onClose,
}: {
  open: boolean;
  loading: boolean;
  availableSourceKinds?: SecureSkillImportSourceKind[];
  sourceKind: SecureSkillImportSourceKind;
  file: File | null;
  folderFiles: File[];
  sourceUrl: string;
  revision: string;
  sourceSubpath: string;
  credentials?: GeneralSkillSourceCredentialRead[];
  credentialId?: string;
  credentialName?: string;
  credentialToken?: string;
  credentialLoading?: boolean;
  job: GeneralSkillImportJobRead | null;
  selectedIds: string[];
  dependencyDecisions: Record<string, SkillDependencyDecision>;
  onFileChange: (file: File | null) => void;
  onFolderFilesChange: (files: File[]) => void;
  onSourceKindChange: (kind: SecureSkillImportSourceKind) => void;
  onSourceUrlChange: (value: string) => void;
  onRevisionChange: (value: string) => void;
  onSourceSubpathChange: (value: string) => void;
  onCredentialIdChange?: (value: string) => void;
  onCredentialNameChange?: (value: string) => void;
  onCredentialTokenChange?: (value: string) => void;
  onCredentialCreate?: () => void;
  onCredentialRotate?: () => void;
  onCredentialRevoke?: () => void;
  onSelectedIdsChange: (ids: string[]) => void;
  onDependencyDecisionChange: (candidateId: string, decision: SkillDependencyDecision) => void;
  onPreview: () => void;
  onConfirm: () => void;
  onReset: () => void;
  onClose: () => void;
}) {
  const hasPreview = job?.status === 'awaiting_approval';
  const isProcessing = Boolean(job && isSkillImportProcessing(job.status));
  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);

  return (
    <Dialog open={open} onOpenChange={(next) => !next && !loading && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[88vh] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[18px] border-[#dfe5f2] p-0 sm:max-w-[760px]"
      >
        <header className="border-b border-[#e8ebf2] bg-[#f8faff] px-[24px] pt-[22px] pb-[18px]">
          <div className="flex items-center gap-3">
            <span className="grid size-10 place-items-center rounded-xl bg-[#eaf0ff] text-[var(--gg-cobalt)]">
              <ShieldCheck className="size-5" />
            </span>
            <div>
              <DialogTitle className="text-[17px] font-semibold text-[#18181a]">
                安全导入 Skill 包
              </DialogTitle>
              <p className="mt-1 text-[12px] leading-[1.5] text-[#757f9c]">
                先检查完整文件树和申请能力，确认后才会固定版本并绑定当前数字员工。
              </p>
            </div>
          </div>
          <ol aria-label="导入进度" className="mt-5 grid grid-cols-3 gap-2">
            {[
              ['1', '包校验'],
              ['2', '候选审核'],
              ['3', '固定并绑定'],
            ].map(([step, label], index) => {
              const active = index === 0 || Boolean(job) && index === 1 || job?.status === 'installed';
              return (
                <li
                  key={step}
                  className={cn(
                    'flex items-center gap-2 rounded-lg border px-3 py-2 text-[12px]',
                    active
                      ? 'border-[#cbd8ff] bg-white text-[#3157e8]'
                      : 'border-transparent bg-[#f1f3f8] text-[#9aa1b4]',
                  )}
                >
                  <span className="font-mono font-semibold">{step}</span>
                  <span>{label}</span>
                </li>
              );
            })}
          </ol>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-[24px] py-[20px]">
          {!job ? (
            <div className="grid gap-4">
              <div role="tablist" aria-label="Skill 来源" className="grid grid-cols-2 rounded-[11px] bg-[#f1f3f8] p-1 sm:grid-cols-5">
                {([
                  ['upload', '上传文件'],
                  ['folder', '选择文件夹'],
                  ['github', 'GitHub 固定版本'],
                  ['skillhub', 'SkillHub'],
                  ['https', 'HTTPS ZIP'],
                ] as const).filter(([kind]) => (
                  kind === 'folder' ? availableSourceKinds.includes('upload') : availableSourceKinds.includes(kind)
                )).map(([kind, label]) => (
                  <button
                    key={kind}
                    type="button"
                    role="tab"
                    aria-selected={sourceKind === kind}
                    disabled={loading}
                    onClick={() => onSourceKindChange(kind)}
                    className={cn(
                      'h-9 rounded-[8px] text-[12px] font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--gg-cobalt)]',
                      sourceKind === kind
                        ? 'bg-white text-[#3157e8] shadow-sm'
                        : 'text-[#757f9c] hover:text-[#343a4a]',
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>

              {sourceKind === 'upload' ? (
                <label className="flex min-h-[190px] cursor-pointer flex-col items-center justify-center rounded-[14px] border border-dashed border-[#bfc9dc] bg-[#fbfcff] px-6 text-center transition-colors hover:border-[var(--gg-cobalt)] hover:bg-[#f7f9ff] focus-within:ring-2 focus-within:ring-[#b9c8ff]">
                  <FileArchive className="mb-3 size-8 text-[#5574dc]" />
                  <strong className="text-[14px] font-semibold text-[#252936]">
                    {file ? file.name : '选择 SKILL.md 或 ZIP Skill 包'}
                  </strong>
                  <span className="mt-2 text-[12px] leading-[1.6] text-[#858b9c]">
                    不会直接运行包内内容；路径、编码、压缩比、文件预算和所有 SKILL.md 会先接受完整检查。
                  </span>
                  {file ? (
                    <span className="mt-3 rounded-full bg-[#edf2ff] px-3 py-1 font-mono text-[11px] text-[#3157e8]">
                      {formatBytes(file.size)}
                    </span>
                  ) : null}
                  <input
                    className="sr-only"
                    type="file"
                    accept=".zip,.md,application/zip,text/markdown"
                    disabled={loading}
                    onChange={(event) => onFileChange(event.target.files?.[0] || null)}
                  />
                </label>
              ) : sourceKind === 'folder' ? (
                <label className="flex min-h-[190px] cursor-pointer flex-col items-center justify-center rounded-[14px] border border-dashed border-[#bfc9dc] bg-[#fbfcff] px-6 text-center transition-colors hover:border-[var(--gg-cobalt)] hover:bg-[#f7f9ff] focus-within:ring-2 focus-within:ring-[#b9c8ff]">
                  <IconFolder className="mb-3 size-8 text-[#5574dc]" />
                  <strong className="text-[14px] font-semibold text-[#252936]">
                    {folderFiles.length ? `已选择 ${folderFiles.length} 个文件` : '选择完整 Skill 文件夹'}
                  </strong>
                  <span className="mt-2 text-[12px] leading-[1.6] text-[#858b9c]">
                    相对路径和全部文件会送入与 ZIP 相同的 fail-closed 检查；不会读取所选目录之外的内容。
                  </span>
                  <input
                    className="sr-only"
                    type="file"
                    multiple
                    {...FOLDER_INPUT_PROPS}
                    disabled={loading}
                    onChange={(event) => onFolderFilesChange(Array.from(event.target.files || []))}
                  />
                </label>
              ) : (
                <section className="grid gap-4 rounded-[14px] border border-[#dfe5f2] bg-[#fbfcff] p-5">
                  <label className="grid gap-2 text-[12px] font-medium text-[#303747]">
                    {sourceKind === 'github'
                      ? 'GitHub 仓库地址'
                      : sourceKind === 'skillhub'
                        ? 'SkillHub slug 或页面地址'
                        : '公开 HTTPS ZIP 地址'}
                    <Input
                      value={sourceUrl}
                      onChange={(event) => onSourceUrlChange(event.target.value)}
                      placeholder={sourceKind === 'github'
                        ? 'https://github.com/mattpocock/skills'
                        : sourceKind === 'skillhub'
                          ? '例如 customer-support 或 https://skillhub.ai/owner/customer-support'
                          : 'https://example.com/skills.zip'}
                      className="h-10 rounded-[9px] border-[#cfd7e6] bg-white font-mono text-[12px]"
                    />
                  </label>
                  {sourceKind === 'github' ? (
                    <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_180px]">
                      <label className="grid gap-2 text-[12px] font-medium text-[#303747]">
                        完整 commit SHA
                        <Input
                          value={revision}
                          onChange={(event) => onRevisionChange(event.target.value)}
                          placeholder="40 位十六进制 commit，不接受 main 或 tag"
                          className="h-10 rounded-[9px] border-[#cfd7e6] bg-white font-mono text-[12px]"
                        />
                      </label>
                      <label className="grid gap-2 text-[12px] font-medium text-[#303747]">
                        仓库内 Skill 目录
                        <Input
                          value={sourceSubpath}
                          onChange={(event) => onSourceSubpathChange(event.target.value)}
                          placeholder="skills"
                          className="h-10 rounded-[9px] border-[#cfd7e6] bg-white font-mono text-[12px]"
                        />
                      </label>
                    </div>
                  ) : null}
                  {['github', 'https'].includes(sourceKind) ? (
                    <section className="grid gap-3 rounded-[11px] border border-[#dfe5f2] bg-white p-4">
                      <div>
                        <strong className="text-[12px] font-semibold text-[#303747]">
                          私有来源凭据（可选）
                        </strong>
                        <p className="mt-1 text-[11px] leading-[1.6] text-[#858b9c]">
                          Token 加密保存且只属于你；导入作业仅保存不透明引用，跨主机重定向不会携带授权头。
                        </p>
                      </div>
                      <label className="grid gap-1.5 text-[11px] font-medium text-[#4e5668]">
                        本次导入使用
                        <select
                          aria-label="本次导入使用的私有来源凭据"
                          value={credentialId}
                          disabled={credentialLoading}
                          onChange={(event) => onCredentialIdChange?.(event.target.value)}
                          className="h-9 rounded-[8px] border border-[#cfd7e6] bg-white px-3 text-[12px] text-[#303747] outline-none focus:border-[var(--gg-cobalt)]"
                        >
                          <option value="">公开来源（不发送 Token）</option>
                          {credentials
                            .filter((credential) => (
                              credential.status === 'active' && credential.source_kind === sourceKind
                            ))
                            .map((credential) => (
                              <option key={credential.id} value={credential.id}>
                                {credential.display_name} · {credential.allowed_host} · v{credential.secret_revision}
                              </option>
                            ))}
                        </select>
                      </label>
                      <div className="grid gap-2 sm:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
                        <Input
                          aria-label="私有来源凭据名称"
                          value={credentialName}
                          disabled={credentialLoading}
                          onChange={(event) => onCredentialNameChange?.(event.target.value)}
                          placeholder="例如：我的 GitHub 只读 Token"
                          className="h-9 rounded-[8px] border-[#cfd7e6] text-[12px]"
                        />
                        <Input
                          aria-label="私有来源 Token"
                          type="password"
                          autoComplete="new-password"
                          value={credentialToken}
                          disabled={credentialLoading}
                          onChange={(event) => onCredentialTokenChange?.(event.target.value)}
                          placeholder={credentialId ? '输入新 Token 可轮换当前凭据' : 'Token 不会回显'}
                          className="h-9 rounded-[8px] border-[#cfd7e6] font-mono text-[12px]"
                        />
                      </div>
                      <div className="flex flex-wrap justify-end gap-2">
                        {credentialId ? (
                          <>
                            <UIButton
                              type="button"
                              variant="outline"
                              disabled={credentialLoading || !credentialToken}
                              onClick={onCredentialRotate}
                              className="h-8 text-[11px]"
                            >
                              轮换 Token
                            </UIButton>
                            <UIButton
                              type="button"
                              variant="outline"
                              disabled={credentialLoading}
                              onClick={onCredentialRevoke}
                              className="h-8 border-[#efcaca] text-[11px] text-[#a62626]"
                            >
                              撤销凭据
                            </UIButton>
                          </>
                        ) : (
                          <UIButton
                            type="button"
                            variant="outline"
                            disabled={credentialLoading || !credentialName.trim() || !credentialToken}
                            onClick={onCredentialCreate}
                            className="h-8 text-[11px]"
                          >
                            加密保存并用于本次导入
                          </UIButton>
                        )}
                      </div>
                    </section>
                  ) : null}
                  <p className="text-[11px] leading-[1.6] text-[#858b9c]">
                    每次重定向都会重新检查 HTTPS 主机与 DNS；私网、loopback、metadata 地址和漂移版本会在下载前拒绝。
                  </p>
                </section>
              )}
            </div>
          ) : null}

          {job?.status === 'failed' ? (
            <section role="alert" className="rounded-[14px] border border-[#f2c7c7] bg-[#fff8f8] p-4">
              <strong className="text-[13px] font-semibold text-[#a62626]">Skill 包未通过安全检查</strong>
              <p className="mt-2 text-[12px] leading-[1.6] text-[#7c4a4a]">
                {job.error_detail_redacted || job.error_code || '请修正文件后重新选择。'}
              </p>
            </section>
          ) : null}

          {isProcessing ? (
            <section
              role="status"
              aria-live="polite"
              className="flex min-h-[190px] flex-col items-center justify-center rounded-[14px] border border-[#d9e2f5] bg-[#f8faff] px-6 text-center"
            >
              <IconRefresh className="size-8 animate-spin text-[#5574dc]" />
              <strong className="mt-4 text-[14px] font-semibold text-[#252936]">
                后台正在安全检查 Skill 包
              </strong>
              <span className="mt-2 max-w-[520px] text-[12px] leading-[1.7] text-[#757f9c]">
                作业已持久保存，可关闭页面后稍后恢复。系统正在完成来源抓取、完整文件树校验、风险分析和候选预览。
              </span>
              <code className="mt-3 rounded-full bg-white px-3 py-1 font-mono text-[10px] text-[#6f7789]">
                {job?.status}
              </code>
            </section>
          ) : null}

          {hasPreview ? (
            <div className="grid gap-4">
              <section className="grid gap-2 rounded-[12px] border border-[#e2e7f2] bg-[#fafbfe] p-4 text-[12px] text-[#60687b] sm:grid-cols-2">
                <div>
                  <span className="block text-[11px] text-[#969daf]">规范包 checksum</span>
                  <code className="mt-1 block truncate font-mono text-[#303747]" title={job.normalized_checksum}>
                    {job.normalized_checksum}
                  </code>
                </div>
                <div>
                  <span className="block text-[11px] text-[#969daf]">暂存占用</span>
                  <span className="mt-1 block font-medium text-[#303747]">{formatBytes(job.quota_bytes)}</span>
                </div>
              </section>

              <fieldset className="grid gap-3">
                <legend className="mb-1 text-[13px] font-semibold text-[#252936]">
                  选择要固定到当前数字员工的 Skill
                </legend>
                {job.candidates.map((candidate) => {
                  const checked = selectedSet.has(candidate.candidate_id);
                  return (
                    <label
                      key={candidate.candidate_id}
                      className={cn(
                        'grid cursor-pointer grid-cols-[auto_minmax(0,1fr)] gap-3 rounded-[14px] border p-4 transition-colors',
                        checked
                          ? 'border-[#9db2ff] bg-[#f7f9ff]'
                          : 'border-[#e2e7f2] bg-white hover:border-[#c7d1e5]',
                      )}
                    >
                      <input
                        type="checkbox"
                        className="mt-1 size-4 accent-[var(--gg-cobalt)]"
                        checked={checked}
                        onChange={(event) => {
                          if (event.target.checked) {
                            onSelectedIdsChange([...selectedIds, candidate.candidate_id]);
                          } else {
                            onSelectedIdsChange(selectedIds.filter((id) => id !== candidate.candidate_id));
                          }
                        }}
                      />
                      <span className="min-w-0">
                        <span className="flex flex-wrap items-center gap-2">
                          <strong className="text-[14px] font-semibold text-[#252936]">{candidate.name}</strong>
                          <span className="rounded-full bg-[#edf2ff] px-2 py-0.5 text-[10px] text-[#3157e8]">
                            {candidate.resources.length} 个文件
                          </span>
                        </span>
                        <span className="mt-1 block text-[12px] leading-[1.6] text-[#6f7789]">
                          {candidate.description}
                        </span>
                        <span className="mt-3 block text-[11px] text-[#969daf]">申请工具（不代表已授权）</span>
                        <span className="mt-1 flex flex-wrap gap-1.5">
                          {candidate.allowed_tools.length ? candidate.allowed_tools.map((tool) => (
                            <code key={tool} className="rounded bg-[#f0f2f6] px-2 py-1 text-[10px] text-[#4e5668]">
                              {tool}
                            </code>
                          )) : <span className="text-[11px] text-[#858b9c]">未声明额外工具范围</span>}
                        </span>
                        <span className="mt-3 flex flex-wrap items-center gap-2 text-[11px]">
                          <span className={cn(
                            'rounded-full px-2 py-1 font-medium',
                            candidate.invocation_policy === 'user_only'
                              ? 'bg-[#fff4df] text-[#946200]'
                              : 'bg-[#edf7ef] text-[#23733b]',
                          )}>
                            {candidate.invocation_policy === 'user_only' ? '仅显式调用' : '允许模型选择'}
                          </span>
                          {candidate.argument_hint ? (
                            <span className="text-[#6f7789]">调用提示：{candidate.argument_hint}</span>
                          ) : null}
                        </span>
                        {Object.keys(candidate.instruction_contracts).length ? (
                          <span className="mt-3 block rounded-lg border border-[#e4e8f1] bg-[#f8f9fc] p-2.5 text-[11px] text-[#626b7d]">
                            <strong className="font-semibold text-[#303747]">已声明运行契约：</strong>{' '}
                            {Object.entries(candidate.instruction_contracts)
                              .map(([key, value]) => `${key}=${value}`)
                              .join(' · ')}
                          </span>
                        ) : null}
                        <span className="mt-3 block text-[11px] text-[#969daf]">许可证与静态风险</span>
                        <span className="mt-1 flex flex-wrap gap-1.5">
                          <span className={cn(
                            'rounded px-2 py-1 text-[10px]',
                            candidate.license_hint
                              ? 'bg-[#edf7ef] text-[#23733b]'
                              : 'bg-[#fff4df] text-[#946200]',
                          )}>
                            {candidate.license_hint ? `许可证声明：${candidate.license_hint}` : '未声明许可证'}
                          </span>
                          {candidate.risk_findings
                            .filter((finding) => finding !== 'license_not_declared')
                            .map((finding) => (
                              <span key={finding} className="rounded bg-[#fff4df] px-2 py-1 text-[10px] text-[#946200]">
                                {skillRiskFindingLabel(finding)}
                              </span>
                            ))}
                        </span>
                        {candidate.dependency_candidates.length ? (
                          <span className="mt-3 block rounded-lg border border-[#e4e8f1] bg-white p-2.5 text-[11px] text-[#626b7d]">
                            <strong className="font-semibold text-[#303747]">待确认的同包 Skill 引用：</strong>{' '}
                            <span className="mt-2 grid gap-2">
                              {candidate.dependency_candidates.map((dependency) => (
                                <label
                                  key={dependency.dependency_candidate_id}
                                  className="grid gap-1.5 rounded-md bg-[#f8f9fc] p-2 sm:grid-cols-[minmax(0,1fr)_150px] sm:items-center"
                                >
                                  <span>
                                    <code className="font-mono text-[#3157e8]">/{dependency.referenced_name}</code>
                                    <span className="ml-1 text-[#969daf]">引用 {dependency.reference_count} 次</span>
                                  </span>
                                  <select
                                    aria-label={`依赖 /${dependency.referenced_name} 的处理方式`}
                                    value={dependencyDecisions[dependency.dependency_candidate_id] || 'ignored'}
                                    onChange={(event) => onDependencyDecisionChange(
                                      dependency.dependency_candidate_id,
                                      event.target.value as SkillDependencyDecision,
                                    )}
                                    className="h-8 rounded-md border border-[#cfd7e6] bg-white px-2 text-[11px] text-[#303747] outline-none focus:border-[var(--gg-cobalt)]"
                                  >
                                    <option value="ignored">仅正文引用</option>
                                    <option value="required">建立必需依赖</option>
                                    <option value="optional">建立可选依赖</option>
                                  </select>
                                </label>
                              ))}
                            </span>
                            <span className="mt-1 block text-[#969daf]">必须逐边确认；正文引用本身不会自动获得依赖或工具权限。</span>
                          </span>
                        ) : null}
                        {candidate.platform_commands.length ? (
                          <span className="mt-2 block text-[11px] text-[#858b9c]">
                            平台命令引用：{candidate.platform_commands.map((command) => `/${command}`).join('、')}
                          </span>
                        ) : null}
                        <code className="mt-3 block truncate font-mono text-[10px] text-[#9aa1b4]" title={candidate.content_checksum}>
                          内容：{candidate.content_checksum}
                        </code>
                      </span>
                    </label>
                  );
                })}
              </fieldset>
            </div>
          ) : null}
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-[#e8ebf2] bg-white px-[24px] py-[16px]">
          <span className="text-[11px] text-[#858b9c]">
            {hasPreview
              ? '默认固定本次修订；后续升级需再次审核。'
              : isProcessing
                ? '关闭不会丢失作业；再次进入当前数字员工时会继续恢复进度。'
                : 'SKILL.md、ZIP 与文件夹共用完整检查；任一文件失败则整包拒绝。'}
          </span>
          <div className="flex items-center gap-2">
            <UIButton variant="outline" disabled={loading} onClick={onClose} className={RETURN_BUTTON_CLASS}>
              取消
            </UIButton>
            {!job ? (
              <UIButton
                disabled={loading || (
                  sourceKind === 'upload'
                    ? !file
                    : sourceKind === 'folder'
                      ? folderFiles.length === 0
                      : !sourceUrl.trim()
                )}
                onClick={onPreview}
                className={PRIMARY_BUTTON_CLASS}
              >
                生成安全预览
              </UIButton>
            ) : null}
            {hasPreview ? (
              <UIButton
                disabled={loading || selectedIds.length === 0}
                onClick={onConfirm}
                className={PRIMARY_BUTTON_CLASS}
              >
                固定版本并绑定
              </UIButton>
            ) : null}
            {job?.status === 'failed' ? (
              <UIButton
                disabled={loading}
                onClick={onReset}
                className={PRIMARY_BUTTON_CLASS}
              >
                修正来源并重试
              </UIButton>
            ) : null}
          </div>
        </footer>
      </DialogContent>
    </Dialog>
  );
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

function isSkillImportProcessing(status: string): boolean {
  return GENERAL_SKILL_IMPORT_PROCESSING_STATUSES.has(status);
}

function skillRiskFindingLabel(finding: string): string {
  const labels: Record<string, string> = {
    requests_tools: '申请工具能力',
    user_only_invocation: '仅允许用户显式调用',
    dependency_review_required: '存在待审核依赖',
    contains_executable_content: '包含脚本资源（默认不执行）',
  };
  return labels[finding] || finding;
}

function defaultDependencyDecisions(
  job: GeneralSkillImportJobRead,
): Record<string, SkillDependencyDecision> {
  return Object.fromEntries(
    job.candidates.flatMap((candidate) => candidate.dependency_candidates.map((dependency) => [
      dependency.dependency_candidate_id,
      'ignored' as const,
    ])),
  );
}

function traceDetail(item: Record<string, unknown>): string {
  return [
    item.rationale,
    item.expected_output,
    item.phase === 'code_finished' ? item.stdout_preview : undefined,
    item.phase === 'code_finished' || item.phase === 'code_timeout' ? item.stderr_preview : undefined,
    item.run_id,
  ]
    .filter((value) => typeof value === 'string' && value.trim())
    .map(String)
    .join('\n');
}

function traceItemCode(item: Record<string, unknown>): string {
  return typeof item.code === 'string' && item.code.trim() ? item.code : '';
}

function resultSucceeded(result: Partial<GeneralSkillRunResponse> | null): boolean {
  if (!result) return false;
  const success = result.structured_result?.success;
  return success !== false && !result.stderr;
}

function isAbortError(error: unknown): boolean {
  if (error instanceof DOMException && error.name === 'AbortError') return true;
  return error instanceof Error && error.name === 'AbortError';
}

function languageFromFilePath(path?: string): string {
  const extension = (path || '').split('.').pop()?.toLowerCase();
  if (extension === 'py') return 'python';
  if (extension === 'json') return 'json';
  if (extension === 'md' || extension === 'markdown') return 'markdown';
  return 'text';
}

function normalizeSkillFilePath(path: string): string {
  return path.replace(/\\/g, '/').replace(/^\/+/, '').replace(/\/+/g, '/').trim();
}

function packagePathFromRaw(value: string): string {
  const normalized = value.replace(/\\/g, '/').replace(/^\/+/, '');
  const parts = normalized.split('/').filter(Boolean);
  return parts.length > 1 ? parts.slice(1).join('/') : normalized;
}

function packagePath(file: File): string {
  return packagePathFromRaw((file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name);
}

function readEntryFile(entry: SkillFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

function readDirectoryEntries(entry: SkillDirectoryEntry): Promise<SkillFileSystemEntry[]> {
  const reader = entry.createReader();
  const output: SkillFileSystemEntry[] = [];

  return new Promise((resolve, reject) => {
    const readNext = () => {
      reader.readEntries((entries) => {
        if (!entries.length) {
          resolve(output);
          return;
        }
        output.push(...entries);
        readNext();
      }, reject);
    };
    readNext();
  });
}

async function collectDroppedEntryFiles(entry: SkillFileSystemEntry): Promise<DroppedSkillFile[]> {
  if (entry.isFile) {
    const file = await readEntryFile(entry as SkillFileEntry);
    return [{ file, path: packagePathFromRaw(entry.fullPath || file.name) }];
  }
  if (!entry.isDirectory) return [];
  const entries = await readDirectoryEntries(entry as SkillDirectoryEntry);
  const nested = await Promise.all(entries.map(collectDroppedEntryFiles));
  return nested.flat();
}

function dataTransferEntry(item: DataTransferItem): SkillFileSystemEntry | null {
  const getter = (item as unknown as { webkitGetAsEntry?: () => unknown }).webkitGetAsEntry;
  const entry = getter?.call(item);
  if (!entry || typeof entry !== 'object') return null;
  return entry as SkillFileSystemEntry;
}

async function droppedSkillFiles(dataTransfer: DataTransfer): Promise<DroppedSkillFile[]> {
  const entries = Array.from(dataTransfer.items || [])
    .map(dataTransferEntry)
    .filter((entry): entry is SkillFileSystemEntry => Boolean(entry));
  if (entries.length) {
    const nested = await Promise.all(entries.map(collectDroppedEntryFiles));
    return nested.flat();
  }
  return Array.from(dataTransfer.files || []).map((file) => ({ file, path: packagePath(file) }));
}

function parseMetadata(markdownText: string): Record<string, string> {
  const lines = markdownText.split(/\r?\n/);
  if (lines[0]?.trim() !== '---') return {};
  const result: Record<string, string> = {};
  for (let index = 1; index < lines.length; index += 1) {
    const line = lines[index].trim();
    if (line === '---') break;
    const colon = line.indexOf(':');
    if (colon < 0) continue;
    const key = line.slice(0, colon).trim();
    const value = line.slice(colon + 1).trim().replace(/^['"]|['"]$/g, '');
    if (key && value) result[key] = value;
  }
  return result;
}

function applyMetadata(
  markdownText: string,
  setters: {
    setSkillName: (value: string) => void;
    setSkillSlug: (value: string) => void;
    setSkillDescription: (value: string) => void;
    setSkillHomepage: (value: string) => void;
  },
) {
  const metadata = parseMetadata(markdownText);
  if (metadata.name || metadata.title) setters.setSkillName(metadata.name || metadata.title);
  if (metadata.slug || metadata.id) setters.setSkillSlug(metadata.slug || metadata.id);
  if (metadata.description || metadata.summary) setters.setSkillDescription(metadata.description || metadata.summary);
  if (metadata.homepage || metadata.url) setters.setSkillHomepage(metadata.homepage || metadata.url);
}

function normalizedSkillFiles(files: GeneralSkillFile[] = []): string {
  return JSON.stringify(
    [...files]
      .map((file) => ({
        path: file.path,
        content: file.content,
        mime_type: file.mime_type || '',
      }))
      .sort((a, b) => a.path.localeCompare(b.path)),
  );
}

function SectionCard({
  className,
  bodyClassName,
  title,
  extra,
  loading,
  children,
  ...rest
}: {
  className?: string;
  bodyClassName?: string;
  title?: ReactNode;
  extra?: ReactNode;
  loading?: boolean;
  children?: ReactNode;
} & Omit<HTMLAttributes<HTMLDivElement>, 'title'>) {
  return (
    <section className={cn(SECTION_CARD_CLASS, 'overflow-hidden', className)} {...rest}>
      {(title || extra) && (
        <div className="flex min-h-[40px] items-center justify-between gap-[12px]">
          <div className={cn('min-w-0', SECTION_CARD_TITLE_CLASS)}>{title}</div>
          {extra ? <div className="shrink-0">{extra}</div> : null}
        </div>
      )}
      <div className={cn('min-h-0 flex-1', bodyClassName)}>
        {loading ? (
          <div className="py-[24px] text-center text-[13px] text-[#858b9c]">加载中…</div>
        ) : (
          children
        )}
      </div>
    </section>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-[6px]">
      <span className={FIELD_LABEL_CLASS}>{label}</span>
      {children}
    </div>
  );
}

function GeneralSkillEditorPage({ mode, currentUser, onLogout }: { mode: 'new' | 'edit' } & GeneralSkillPageProps) {
  const navigate = useNavigate();
  const { slug: routeSlug } = useParams();
  const [rows, setRows] = useState<GeneralSkillRead[]>([]);
  const [markdown, setMarkdown] = useState(EMPTY_SKILL_MARKDOWN);
  const [skillName, setSkillName] = useState('');
  const [skillSlug, setSkillSlug] = useState('');
  const [skillDescription, setSkillDescription] = useState('');
  const [skillHomepage, setSkillHomepage] = useState('');
  const [skillFiles, setSkillFiles] = useState<GeneralSkillFile[]>([
    { path: 'SKILL.md', content: EMPTY_SKILL_MARKDOWN, size: EMPTY_SKILL_MARKDOWN.length, mime_type: 'text/markdown' },
  ]);
  const [selectedSlug, setSelectedSlug] = useState<string>();
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [runResult, setRunResult] = useState<GeneralSkillRunResponse | null>(null);
  const [liveResult, setLiveResult] = useState<Partial<GeneralSkillRunResponse> | null>(null);
  const [modelConfigs, setModelConfigs] = useState<ModelConfigRead[]>([]);
  const [selectedRunModelId, setSelectedRunModelId] = useState(
    () => window.localStorage.getItem(`${GENERAL_SKILL_RUN_MODEL_STORAGE_KEY}:${getRequestTenantId()}`) || '',
  );
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [selectedFilePath, setSelectedFilePath] = useState('SKILL.md');
  const [editorScroll, setEditorScroll] = useState({ top: 0, left: 0 });
  const [clawhubModalOpen, setClawhubModalOpen] = useState(false);
  const [clawhubSource, setClawhubSource] = useState('');
  const [clawhubLoading, setClawhubLoading] = useState(false);
  const [agentImportOpen, setAgentImportOpen] = useState(false);
  const [agentImportMode, setAgentImportMode] = useState<GeneralSkillImportMode>('plaza');
  const [agentImportLoading, setAgentImportLoading] = useState(false);
  const [agentImportAgents, setAgentImportAgents] = useState<AgentProfileRead[]>([]);
  const [agentImportSourceAgentId, setAgentImportSourceAgentId] = useState('');
  const [agentImportSourceSkills, setAgentImportSourceSkills] = useState<GeneralSkillRead[]>([]);
  const [agentImportSelectedSkillIds, setAgentImportSelectedSkillIds] = useState<string[]>([]);
  const [agentId, setAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [isOverallAgent, setIsOverallAgent] = useState(true);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [deleteSkillTarget, setDeleteSkillTarget] = useState<GeneralSkillRead | null>(null);
  const [deleteFileTarget, setDeleteFileTarget] = useState<GeneralSkillFile | null>(null);
  const [renameTarget, setRenameTarget] = useState<GeneralSkillFile | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [importPrepareOpen, setImportPrepareOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const clawhubAbortRef = useRef<AbortController | null>(null);
  const importPrepareActionRef = useRef<null | (() => void | Promise<void>)>(null);

  const selectedSkill = useMemo(
    () => rows.find((row) => row.slug === selectedSlug),
    [rows, selectedSlug],
  );
  const activeResult = runResult || liveResult;
  const selectedFile = useMemo(
    () => skillFiles.find((file) => file.path === selectedFilePath) || skillFiles[0],
    [skillFiles, selectedFilePath],
  );
  const selectedFileLanguage = useMemo(() => languageFromFilePath(selectedFile?.path), [selectedFile?.path]);
  const isNew = mode === 'new';
  const currentAgent = useMemo(() => agents.find((item) => item.id === agentId), [agents, agentId]);
  const canManageCurrentScope = currentAgent
    ? canManageEmployeeAgent(currentAgent, currentUser)
    : isEnterpriseAdmin(currentUser) && isOverallAgent;
  const pageTitle = isNew ? '新建空白技能' : '编辑技能';
  const pageDescription = isOverallAgent
    ? (isNew
      ? '填写技能定义并编辑 SKILL.md，保存后可在右侧运行测试。'
      : '维护技能广场中的技能定义、文件包和运行测试。')
    : (isNew
      ? '为当前数字员工创建技能，填写基本信息并编辑技能文件。'
      : '维护当前数字员工技能的定义、文件包和运行测试。');

  const load = () => {
    const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
    return api
      .get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${getRequestTenantId()}${agentSuffix}`)
      .then((items) => {
        setRows(items);
        if (mode === 'edit') {
          const target = items.find((item) => item.slug === routeSlug);
          if (target) {
            editSkill(target);
          } else if (routeSlug) {
            notify.error('未找到要编辑的技能');
          }
        }
      })
      .catch((error) => notify.error(error.message));
  };

  useEffect(() => {
    if (mode === 'new') {
      newSkill();
    }
    void load();
  }, [agentId, mode, routeSlug]);

  useEffect(() => {
    api
      .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${getRequestTenantId()}`)
      .then((items) => {
        setAgents(items);
        setIsOverallAgent(Boolean(items.find((item) => item.id === agentId)?.is_overall ?? true));
      })
      .catch(() => setIsOverallAgent(true));
  }, [agentId]);

  useEffect(() => {
    api
      .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${getRequestTenantId()}`)
      .then((items) => {
        const enabled = items.filter((item) => item.enabled);
        setModelConfigs(enabled);
        setSelectedRunModelId((current) => {
          if (current && enabled.some((item) => item.id === current)) return current;
          const fallback = enabled.find((item) => item.is_default)?.id || enabled[0]?.id || '';
          if (fallback) {
            window.localStorage.setItem(`${GENERAL_SKILL_RUN_MODEL_STORAGE_KEY}:${getRequestTenantId()}`, fallback);
          }
          return fallback;
        });
      })
      .catch(() => setModelConfigs([]));
  }, []);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const detail = (event as CustomEvent<{ agentId?: string }>).detail;
      setAgentId(detail?.agentId || window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
    };
    window.addEventListener('gongge-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('gongge-enterprise-agent-scope-change', onScopeChange);
  }, []);

  useEffect(() => {
    folderInputRef.current?.setAttribute('webkitdirectory', '');
    folderInputRef.current?.setAttribute('directory', '');
  }, []);

  useEffect(() => {
    if (!skillFiles.length) return;
    if (!skillFiles.some((file) => file.path === selectedFilePath)) {
      const skillFile = skillFiles.find((file) => file.path.split('/').pop()?.toLowerCase() === 'skill.md');
      setSelectedFilePath(skillFile?.path || skillFiles[0].path);
    }
  }, [skillFiles, selectedFilePath]);

  useEffect(() => {
    setEditorScroll({ top: 0, left: 0 });
  }, [selectedFilePath]);

  function hasUnsavedEditingChanges(): boolean {
    if (!editingSlug) return false;
    const original = rows.find((row) => row.slug === editingSlug);
    if (!original) return false;
    const stableSlug = editingSlug || skillSlug;
    return (
      markdown !== original.skill_markdown
      || skillName !== original.name
      || stableSlug !== original.slug
      || skillDescription !== (original.description || '')
      || skillHomepage !== (original.homepage || '')
      || normalizedSkillFiles(skillFiles) !== normalizedSkillFiles(
        original.skill_files?.length ? original.skill_files : [{ path: 'SKILL.md', content: original.skill_markdown }],
      )
    );
  }

  async function importSkill(): Promise<GeneralSkillRead | null> {
    if (!canManageCurrentScope) {
      notify.error('只有管理员可以编辑技能广场内容');
      return null;
    }
    if (!markdown.trim()) {
      notify.warning('请先粘贴或上传 SKILL.md');
      return null;
    }
    setSaving(true);
    try {
      const row = await api.post<GeneralSkillRead>('/api/enterprise/general-skills/import', {
        tenant_id: getRequestTenantId(),
        agent_id: !isOverallAgent && agentId ? agentId : undefined,
        name: skillName.trim() || undefined,
        slug: editingSlug || skillSlug.trim() || undefined,
        description: skillDescription.trim() || undefined,
        homepage: skillHomepage.trim() || undefined,
        markdown,
        files: skillFiles.length ? skillFiles : [{ path: 'SKILL.md', content: markdown }],
        status: 'published',
        original_slug: editingSlug || undefined,
      });
      notify.success(editingSlug ? `已保存 ${row.name}` : `已新增 ${row.name}`);
      setSelectedSlug(row.slug);
      setEditingSlug(row.slug);
      setMarkdown(row.skill_markdown);
      setSkillName(row.name);
      setSkillSlug(row.slug);
      setSkillDescription(row.description || '');
      setSkillHomepage(row.homepage || '');
      setSkillFiles(row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md', content: row.skill_markdown }]);
      setSelectedFilePath((row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md' }])[0].path);
      setRows((current) => {
        const withoutSaved = current.filter((item) => item.id !== row.id && item.slug !== row.slug);
        return [row, ...withoutSaved];
      });
      navigate(`/enterprise/general-skills/${encodeURIComponent(row.slug)}/edit`, { replace: !editingSlug });
      void load();
      return row;
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存技能失败');
      return null;
    } finally {
      setSaving(false);
    }
  }

  function newSkill() {
    setMarkdown(EMPTY_SKILL_MARKDOWN);
    setSkillName('');
    setSkillSlug('');
    setSkillDescription('');
    setSkillHomepage('');
    setSkillFiles([{ path: 'SKILL.md', content: EMPTY_SKILL_MARKDOWN, size: EMPTY_SKILL_MARKDOWN.length, mime_type: 'text/markdown' }]);
    setSelectedFilePath('SKILL.md');
    setEditingSlug(null);
    setSelectedSlug(undefined);
    setQuery('');
    setRunResult(null);
    setLiveResult(null);
  }

  function editSkill(row: GeneralSkillRead) {
    setMarkdown(row.skill_markdown);
    setSkillName(row.name);
    setSkillSlug(row.slug);
    setSkillDescription(row.description || '');
    setSkillHomepage(row.homepage || '');
    setSkillFiles(row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md', content: row.skill_markdown }]);
    setSelectedFilePath((row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md' }])[0].path);
    setSelectedSlug(row.slug);
    setEditingSlug(row.slug);
    setRunResult(null);
  }

  function replaceRow(row: GeneralSkillRead) {
    setRows((current) => current.map((item) => (item.id === row.id ? row : item)));
    if (editingSlug === row.slug) {
      setSkillName(row.name);
      setSkillSlug(row.slug);
      setSkillDescription(row.description || '');
      setSkillHomepage(row.homepage || '');
      setMarkdown(row.skill_markdown);
      setSkillFiles(row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md', content: row.skill_markdown }]);
      setSelectedFilePath((row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md' }])[0].path);
    }
  }

  async function setSkillPublished(row: GeneralSkillRead, published: boolean) {
    if (!canManageCurrentScope) {
      notify.error('只有管理员可以编辑技能广场内容');
      return;
    }
    try {
      const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      const next = await api.post<GeneralSkillRead>(
        `/api/enterprise/general-skills/${row.slug}/${published ? 'publish' : 'archive'}?tenant_id=${getRequestTenantId()}${agentSuffix}`,
      );
      replaceRow(next);
      notify.success(published ? '已启用技能' : '已停用技能');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : published ? '发布失败' : '下线失败');
    }
  }

  async function runDeleteSkill() {
    const row = deleteSkillTarget;
    if (!row) return;
    if (!canManageCurrentScope) {
      notify.error('只有管理员可以编辑技能广场内容');
      return;
    }
    const branchMode = !isOverallAgent;
    try {
      const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      await api.delete(`/api/enterprise/general-skills/${row.slug}?tenant_id=${getRequestTenantId()}${agentSuffix}`);
      const nextRows = rows.filter((item) => item.id !== row.id);
      setRows(nextRows);
      if (selectedSlug === row.slug || editingSlug === row.slug) {
        const next = nextRows[0];
        if (next) {
          setSelectedSlug(next.slug);
          editSkill(next);
        } else {
          setSelectedSlug(undefined);
          newSkill();
        }
      }
      notify.success(branchMode ? '已移除技能' : '已删除技能');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除失败');
    } finally {
      setDeleteSkillTarget(null);
    }
  }

  function startImportedDraft() {
    setEditingSlug(null);
    setSelectedSlug(undefined);
    setRunResult(null);
    setLiveResult(null);
  }

  async function withImportPreparation(importAction: () => void | Promise<void>) {
    if (!hasUnsavedEditingChanges()) {
      await importAction();
      return;
    }
    importPrepareActionRef.current = importAction;
    setImportPrepareOpen(true);
  }

  async function confirmImportPrepareSave() {
    const action = importPrepareActionRef.current;
    setImportPrepareOpen(false);
    const saved = await importSkill();
    if (saved && action) await action();
    importPrepareActionRef.current = null;
  }

  async function confirmImportPrepareSkip() {
    const action = importPrepareActionRef.current;
    setImportPrepareOpen(false);
    importPrepareActionRef.current = null;
    if (action) await action();
  }

  function requestImport(kind: 'file' | 'folder') {
    void withImportPreparation(() => {
      if (kind === 'folder') {
        folderInputRef.current?.click();
        return;
      }
      fileInputRef.current?.click();
    });
  }

  function requestClawHubImport() {
    void withImportPreparation(() => {
      clawhubAbortRef.current?.abort();
      clawhubAbortRef.current = null;
      setClawhubLoading(false);
      setClawhubSource('');
      setClawhubModalOpen(true);
    });
  }

  function cancelClawHubImport() {
    clawhubAbortRef.current?.abort();
    clawhubAbortRef.current = null;
    setClawhubLoading(false);
    setClawhubModalOpen(false);
  }

  function requestAgentImport(mode: GeneralSkillImportMode) {
    void withImportPreparation(async () => {
      try {
        const agents = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${getRequestTenantId()}`);
        const firstSource = mode === 'plaza'
          ? openGalleryAgentId(agents)
          : visibleEmployeeAgents(agents, currentUser, { activeOnly: true, excludeAgentId: agentId })[0]?.id || '';
        setAgentImportMode(mode);
        setAgentImportAgents(agents);
        setAgentImportSourceAgentId(firstSource);
        setAgentImportSelectedSkillIds([]);
        setAgentImportOpen(true);
        if (firstSource) {
          await loadAgentImportSourceSkills(firstSource);
        } else {
          setAgentImportSourceSkills([]);
        }
      } catch (error) {
        notify.error(error instanceof Error ? error.message : '加载员工列表失败');
      }
    });
  }

  async function loadAgentImportSourceSkills(sourceAgentId: string) {
    setAgentImportSourceSkills([]);
    setAgentImportSelectedSkillIds([]);
    if (!sourceAgentId) return;
    try {
      const sourceRows = await api.get<GeneralSkillRead[]>(
        `/api/enterprise/general-skills?tenant_id=${getRequestTenantId()}&agent_id=${encodeURIComponent(sourceAgentId)}`,
      );
      const existingIds = new Set(rows.map((item) => item.id));
      setAgentImportSourceSkills(sourceRows.filter((item) => item.status === 'published' && !existingIds.has(item.id)));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载来源技能失败');
    }
  }

  async function submitAgentImportSkills() {
    if (!agentId) {
      notify.warning('请先选择一个数字员工');
      return;
    }
    if (!agentImportSourceAgentId) {
      notify.warning(agentImportMode === 'plaza' ? '请选择开放广场' : '请选择复制来源');
      return;
    }
    if (!agentImportSelectedSkillIds.length) {
      notify.warning('请选择要复制的技能');
      return;
    }
    setAgentImportLoading(true);
    try {
      await api.post(`/api/enterprise/agents/${encodeURIComponent(agentId)}/resources/import`, {
        tenant_id: getRequestTenantId(),
        source_agent_id: agentImportSourceAgentId,
        resource_type: 'general_skill',
        resource_ids: agentImportSelectedSkillIds,
      });
      notify.success(`已复制 ${agentImportSelectedSkillIds.length} 个技能`);
      setAgentImportOpen(false);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '复制技能失败');
    } finally {
      setAgentImportLoading(false);
    }
  }

  async function importClawHubSource() {
    if (!clawhubSource.trim()) {
      notify.warning('请输入开源平台地址、GitHub 仓库或 SKILL.md 链接');
      return;
    }
    const controller = new AbortController();
    clawhubAbortRef.current?.abort();
    clawhubAbortRef.current = controller;
    setClawhubLoading(true);
    try {
      const row = await api.postWithSignal<GeneralSkillRead>('/api/enterprise/general-skills/import-skillhub', {
        tenant_id: getRequestTenantId(),
        agent_id: !isOverallAgent && agentId ? agentId : undefined,
        source: clawhubSource.trim(),
        status: 'published',
      }, controller.signal);
      if (controller.signal.aborted) return;
      notify.success(`已新增 ${row.name}`);
      setRows((current) => [row, ...current.filter((item) => item.id !== row.id && item.slug !== row.slug)]);
      setSelectedSlug(row.slug);
      editSkill(row);
      setClawhubModalOpen(false);
      void load();
    } catch (error) {
      if (isAbortError(error)) {
        notify.info('已取消导入');
        return;
      }
      notify.error(error instanceof Error ? error.message : '从开源平台导入失败');
    } finally {
      if (clawhubAbortRef.current === controller) {
        clawhubAbortRef.current = null;
        setClawhubLoading(false);
      }
    }
  }

  async function importSkillPackageFile(file: File) {
    const controller = new AbortController();
    clawhubAbortRef.current?.abort();
    clawhubAbortRef.current = controller;
    setClawhubLoading(true);
    try {
      const contentBase64 = await fileToBase64(file);
      if (controller.signal.aborted) return;
      const row = await api.postWithSignal<GeneralSkillRead>('/api/enterprise/general-skills/import-package', {
        tenant_id: getRequestTenantId(),
        agent_id: !isOverallAgent && agentId ? agentId : undefined,
        filename: file.name,
        content_base64: contentBase64,
        status: 'published',
      }, controller.signal);
      if (controller.signal.aborted) return;
      notify.success(`已上传 ${row.name}`);
      setRows((current) => [row, ...current.filter((item) => item.id !== row.id && item.slug !== row.slug)]);
      setSelectedSlug(row.slug);
      editSkill(row);
      setClawhubModalOpen(false);
      void load();
    } catch (error) {
      if (isAbortError(error)) {
        notify.info('已取消导入');
        return;
      }
      notify.error(error instanceof Error ? error.message : '上传技能包失败');
    } finally {
      if (clawhubAbortRef.current === controller) {
        clawhubAbortRef.current = null;
        setClawhubLoading(false);
      }
    }
  }

  function updateSelectedFile(text: string) {
    if (!selectedFile) return;
    setSkillFiles((current) => current.map((file) => (
      file.path === selectedFile.path
        ? { ...file, content: text, size: text.length }
        : file
    )));
    if (selectedFile.path.split('/').pop()?.toLowerCase() === 'skill.md') {
      setMarkdown(text);
    }
  }

  function addSkillFile() {
    const base = 'notes.md';
    let candidate = base;
    let index = 2;
    while (skillFiles.some((file) => file.path === candidate)) {
      candidate = `notes-${index}.md`;
      index += 1;
    }
    setSkillFiles((current) => [...current, { path: candidate, content: '', size: 0, mime_type: 'text/markdown' }]);
    setSelectedFilePath(candidate);
  }

  function deleteSelectedFile() {
    if (!selectedFile) return;
    deleteSkillFile(selectedFile);
  }

  function deleteSkillFile(target: GeneralSkillFile) {
    if (target.path.split('/').pop()?.toLowerCase() === 'skill.md') {
      notify.warning('SKILL.md 是技能入口，不能删除');
      return;
    }
    setDeleteFileTarget(target);
  }

  function runDeleteFile() {
    const target = deleteFileTarget;
    if (!target) return;
    setSkillFiles((current) => current.filter((file) => file.path !== target.path));
    setDeleteFileTarget(null);
  }

  function renameSkillFile(target: GeneralSkillFile) {
    setRenameTarget(target);
    setRenameValue(target.path);
  }

  function runRenameFile() {
    const target = renameTarget;
    if (!target) return;
    {
      const nextPath = renameValue;
      {
        const normalized = normalizeSkillFilePath(nextPath);
        if (!normalized) {
          notify.error('文件名不能为空');
          return;
        }
        if (normalized === target.path) {
          setRenameTarget(null);
          return;
        }
        if (skillFiles.some((file) => file.path === normalized)) {
          notify.error('已存在同名文件');
          return;
        }
        setSkillFiles((current) => current.map((file) => (
          file.path === target.path
            ? { ...file, path: normalized }
            : file
        )));
        if (selectedFilePath === target.path) {
          setSelectedFilePath(normalized);
        }
        setRenameTarget(null);
      }
    }
  }

  async function runSkill() {
    const slug = selectedSkill?.slug;
    if (!slug) {
      notify.warning('请先导入技能');
      return;
    }
    if (!query.trim()) {
      notify.warning('请输入测试问题');
      return;
    }
    setLoading(true);
    setRunResult(null);
    setLiveResult({
      skill_slug: slug,
      execution_trace: [],
      generated_code: '',
      stdout: '',
      stderr: '',
      structured_result: {},
      reply: '',
    });
    const controller = new AbortController();
    let timedOut = false;
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, GENERAL_SKILL_RUN_TIMEOUT_MS);
    try {
      let completed = false;
      await streamPost(
        `/api/enterprise/general-skills/${slug}/run/stream`,
        {
          tenant_id: getRequestTenantId(),
          agent_id: agentId || undefined,
          user_id: 'enterprise_demo',
          query,
          model_config_id: selectedRunModelId || undefined,
          max_attempts: 10,
        },
        (item) => {
          if (item.event === 'trace') {
            const traceItem = item.data;
            setLiveResult((current) => {
              const previous = current || { skill_slug: slug, execution_trace: [] };
              const executionTrace = [...(previous.execution_trace || []), traceItem];
              const nextCode = typeof traceItem.code === 'string' && traceItem.code.trim()
                ? traceItem.code
                : previous.generated_code || '';
              const nextStructured = typeof traceItem.structured_result === 'object' && traceItem.structured_result
                ? traceItem.structured_result as Record<string, unknown>
                : previous.structured_result || {};
              const chunk = typeof traceItem.text === 'string' ? traceItem.text : '';
              const phase = typeof traceItem.phase === 'string' ? traceItem.phase : '';
              return {
                ...previous,
                execution_trace: executionTrace,
                generated_code: nextCode,
                stdout: phase === 'stdout_chunk'
                  ? `${previous.stdout || ''}${chunk}`
                  : typeof traceItem.stdout_preview === 'string' ? traceItem.stdout_preview : previous.stdout || '',
                stderr: phase === 'stderr_chunk'
                  ? `${previous.stderr || ''}${chunk}`
                  : typeof traceItem.stderr_preview === 'string' ? traceItem.stderr_preview : previous.stderr || '',
                structured_result: nextStructured,
              };
            });
          }
          if (item.event === 'complete') {
            const result = item.data as unknown as GeneralSkillRunResponse;
            completed = true;
            setRunResult(result);
            setLiveResult(null);
            notify.success('运行完成');
          }
          if (item.event === 'error') {
            const text = typeof item.data.message === 'string' ? item.data.message : '运行失败';
            completed = true;
            setLiveResult((current) => ({
              ...(current || { skill_slug: slug, execution_trace: [] }),
              stderr: text,
              structured_result: { success: false, error: text },
              reply: '运行失败',
            }));
            notify.error(text);
          }
        },
        controller.signal,
      );
      if (!completed) {
        notify.warning('运行流已结束，但未收到最终结果');
      }
    } catch (error) {
      const text = timedOut
        ? '技能运行超时，请检查模型或稍后重试。'
        : error instanceof Error ? error.message : '运行失败';
      setLiveResult((current) => ({
        ...(current || { skill_slug: slug, execution_trace: [] }),
        stderr: text,
        structured_result: { success: false, error: text },
        reply: '运行失败',
      }));
      notify.error(text);
    } finally {
      window.clearTimeout(timeoutId);
      setLoading(false);
    }
  }

  async function importSingleFile(target: File) {
    const text = await target.text();
    const nextFile = { path: 'SKILL.md', content: text, size: target.size, mime_type: target.type || 'text/markdown' };
    startImportedDraft();
    setSkillFiles([nextFile]);
    setSelectedFilePath('SKILL.md');
    setMarkdown(text);
    applyMetadata(text, { setSkillName, setSkillSlug, setSkillDescription, setSkillHomepage });
    notify.success(`已读取 ${target.name}`);
  }

  async function importSkillPackage(targets: DroppedSkillFile[]) {
    if (!targets.length) return;
    const nextFiles: GeneralSkillFile[] = [];
    let failedCount = 0;
    for (const { file, path } of targets) {
      try {
        const text = await file.text();
        nextFiles.push({
          path,
          content: text,
          size: file.size,
          mime_type: file.type || undefined,
        });
      } catch {
        failedCount += 1;
      }
    }
    if (!nextFiles.length) {
      notify.error('没有读取到可导入的技能文件');
      return;
    }
    nextFiles.sort((a, b) => a.path.localeCompare(b.path));
    startImportedDraft();
    setSkillFiles(nextFiles);
    const skillFile = nextFiles.find((item) => item.path.split('/').pop()?.toLowerCase() === 'skill.md');
    if (skillFile) {
      setMarkdown(skillFile.content);
      setSelectedFilePath(skillFile.path);
      applyMetadata(skillFile.content, { setSkillName, setSkillSlug, setSkillDescription, setSkillHomepage });
      notify.success(`已读取 ${nextFiles.length} 个文件${failedCount ? `，跳过 ${failedCount} 个无法读取文件` : ''}`);
    } else {
      setSelectedFilePath(nextFiles[0]?.path || 'SKILL.md');
      notify.warning('文件夹中没有找到 SKILL.md');
    }
  }

  async function importFolderFiles(fileList: FileList | null) {
    await importSkillPackage(Array.from(fileList || []).map((file) => ({ file, path: packagePath(file) })));
  }

  async function handleFileInputChange(event: ChangeEvent<HTMLInputElement>) {
    const target = event.target.files?.[0];
    if (target) {
      if (isSkillPackageArchive(target)) {
        await importSkillPackageFile(target);
      } else {
        await importSingleFile(target);
      }
    }
    event.target.value = '';
  }

  async function handleFolderInputChange(event: ChangeEvent<HTMLInputElement>) {
    await importFolderFiles(event.target.files);
    event.target.value = '';
  }

  function acceptsFileDrop(event: DragEvent<HTMLElement>): boolean {
    return Array.from(event.dataTransfer.types || []).includes('Files');
  }

  function handleDragEnter(event: DragEvent<HTMLElement>) {
    if (!acceptsFileDrop(event)) return;
    event.preventDefault();
    setDragActive(true);
  }

  function handleDragOver(event: DragEvent<HTMLElement>) {
    if (!acceptsFileDrop(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    setDragActive(true);
  }

  function handleDragLeave(event: DragEvent<HTMLElement>) {
    const nextTarget = event.relatedTarget;
    if (nextTarget instanceof Node && event.currentTarget.contains(nextTarget)) return;
    setDragActive(false);
  }

  async function handleDrop(event: DragEvent<HTMLElement>) {
    if (!acceptsFileDrop(event)) return;
    event.preventDefault();
    setDragActive(false);
    const dropped = await droppedSkillFiles(event.dataTransfer);
    if (!dropped.length) return;
    await withImportPreparation(async () => {
      if (dropped.length === 1 && !dropped[0].path.includes('/')) {
        if (isSkillPackageArchive(dropped[0].file)) {
          await importSkillPackageFile(dropped[0].file);
        } else {
          await importSingleFile(dropped[0].file);
        }
        return;
      }
      await importSkillPackage(dropped);
    });
  }

  const isLiveRunning = loading && !runResult;

  const importMenu = canManageCurrentScope ? (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <UIButton variant="outline" className={RETURN_BUTTON_CLASS}>
          <UploadOutlined className="size-[14px]!" />
          导入
        </UIButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
        <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => requestImport('file')}>选择文件</DropdownMenuItem>
        <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => requestImport('folder')}>选择文件夹</DropdownMenuItem>
        {!isOverallAgent && (
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => requestAgentImport('plaza')}>
            <UploadOutlined />
            从广场复制
          </DropdownMenuItem>
        )}
        <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => requestClawHubImport()}>
          <GithubOutlined />
          从开源平台导入
        </DropdownMenuItem>
        {!isOverallAgent && (
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => requestAgentImport('employee')}>
            <TeamOutlined />
            从数字员工复制技能
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  ) : null;

  return (
    <div
      className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]"
      aria-busy={loading || saving}
    >
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        title={pageTitle}
        description={pageDescription}
      />

      <div className="mt-[20px] mb-[16px] flex flex-wrap justify-end gap-[16px]">
        <UIButton variant="outline" className={RETURN_BUTTON_CLASS} onClick={() => navigate('/enterprise/general-skills')}>
          <IconArrowRight className="size-3.5 rotate-180" />
          返回技能
        </UIButton>
        {!isNew && canManageCurrentScope && (
          <UIButton variant="outline" className={RETURN_BUTTON_CLASS} onClick={() => navigate('/enterprise/general-skills/new')}>
            <PlusOutlined />
            新建技能
          </UIButton>
        )}
        {importMenu}
        {canManageCurrentScope && (
          <UIButton disabled={saving} className={PRIMARY_BUTTON_CLASS} onClick={() => void importSkill()}>
            保存
          </UIButton>
        )}
      </div>

      <div className="grid grid-cols-1 gap-[20px] xl:grid-cols-2 xl:grid-rows-[auto_minmax(0,1fr)] xl:items-stretch">
          <SectionCard title="基本信息">
            <div className="grid grid-cols-1 gap-[16px] md:grid-cols-2">
              <Field label="技能名称">
                <Input
                  value={skillName}
                  onChange={(event) => setSkillName(event.target.value)}
                  disabled={!canManageCurrentScope}
                  placeholder="例如 天气查询、代码审查"
                />
              </Field>
              <Field label="Slug">
                <Input
                  value={skillSlug}
                  onChange={(event) => {
                    if (editingSlug) return;
                    setSkillSlug(event.target.value);
                  }}
                  disabled={!canManageCurrentScope || Boolean(editingSlug)}
                  placeholder={editingSlug ? '创建后不可修改' : '用于路由和接口路径，例如 weather-zh'}
                />
              </Field>
              <Field label="描述">
                <Input
                  value={skillDescription}
                  onChange={(event) => setSkillDescription(event.target.value)}
                  disabled={!canManageCurrentScope}
                  placeholder="用于员工选择技能时的说明"
                />
              </Field>
              <Field label="主页链接">
                <Input
                  value={skillHomepage}
                  onChange={(event) => setSkillHomepage(event.target.value)}
                  disabled={!canManageCurrentScope}
                  placeholder="可选，参考文档或项目主页"
                />
              </Field>
            </div>
          </SectionCard>

          <SectionCard
            className="xl:col-start-2 xl:row-start-1"
            title="运行测试"
            extra={(
              <div className="flex flex-wrap items-center justify-end gap-[8px]">
                <ModelConfigDropdown
                  models={modelConfigs}
                  value={selectedRunModelId}
                  onChange={(modelId) => {
                    setSelectedRunModelId(modelId);
                    window.localStorage.setItem(`${GENERAL_SKILL_RUN_MODEL_STORAGE_KEY}:${getRequestTenantId()}`, modelId);
                  }}
                />
                <UIButton disabled={loading || !selectedSkill?.slug} className={PRIMARY_BUTTON_CLASS} onClick={() => void runSkill()}>
                  <ExperimentOutlined />
                  运行
                </UIButton>
              </div>
            )}
          >
            <div className="flex flex-col gap-[12px]">
              <Field label="选择技能">
                <UISelect value={selectedSkill?.slug} onValueChange={setSelectedSlug}>
                  <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, 'w-full')}>
                    <SelectValue placeholder={isNew && !selectedSkill ? '保存后可选择并测试' : '选择技能'} />
                  </SelectTrigger>
                  <SelectContent>
                    {rows.map((row) => (
                      <SelectItem key={row.slug} value={row.slug}>{`${row.name} / ${row.slug}`}</SelectItem>
                    ))}
                  </SelectContent>
                </UISelect>
              </Field>
              <Field label="测试问题">
                <Input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="输入要测试的问题"
                />
              </Field>
            </div>
          </SectionCard>

          <SectionCard
            className={cn(
              'flex h-full min-h-0 flex-col xl:col-start-1 xl:row-start-2',
              dragActive && SKILL_EDITOR_DRAG_ACTIVE_CLASS,
            )}
            bodyClassName="relative flex min-h-0 flex-1 flex-col p-0"
            onDragEnter={handleDragEnter}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            title={(
              <span className="flex items-center gap-[8px]">
                <IconProfileFile className="size-[14px] shrink-0 text-[#757f9c]" />
                <span>技能文件</span>
              </span>
            )}
          >
            <input
              ref={fileInputRef}
              className={HIDDEN_FILE_INPUT_CLASS}
              type="file"
              accept=".zip,.md,.markdown,.txt"
              onChange={handleFileInputChange}
              hidden
              aria-hidden="true"
              tabIndex={-1}
            />
            <input
              ref={folderInputRef}
              className={HIDDEN_FILE_INPUT_CLASS}
              type="file"
              multiple
              {...FOLDER_INPUT_PROPS}
              onChange={handleFolderInputChange}
              hidden
              aria-hidden="true"
              tabIndex={-1}
            />
            {dragActive && (
              <div className={SKILL_DROP_HINT_CLASS}>
                <UploadOutlined />
                <span>释放以导入 SKILL.md、zip 技能包或完整技能文件夹</span>
              </div>
            )}
            <div className={SKILL_FILE_EDITOR_CLASS}>
              <aside className={SKILL_FILE_TREE_CLASS}>
                <div className={SKILL_FILE_TREE_HEADER_CLASS}>
                  <IconFolder className="size-[14px] shrink-0 text-[#757f9c]" />
                  <span>文件</span>
                </div>
                <div className={SKILL_FILE_TREE_LIST_CLASS}>
                  {skillFiles.map((file) => (
                    <ContextMenu.Root key={file.path}>
                      <ContextMenu.Trigger asChild>
                        <button
                          type="button"
                          className={skillFileNodeClass(file.path === selectedFile?.path)}
                          onClick={() => setSelectedFilePath(file.path)}
                          onContextMenu={() => setSelectedFilePath(file.path)}
                          title={file.path}
                        >
                          <IconProfileFile className="size-[14px] shrink-0" />
                          <span className="min-w-0 truncate">{file.path}</span>
                        </button>
                      </ContextMenu.Trigger>
                      <ContextMenu.Portal>
                        <ContextMenu.Content className={MENU_CONTENT_CLASS}>
                          <ContextMenu.Item className={MENU_ITEM_CLASS} onSelect={() => renameSkillFile(file)}>
                            <EditOutlined />
                            重命名
                          </ContextMenu.Item>
                          <ContextMenu.Item className={MENU_ITEM_DANGER_CLASS} onSelect={() => deleteSkillFile(file)}>
                            <DeleteOutlined />
                            删除
                          </ContextMenu.Item>
                        </ContextMenu.Content>
                      </ContextMenu.Portal>
                    </ContextMenu.Root>
                  ))}
                </div>
                <div className={SKILL_FILE_TREE_ACTIONS_CLASS}>
                  <UIButton variant="outline" onClick={addSkillFile} className={RETURN_BUTTON_CLASS}>
                    <IconAdd className="size-[14px]" />
                    新建文件
                  </UIButton>
                  <UIButton
                    variant="outline"
                    onClick={deleteSelectedFile}
                    className={DELETE_BUTTON_CLASS}
                  >
                    <IconTrash className="size-[14px]" />
                    删除
                  </UIButton>
                </div>
              </aside>
              <section className={SKILL_FILE_PANE_CLASS}>
                <div className={SKILL_FILE_TAB_CLASS}>
                  <IconProfileFile className="size-[14px] shrink-0 text-[#757f9c]" />
                  <span className="min-w-0 truncate text-[#18181a]">{selectedFile?.path || '未选择文件'}</span>
                </div>
                <div className={SKILL_CODE_EDITOR_CLASS} data-language={selectedFileLanguage}>
                  <pre className={SKILL_CODE_HIGHLIGHT_CLASS} aria-hidden="true">
                    <code
                      className={SKILL_CODE_HIGHLIGHT_CODE_CLASS}
                      style={{
                        transform: `translate(${-editorScroll.left}px, ${-editorScroll.top}px)`,
                      }}
                    >
                      {renderCodeTokens(selectedFile?.content || '\u200b', selectedFileLanguage)}
                    </code>
                  </pre>
                  <textarea
                    className={SKILL_CODE_INPUT_CLASS}
                    value={selectedFile?.content || ''}
                    onChange={(event) => updateSelectedFile(event.target.value)}
                    onScroll={(event) => setEditorScroll({
                      top: event.currentTarget.scrollTop,
                      left: event.currentTarget.scrollLeft,
                    })}
                    spellCheck={false}
                  />
                </div>
              </section>
            </div>
          </SectionCard>

          <SectionCard
            className="flex h-full min-h-0 flex-col xl:col-start-2 xl:row-start-2"
            bodyClassName="flex min-h-0 flex-1 flex-col overflow-auto p-[18px]"
            title={(
              <span className="flex items-center gap-[8px]">
                <IconPlay className="size-[14px] shrink-0 text-[#757f9c]" />
                <span>运行结果</span>
                {activeResult && (
                  isLiveRunning
                    ? <span className="inline-flex items-center gap-[4px] rounded-full bg-[#e6f4ff] px-[8px] py-px text-[12px] font-bold text-[#0958d9]">运行中</span>
                    : resultSucceeded(activeResult)
                    ? <span className="inline-flex items-center gap-[4px] rounded-full bg-[#eafbf0] px-[8px] py-px text-[12px] font-bold text-[#018434]"><CheckCircleOutlined />成功</span>
                    : <span className="inline-flex items-center gap-[4px] rounded-full bg-[#fce7e7] px-[8px] py-px text-[12px] font-bold text-[#d20b0b]"><CloseCircleOutlined />失败</span>
                )}
              </span>
            )}
          >
            {activeResult ? (
              <div className={SKILL_RESULT_LAYOUT_CLASS}>
                {(() => {
                  const traceItems = activeResult.execution_trace || [];
                  const latestCodeIndex = traceItems.reduce(
                    (latest, traceItem, traceIndex) => (traceItemCode(traceItem) ? traceIndex : latest),
                    -1,
                  );
                  return (
                    <>
                <section className={SKILL_REPLY_PANEL_CLASS}>
                  <div className={SKILL_SECTION_LABEL_CLASS}>最终回复</div>
                  <p className={SKILL_REPLY_TEXT_CLASS}>
                    {activeResult.reply || (loading ? '正在运行技能...' : '暂无回复')}
                  </p>
                </section>

                <section>
                  <div className={SKILL_SECTION_LABEL_CLASS}>执行流程</div>
                  <div className={SKILL_TRACE_LIST_CLASS}>
                    {traceItems.map((item, index) => {
                      const phase = typeof item.phase === 'string' ? item.phase : '';
                      const detail = traceDetail(item);
                      const code = traceItemCode(item);
                      const codeTitle = typeof item.attempt === 'number'
                        ? `第 ${item.attempt} 次 Python runner`
                        : 'Python runner';
                      return (
                        <div className={SKILL_TRACE_ITEM_CLASS} key={`${phase || 'phase'}-${index}`}>
                          <div className={SKILL_TRACE_DOT_CLASS} />
                          <div className={SKILL_TRACE_ITEM_BODY_CLASS}>
                            <div className={SKILL_TRACE_TITLE_CLASS}>{PHASE_LABELS[phase] || String(item.message || phase || '执行')}</div>
                            <div className={SKILL_TRACE_MESSAGE_CLASS}>{String(item.message || '')}</div>
                            {detail && (
                              <RunCodePanel
                                className="mt-2"
                                title={phase === 'code_finished' ? '查看执行结果' : phase === 'stdout_chunk' ? '查看运行输出' : '查看详情'}
                                code={detail}
                                language={codeLanguage(detail)}
                                defaultOpen={phase === 'code_finished' || phase === 'code_timeout'}
                              />
                            )}
                            {code && (
                              <details className={cn(SKILL_TRACE_CODE_DETAILS_CLASS, 'mt-[10px]')} open={index === latestCodeIndex}>
                                <summary className={SKILL_TRACE_CODE_SUMMARY_CLASS}>
                                  {codeTitle}
                                  <TraceDisclosureLabel />
                                </summary>
                                <CodeBlock className={SKILL_CODE_BLOCK_CLASS} code={code} language="python" />
                              </details>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </section>

                <section>
                  <div className={SKILL_SECTION_LABEL_CLASS}>运行输出</div>
                  <div className={SKILL_OUTPUT_STACK_CLASS}>
                    <RunCodePanel
                      title="结构化结果"
                      code={formatJson(activeResult.structured_result) || '无结构化结果'}
                      language="json"
                      defaultOpen
                    />
                    <RunCodePanel
                      title="stdout"
                      code={formatJson(activeResult.stdout) || '无 stdout'}
                      language={codeLanguage(formatJson(activeResult.stdout), 'text')}
                    />
                    <RunCodePanel
                      title="stderr"
                      code={formatJson(activeResult.stderr) || '无 stderr'}
                      language={codeLanguage(formatJson(activeResult.stderr), 'text')}
                    />
                  </div>
                </section>
                    </>
                  );
                })()}
              </div>
            ) : (
              <div className="flex min-h-[560px] flex-1 flex-col items-center justify-center gap-[8px] text-center text-[13px] text-muted-foreground xl:min-h-0">
                运行后将在这里显示回复、执行流程、代码和输出
              </div>
            )}
          </SectionCard>
      </div>
      <ClawHubDialog
        open={clawhubModalOpen}
        loading={clawhubLoading}
        source={clawhubSource}
        onSourceChange={setClawhubSource}
        onClose={cancelClawHubImport}
        onSubmit={() => void importClawHubSource()}
      />
      <ResourceImportDialog
        open={agentImportOpen}
        loading={agentImportLoading}
        icon={<PlazaResourceIcon kind="general-skills" size="compact" />}
        title={agentImportMode === 'plaza' ? '从广场复制技能' : '从数字员工复制技能'}
        sourcePlaceholder={agentImportMode === 'plaza' ? '选择开放广场' : '选择复制来源'}
        sources={agentImportMode === 'plaza'
          ? openGalleryImportSourceOptions(agentImportAgents, '开放广场')
          : visibleEmployeeAgents(agentImportAgents, currentUser, { activeOnly: true, excludeAgentId: agentId })
            .map((item) => ({ value: item.id, label: item.name }))}
        sourceId={agentImportSourceAgentId}
        itemsLabel="选择技能"
        items={agentImportSourceSkills.map((item) => ({
          id: item.id,
          label: (
            <>
              {item.name}
              <span className="text-[#858b9c]"> · {item.slug}</span>
            </>
          ),
        }))}
        selectedIds={agentImportSelectedSkillIds}
        emptyText="没有可复制的技能"
        note={agentImportMode === 'plaza'
          ? '从开放广场复制可用技能；不会覆盖当前编辑区内容。'
          : '从数字员工复制可用技能；不会覆盖当前编辑区内容。'}
        onSourceChange={(value) => {
          setAgentImportSourceAgentId(value);
          void loadAgentImportSourceSkills(value);
        }}
        onSelectedChange={setAgentImportSelectedSkillIds}
        onClose={() => setAgentImportOpen(false)}
        onSubmit={() => void submitAgentImportSkills()}
      />

      <ConfirmDialog
        open={Boolean(deleteSkillTarget)}
        onOpenChange={(open) => !open && setDeleteSkillTarget(null)}
        title={deleteSkillTarget ? `${isOverallAgent ? '删除' : '移除'}技能「${deleteSkillTarget.name}」？` : ''}
        description={isOverallAgent
          ? '删除后该技能不会再出现在组织技能库中，此操作不可撤销。'
          : '这只会在当前数字员工中隐藏该技能；开放广场和其他数字员工仍然保留。'}
        confirmText={isOverallAgent ? '删除' : '移除'}
        onConfirm={() => void runDeleteSkill()}
      />

      <ConfirmDialog
        open={Boolean(deleteFileTarget)}
        onOpenChange={(open) => !open && setDeleteFileTarget(null)}
        title={deleteFileTarget ? `删除文件「${deleteFileTarget.path}」？` : ''}
        description="删除后需要重新导入或手动新建该文件。"
        confirmText="删除"
        onConfirm={runDeleteFile}
      />

      <Dialog
        open={importPrepareOpen}
        onOpenChange={(open) => { if (!open) { setImportPrepareOpen(false); importPrepareActionRef.current = null; } }}
      >
        <DialogContent aria-describedby={undefined} className="flex w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[460px]">
          <DialogTitle className="border-b border-border px-[24px] py-[16px] text-[16px] font-semibold text-foreground">
            导入新技能前是否保存当前技能？
          </DialogTitle>
          <p className="px-[24px] py-[16px] text-[13px] leading-[20px] text-[#4f5669]">
            你正在编辑现有技能。导入会进入新建状态，不会覆盖当前技能。
          </p>
          <div className="flex items-center justify-end gap-[8px] bg-background px-[24px] py-[12px]">
            <UIButton
              variant="outline"
              onClick={() => { setImportPrepareOpen(false); importPrepareActionRef.current = null; }}
              className="h-[32px] rounded-[10px] border-[#e3e7f1] bg-white px-[14px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              取消
            </UIButton>
            <UIButton
              variant="outline"
              onClick={() => void confirmImportPrepareSkip()}
              className="h-[32px] rounded-[10px] border-[#e3e7f1] bg-white px-[14px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              不保存，继续导入
            </UIButton>
            <UIButton
              onClick={() => void confirmImportPrepareSave()}
              className="h-[36px] rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-[14px] text-[14px] font-semibold text-white hover:bg-[#244bc7]"
            >
              保存并发布
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(renameTarget)} onOpenChange={(open) => { if (!open) setRenameTarget(null); }}>
        <DialogContent aria-describedby={undefined} className="flex w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[420px]">
          <DialogTitle className="border-b border-border px-[24px] py-[16px] text-[16px] font-semibold text-foreground">
            重命名文件
          </DialogTitle>
          <div className="px-[24px] py-[16px]">
            <Input
              autoFocus
              value={renameValue}
              onChange={(event) => setRenameValue(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  runRenameFile();
                }
              }}
            />
          </div>
          <div className="flex items-center justify-end gap-[8px] bg-background px-[24px] py-[12px]">
            <UIButton
              variant="outline"
              onClick={() => setRenameTarget(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              取消
            </UIButton>
            <UIButton
              onClick={runRenameFile}
              className="h-[36px] w-[80px] rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-[12px] text-[14px] font-semibold text-white hover:bg-[#244bc7]"
            >
              重命名
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
