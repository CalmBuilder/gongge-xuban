import {
  Checkbox,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui';
import { cn } from '@/lib/utils';
import {
  MENU_ITEM_CLASS,
  MENU_ITEM_DANGER_CLASS,
  RESOURCE_CARD_AVATAR_SLOT_CLASS,
  RESOURCE_CARD_CLASS,
  RESOURCE_CARD_IDENTITY_CLASS,
} from '@/lib/enterprise-ui';

import IconChat from '../assets/icons/chat.svg?react';
import IconEdit from '../assets/icons/edit.svg?react';
import IconPlatform from '../assets/icons/nav-platform.svg?react';
import IconImage from '../assets/icons/image.svg?react';
import IconMore from '../assets/icons/more.svg?react';
import IconPause from '../assets/icons/pause.svg?react';
import IconPlay from '../assets/icons/play.svg?react';
import IconTrash from '../assets/icons/trash.svg?react';
import { isGalleryEmployee } from '../auth';
import {
  employeeDisplayNameWithCreator,
  employeeProfile,
  expertCategory,
  expertReadiness,
  expertSourceLabel,
  expertSubcategory,
  expertUnresolvedRequirements,
  expertUpstreamUrl,
  isExpertEmployee,
  type EmployeeStatusKind,
  resourceCount,
} from '../employee';
import type { AgentProfileRead } from '../types';
import EmployeeAvatar from './EmployeeAvatar';

export type EmployeeCardProps = {
  employee: AgentProfileRead;
  canManage: boolean;
  canGovern?: boolean;
  canChat?: boolean;
  selected?: boolean;
  busy?: boolean;
  /** Override the status text when the card represents a template rather than a live employee. */
  statusLabel?: string;
  /** Distinguishes a live employee from a published-but-not-running template. */
  statusKind?: EmployeeStatusKind;
  /** The expert-template management card only exposes governance actions. */
  cardMode?: 'employee' | 'expert-template';
  /** Override the publication action wording for a resource-specific plaza. */
  publicationLabel?: string;
  relationLabels?: string[];
  /** Show the top-right "更多" actions menu. Hidden on the 对话端 gallery. */
  showMenu?: boolean;
  showExpertSource?: boolean;
  showExpertDepartment?: boolean;
  showGovernanceForm?: boolean;
  selectable?: boolean;
  checked?: boolean;
  onOpen: () => void;
  onStatus: (status: 'active' | 'archived') => void;
  onGallery: (published: boolean) => void;
  onPublication?: () => void;
  onDelete: () => void;
  onAvatar: () => void;
  onEdit: () => void;
  onChat: () => void;
  onCheckedChange?: (checked: boolean) => void;
  onEditClassification?: () => void;
  usageActionLabel?: string;
  onUsageAction?: () => void;
};

export default function EmployeeCard({
  employee,
  canManage,
  canGovern = canManage,
  canChat = true,
  selected = false,
  busy = false,
  statusLabel,
  statusKind,
  cardMode = 'employee',
  publicationLabel,
  relationLabels = [],
  showMenu = true,
  showExpertSource = true,
  showExpertDepartment = true,
  showGovernanceForm = false,
  selectable = false,
  checked = false,
  onOpen,
  onStatus,
  onGallery,
  onPublication,
  onDelete,
  onAvatar,
  onEdit,
  onChat,
  onCheckedChange,
  onEditClassification,
  usageActionLabel,
  onUsageAction,
}: EmployeeCardProps) {
  const profile = employeeProfile(employee);
  const sopCount = resourceCount(employee.resources, 'skill');
  const skillCount = resourceCount(employee.resources, 'general_skill');
  const kbCount = resourceCount(employee.resources, 'knowledge_base');
  const galleryPublished = isGalleryEmployee(employee);
  const online = employee.status === 'active';
  const effectiveStatusKind: EmployeeStatusKind = statusKind || (statusLabel ? 'available' : online ? 'online' : 'offline');
  const statusPresentation = {
    online: {
      badge: 'bg-[var(--gg-surface)] text-[var(--gg-text-muted)]',
      dot: 'bg-[var(--gg-state-success)]',
    },
    available: {
      badge: 'bg-[var(--gg-interaction-soft)] text-[var(--gg-interaction)]',
      dot: 'bg-[var(--gg-interaction)]',
    },
    offline: {
      badge: 'bg-[var(--gg-surface)] text-[var(--gg-text-muted)]',
      dot: 'bg-[var(--gg-text-muted)]',
    },
  }[effectiveStatusKind];
  const isExpertTemplateManagement = cardMode === 'expert-template';
  const isExpert = isExpertEmployee(employee);
  const expertSource = expertSourceLabel(employee);
  const expertDepartment = expertCategory(employee);
  const expertDirection = expertSubcategory(employee);
  const readiness = expertReadiness(employee);
  const unresolvedRequirements = expertUnresolvedRequirements(employee);
  const sourceUrl = expertUpstreamUrl(employee);
  const readinessPresentation = {
    ready: { label: '能力已就绪', className: 'border-[var(--gg-capability-line)] bg-[var(--gg-state-success-soft)] text-[var(--gg-state-success)]' },
    partial: { label: '部分能力待接入', className: 'border-[#F1D9B2] bg-[var(--gg-state-warning-soft)] text-[var(--gg-state-warning)]' },
    blocked: { label: '核心执行能力待接入', className: 'border-[var(--gg-line)] bg-[var(--gg-state-neutral-soft)] text-[var(--gg-state-neutral)]' },
  }[readiness];
  const governanceBadge = employee.governance_form === 'organization_employee'
    ? '专家·组织数字员工'
    : employee.governance_form === 'organization_pending'
      ? '专家·待组织化'
      : employee.governance_form === 'template'
        ? '专家模板'
        : '专家能力分身';
  const governanceFormBadge = employee.governance_form === 'organization_employee'
    ? '组织数字员工'
    : employee.governance_form === 'organization_pending'
      ? '待组织化'
      : employee.governance_form === 'template'
        ? '专家模板'
        : '能力分身';
  const governanceReasonText = (employee.governance_reasons || []).join('、');

  // Show raw API values on the card (bypass the 共格 term relabeling in normalizeProductDisplayText).
  const rawRoleName = (employee.metadata?.role_name as string | undefined) || profile.roleName;
  const displayName = employee.is_overall ? '开放广场' : employeeDisplayNameWithCreator(employee);
  const displayDescription = employee.description || '暂无描述';

  const stats: Array<{ value: number; label: string }> = [
    { value: kbCount, label: '资料' },
    { value: skillCount, label: '技能' },
    { value: sopCount, label: 'SOP' },
  ];

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => {
        if (!busy) onOpen();
      }}
      onKeyDown={(event) => {
        if (!busy && (event.key === 'Enter' || event.key === ' ')) {
          event.preventDefault();
          onOpen();
        }
      }}
      aria-pressed={selected}
      aria-busy={busy}
      className={cn(
        'gongge-employee-card cursor-pointer overflow-visible',
        RESOURCE_CARD_CLASS,
        selected && 'border-[var(--gg-cobalt)] shadow-[0_16px_36px_rgba(49,87,232,0.14)]',
        checked && 'ring-2 ring-[color-mix(in_srgb,var(--gg-cobalt)_24%,transparent)]',
      )}
    >
      {selectable && isExpert && (
        <div className="absolute left-[12px] top-[12px] z-20 rounded-[var(--gg-radius-control)] bg-[var(--gg-surface)] p-[5px] shadow-[0_3px_10px_rgba(49,87,232,0.12)]">
          <Checkbox
            aria-label={`选择${displayName}`}
            checked={checked}
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.stopPropagation()}
            onCheckedChange={(value) => onCheckedChange?.(value === true)}
          />
        </div>
      )}
      {/* Header band: the portrait stays inside a fixed column so long names never pass beneath it. */}
      <div data-resource-identity className={cn(
        'gongge-employee-identity',
        RESOURCE_CARD_IDENTITY_CLASS,
        'bg-[var(--gg-interaction-soft)]',
      )}>
        <div data-avatar-slot className={RESOURCE_CARD_AVATAR_SLOT_CLASS}>
          <EmployeeAvatar
            agent={employee}
            width={56}
            height={56}
            radius="var(--gg-radius-avatar-card)"
            fit="cover"
            objectPosition="center bottom"
            className="employee-resource-avatar border-0! bg-transparent! bg-none! shadow-none! after:hidden!"
          />
        </div>

        {/* Name / role / status */}
        <div className="min-w-0 flex flex-col gap-[2px]">
          <strong className="gg-type-card-title truncate">
            {employee.is_overall ? displayName : <span data-i18n-ignore>{displayName}</span>}
          </strong>
          <span className="gg-type-meta truncate">
            {rawRoleName === '待补充岗位' ? rawRoleName : <span data-i18n-ignore>{rawRoleName}</span>}
          </span>
          <div>
            <span className={cn('gg-type-caption inline-flex items-center gap-[4px] rounded-full px-[7px] py-[2px]', statusPresentation.badge)}>
              <i className={cn('size-[6px] shrink-0 rounded-full', statusPresentation.dot)} aria-hidden="true" />
              {statusLabel || (online ? '在线' : '下线')}
            </span>
          </div>
        </div>

        {/* Chat button */}
        {!isExpertTemplateManagement && <button
          type="button"
          aria-label="发起对话"
          disabled={!online || busy || !canChat}
          onClick={(event) => {
            event.stopPropagation();
            onChat();
          }}
          className="grid size-[30px] shrink-0 self-center place-items-center rounded-[var(--gg-radius-control)] bg-[var(--gg-surface)] text-[var(--gg-interaction)] shadow-[0_4px_12px_rgba(49,87,232,0.10)] transition-colors hover:bg-[var(--gg-interaction)] hover:text-white disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:bg-[var(--gg-surface)] disabled:hover:text-[var(--gg-text-muted)]"
        >
          <IconChat className="size-[16px]!" />
        </button>}

      </div>

      {/* Actions menu */}
      {showMenu && (
      <div className="absolute right-[12px] top-[12px] z-20">
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label="员工操作"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={(event) => event.stopPropagation()}
            className="grid size-7 place-items-center rounded-[var(--gg-radius-control)] text-[var(--gg-text-muted)] transition-colors outline-none hover:bg-[var(--gg-surface-subtle)] focus-visible:bg-[var(--gg-surface-subtle)]"
          >
            <IconMore className="size-[16px]!" />
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="end"
            className="flex w-auto min-w-[128px] flex-col gap-[4px] rounded-[var(--gg-radius-card)] border border-[var(--gg-line)] bg-[var(--gg-surface)] p-[4px] shadow-[var(--gg-shadow-card)] ring-0 [--accent:var(--gg-interaction-soft)] [--accent-foreground:var(--gg-text-primary)]"
            onCloseAutoFocus={(event) => event.preventDefault()}
          >
            {!isExpertTemplateManagement && <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={!canManage || busy || !onPublication}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onPublication?.()}
            >
              <IconPlatform className="size-[16px]" />
              提交组织审核
            </DropdownMenuItem>}
            {!isExpertTemplateManagement && <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={!online || busy || !canChat}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onChat()}
            >
              <IconChat className="size-[16px]" />
              发起对话
            </DropdownMenuItem>}
            {!isExpertTemplateManagement && (online ? (
              <DropdownMenuItem
                className={MENU_ITEM_CLASS}
                disabled={!canManage || busy}
                onClick={(event) => event.stopPropagation()}
                onSelect={() => onStatus('archived')}
              >
                <IconPause className="size-[16px]" />
                下线
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem
                className={MENU_ITEM_CLASS}
                disabled={!canManage || busy}
                onClick={(event) => event.stopPropagation()}
                onSelect={() => onStatus('active')}
              >
                <IconPlay className="size-[16px]" />
                上线
              </DropdownMenuItem>
            ))}
            <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={!canGovern || busy}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onGallery(!galleryPublished)}
            >
              <IconPlatform className="size-[16px]" />
              {publicationLabel || (galleryPublished ? '从广场下架' : '发布到广场')}
            </DropdownMenuItem>
            {!isExpertTemplateManagement && <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={!canManage || busy}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onEdit()}
            >
              <IconEdit className="size-[16px]" />
              编辑资料
            </DropdownMenuItem>}
            {isExpert && onEditClassification && (
              <DropdownMenuItem
                className={MENU_ITEM_CLASS}
                disabled={!(isExpertTemplateManagement ? canGovern : canManage) || busy}
                onClick={(event) => event.stopPropagation()}
                onSelect={onEditClassification}
              >
                <IconEdit className="size-[16px]" />
                编辑专家分类
              </DropdownMenuItem>
            )}
            {!isExpertTemplateManagement && <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={!canManage || busy}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onAvatar()}
            >
              <IconImage className="size-[16px]" />
              设置头像
            </DropdownMenuItem>}
            {!isExpertTemplateManagement && <DropdownMenuSeparator className="my-[2px] bg-[#eef0f4]" />}
            {!isExpertTemplateManagement && <DropdownMenuItem
              variant="destructive"
              className={MENU_ITEM_DANGER_CLASS}
              disabled={!canManage || busy}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onDelete()}
            >
              <IconTrash className="size-[16px]" />
              删除
            </DropdownMenuItem>}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      )}

      {/* Description */}
      <p className="gg-type-body mt-[12px] line-clamp-3 min-h-[66px] shrink-0">
        {employee.description ? <span data-i18n-ignore>{displayDescription}</span> : displayDescription}
      </p>

      {relationLabels.length > 0 && (
        <div className="gg-type-caption mt-[9px] flex flex-wrap items-center gap-[5px]">
          {relationLabels.map((label) => (
            <span
              key={label}
              className="gg-type-caption rounded-full border border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] px-[7px] py-[2px] text-[var(--gg-text-secondary)]"
            >
              {label}
            </span>
          ))}
        </div>
      )}
      {usageActionLabel && onUsageAction && (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            onUsageAction();
          }}
          disabled={busy}
          className="gg-type-caption mt-[8px] w-fit rounded-full border border-[var(--gg-line)] bg-[var(--gg-surface)] px-[10px] py-[3px] text-[var(--gg-interaction)] hover:bg-[var(--gg-interaction-soft)] disabled:opacity-50"
        >
          {usageActionLabel}
        </button>
      )}

      {isExpert && (
        <div className="gg-type-caption mt-[9px] flex flex-wrap items-center gap-[5px]">
          <span className="gg-type-caption rounded-full border border-[var(--gg-line)] bg-[var(--gg-interaction-soft)] px-[7px] py-[2px] font-semibold text-[var(--gg-interaction)]">
            {governanceBadge}
          </span>
          {showExpertSource && expertSource && (
            <span data-testid="expert-source-badge" className="gg-type-caption rounded-full border border-[var(--gg-line)] bg-[var(--gg-surface)] px-[7px] py-[2px] text-[var(--gg-text-secondary)]" data-i18n-ignore>
              {expertSource}
            </span>
          )}
          <span
            className={cn('gg-type-caption rounded-full border px-[7px] py-[2px]', readinessPresentation.className)}
            title={unresolvedRequirements.length ? `未接入能力：${unresolvedRequirements.join('、')}` : undefined}
          >
            {readinessPresentation.label}
          </span>
          {showExpertDepartment && expertDepartment && (
            <span data-testid="expert-department-badge" className="gg-type-caption max-w-[110px] truncate rounded-full bg-[var(--gg-state-neutral-soft)] px-[7px] py-[2px] text-[var(--gg-text-muted)]" data-i18n-ignore>
              {expertDepartment}
            </span>
          )}
          {expertDirection && (
            <span className="gg-type-caption max-w-[120px] truncate rounded-full bg-[var(--gg-capability-soft)] px-[7px] py-[2px] text-[var(--gg-capability)]" data-i18n-ignore>
              {expertDirection}
            </span>
          )}
          {sourceUrl && (
            <a
              href={sourceUrl}
              target="_blank"
              rel="noreferrer"
              aria-label="查看原始来源"
              className="gg-type-caption ml-auto text-[var(--gg-interaction)] underline decoration-[var(--gg-interaction)] underline-offset-2 hover:decoration-[var(--gg-interaction-hover)]"
              onPointerDown={(event) => event.stopPropagation()}
              onClick={(event) => event.stopPropagation()}
            >
              查看原始来源
            </a>
          )}
        </div>
      )}

      {showGovernanceForm && !isExpert && (
        <div className="gg-type-caption mt-[9px] flex flex-wrap items-center gap-[5px]">
          <span
            className="gg-type-caption rounded-full border border-[var(--gg-line)] bg-[var(--gg-interaction-soft)] px-[7px] py-[2px] font-semibold text-[var(--gg-interaction)]"
            title={governanceReasonText || undefined}
          >
            {governanceFormBadge}
          </span>
          {employee.governance_form === 'organization_pending' && governanceReasonText && (
            <span className="gg-type-caption rounded-full border border-[#F1D9B2] bg-[var(--gg-state-warning-soft)] px-[7px] py-[2px] text-[var(--gg-state-warning)]">
              需补齐组织前置
            </span>
          )}
        </div>
      )}

      {/* Work style tags */}
      <div className={cn('flex flex-wrap items-center gap-[6px]', isExpert ? 'my-[8px]' : 'my-[10px]')}>
        {profile.workStyles.slice(0, 3).map((item) => (
          <span
            key={item}
            className="gg-type-caption rounded-full border border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] px-[8px] py-[2px]"
          >
            <span data-i18n-ignore>{item}</span>
          </span>
        ))}
      </div>

      {/* Stats — pinned to the bottom of the card */}
      <div className="mt-auto grid grid-cols-3 rounded-[var(--gg-radius-card)] border border-[var(--gg-border)] bg-[var(--gg-surface-subtle)]">
        {stats.map((stat, index) => (
          <div
            key={stat.label}
            className={cn(
              'flex flex-col justify-center gap-[4px] px-[20px] py-[6px]',
              index < stats.length - 1 && 'border-r border-[var(--gg-border)]',
            )}
          >
            <strong className="gg-type-section-title font-semibold  text-[var(--gg-text-primary)]">{stat.value}</strong>
            <em className="gg-type-caption not-italic">{stat.label}</em>
          </div>
        ))}
      </div>
    </div>
  );
}
