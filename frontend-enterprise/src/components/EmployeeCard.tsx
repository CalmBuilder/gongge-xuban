import {
  Checkbox,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui';
import { cn } from '@/lib/utils';

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
  resourceCount,
} from '../employee';
import type { AgentProfileRead } from '../types';
import EmployeeAvatar from './EmployeeAvatar';

// Hover colors come from the scoped --accent / --accent-foreground overrides on
// DropdownMenuContent (see below), so items only need layout + default color here.
// Kept in sync with the ScheduledTasksTab action menu.
const MENU_ITEM_CLASS =
  'cursor-pointer gap-[4px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[#858b9c] focus:text-[#18181a]';
const MENU_ITEM_DANGER_CLASS =
  'cursor-pointer gap-[4px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[#d20b0b] focus:bg-[#fce7e7] focus:text-[#d20b0b] focus:[&_svg]:text-[#d20b0b]!';

export type EmployeeCardProps = {
  employee: AgentProfileRead;
  canManage: boolean;
  canGovern?: boolean;
  canChat?: boolean;
  selected?: boolean;
  busy?: boolean;
  relationLabels?: string[];
  /** Show the top-right "更多" actions menu. Hidden on the 对话端 gallery. */
  showMenu?: boolean;
  showExpertSource?: boolean;
  showExpertDepartment?: boolean;
  selectable?: boolean;
  checked?: boolean;
  onOpen: () => void;
  onStatus: (status: 'active' | 'archived') => void;
  onGallery: (published: boolean) => void;
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
  relationLabels = [],
  showMenu = true,
  showExpertSource = true,
  showExpertDepartment = true,
  selectable = false,
  checked = false,
  onOpen,
  onStatus,
  onGallery,
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
  const isExpert = isExpertEmployee(employee);
  const expertSource = expertSourceLabel(employee);
  const expertDepartment = expertCategory(employee);
  const expertDirection = expertSubcategory(employee);
  const readiness = expertReadiness(employee);
  const unresolvedRequirements = expertUnresolvedRequirements(employee);
  const sourceUrl = expertUpstreamUrl(employee);
  const readinessPresentation = {
    ready: { label: '能力已就绪', className: 'border-[#bbebd0] bg-[#effaf3] text-[#237a48]' },
    partial: { label: '部分能力待接入', className: 'border-[#f0d6a7] bg-[#fff8e8] text-[#9a6414]' },
    blocked: { label: '核心执行能力待接入', className: 'border-[#d8deea] bg-[#f3f5f9] text-[#657087]' },
  }[readiness];

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
        'gongge-employee-card group relative flex h-full flex-col cursor-pointer overflow-visible rounded-[14px] border border-[var(--gg-border)] bg-white px-[14px] py-[14px] transition-[border-color,box-shadow,transform] hover:-translate-y-[2px] hover:border-[#c8d4f0] hover:shadow-[0_12px_28px_rgba(15,23,42,0.08)]',
        '',
        selected && 'border-[var(--gg-cobalt)] shadow-[0_16px_36px_rgba(49,87,232,0.14)]',
        checked && 'ring-2 ring-[color-mix(in_srgb,var(--gg-cobalt)_24%,transparent)]',
      )}
    >
      {selectable && isExpert && (
        <div className="absolute left-[12px] top-[12px] z-20 rounded-[7px] bg-white p-[5px] shadow-[0_3px_10px_rgba(49,87,232,0.12)]">
          <Checkbox
            aria-label={`选择${displayName}`}
            checked={checked}
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.stopPropagation()}
            onCheckedChange={(value) => onCheckedChange?.(value === true)}
          />
        </div>
      )}
      {/* Header band (shorter than the avatar so the illustration overflows above it) */}
      <div className="gongge-employee-identity mt-[30px] flex h-[72px] box-border gap-[12px] rounded-[12px] bg-[#eef3ff] p-[10px]" >

        {/* Avatar illustration — absolutely positioned so its head pokes above the gray band */}
        <div className='w-[80px] relative'>
          <div className='absolute inset-0 flex items-end justify-center'>
            <EmployeeAvatar
              agent={employee}
              width={80}
              height={94}
              fit="contain"
              objectPosition="center bottom"
              className="overflow-visible! rounded-none! border-0! bg-transparent! bg-none! shadow-none! after:hidden!"
            />
          </div>
          

        </div>

        {/* Name / role / status */}
        <div className="flex-1 flex flex-col gap-[2px]">
          <strong className="truncate text-[14px] font-semibold text-[var(--gg-ink)]">
            {employee.is_overall ? displayName : <span data-i18n-ignore>{displayName}</span>}
          </strong>
          <span className="truncate text-[11px] text-[var(--gg-slate)]">
            {rawRoleName === '待补充岗位' ? rawRoleName : <span data-i18n-ignore>{rawRoleName}</span>}
          </span>
          <div className="leading-none">
            <span className="inline-flex items-center gap-[4px] rounded-[90px] bg-white/80 px-[7px] py-[2px] text-[11px] font-medium text-[var(--gg-slate)]">
              <i className={cn('size-[6px] shrink-0 rounded-full', online ? 'bg-[#22c55e]' : 'bg-[#9ca3af]')} aria-hidden="true" />
              {online ? '在线' : '下线'}
            </span>
          </div>
        </div>

        {/* Chat button */}
        <button
          type="button"
          aria-label="发起对话"
          disabled={!online || busy || !canChat}
          onClick={(event) => {
            event.stopPropagation();
            onChat();
          }}
          className="grid size-[30px] shrink-0 self-center place-items-center rounded-[10px] bg-white text-[var(--gg-cobalt)] shadow-[0_4px_12px_rgba(49,87,232,0.10)] transition-colors hover:bg-[var(--gg-cobalt)] hover:text-white disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:bg-white disabled:hover:text-[var(--gg-slate)]"
        >
          <IconChat className="size-[16px]!" />
        </button>

      </div>

      {/* Actions menu */}
      {showMenu && (
      <div className="absolute right-[12px] top-[12px] z-20">
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label="员工操作"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={(event) => event.stopPropagation()}
            className="grid size-7 place-items-center rounded-[10px] text-[#757F9C] transition-colors outline-none hover:bg-black/5 focus-visible:bg-black/5"
          >
            <IconMore className="size-[16px]!" />
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="end"
            className="flex w-auto min-w-[128px] flex-col gap-[4px] rounded-[14px] border-0 bg-white p-[4px] shadow-[0px_0px_8px_rgba(0,0,0,0.1)] ring-0 [--accent:#F6F6F6] [--accent-foreground:#18181A]"
            onCloseAutoFocus={(event) => event.preventDefault()}
          >
            <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={!online || busy || !canChat}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onChat()}
            >
              <IconChat className="size-[16px]" />
              发起对话
            </DropdownMenuItem>
            {online ? (
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
            )}
            <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={!canGovern || busy}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onGallery(!galleryPublished)}
            >
              <IconPlatform className="size-[16px]" />
              {galleryPublished ? '从广场下架' : '发布到广场'}
            </DropdownMenuItem>
            <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={!canManage || busy}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onEdit()}
            >
              <IconEdit className="size-[16px]" />
              编辑资料
            </DropdownMenuItem>
            {isExpert && onEditClassification && (
              <DropdownMenuItem
                className={MENU_ITEM_CLASS}
                disabled={!canManage || busy}
                onClick={(event) => event.stopPropagation()}
                onSelect={onEditClassification}
              >
                <IconEdit className="size-[16px]" />
                编辑专家分类
              </DropdownMenuItem>
            )}
            <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={!canManage || busy}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onAvatar()}
            >
              <IconImage className="size-[16px]" />
              设置头像
            </DropdownMenuItem>
            <DropdownMenuSeparator className="my-[2px] bg-[#eef0f4]" />
            <DropdownMenuItem
              variant="destructive"
              className={MENU_ITEM_DANGER_CLASS}
              disabled={!canManage || busy}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onDelete()}
            >
              <IconTrash className="size-[16px]" />
              删除
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      )}

      {/* Description */}
      <p className="mt-[12px] line-clamp-2 h-[38px] shrink-0 text-[12px] leading-[19px] text-[var(--gg-slate)]">
        {employee.description ? <span data-i18n-ignore>{displayDescription}</span> : displayDescription}
      </p>

      {relationLabels.length > 0 && (
        <div className="mt-[9px] flex flex-wrap items-center gap-[5px] text-[11px] leading-[16px]">
          {relationLabels.map((label) => (
            <span
              key={label}
              className="rounded-full border border-[#d7e1f5] bg-[#f4f7fd] px-[7px] py-[2px] font-medium text-[#52617d]"
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
          className="mt-[8px] w-fit rounded-full border border-[#cbd8f2] bg-white px-[10px] py-[3px] text-[11px] font-medium text-[var(--gg-cobalt)] hover:bg-[#f3f6fd] disabled:opacity-50"
        >
          {usageActionLabel}
        </button>
      )}

      {isExpert && (
        <div className="mt-[9px] flex flex-wrap items-center gap-[5px] text-[11px] leading-[16px]">
          <span className="rounded-full border border-[#ccd9f4] bg-[#f2f6ff] px-[7px] py-[2px] font-semibold text-[var(--gg-cobalt)]">
            专家（能力分身）
          </span>
          {showExpertSource && expertSource && (
            <span data-testid="expert-source-badge" className="rounded-full border border-[#e0e5ef] bg-white px-[7px] py-[2px] text-[#657087]" data-i18n-ignore>
              {expertSource}
            </span>
          )}
          <span
            className={cn('rounded-full border px-[7px] py-[2px] font-medium', readinessPresentation.className)}
            title={unresolvedRequirements.length ? `未接入能力：${unresolvedRequirements.join('、')}` : undefined}
          >
            {readinessPresentation.label}
          </span>
          {showExpertDepartment && expertDepartment && (
            <span data-testid="expert-department-badge" className="max-w-[110px] truncate rounded-full bg-[#f5f6f9] px-[7px] py-[2px] text-[#7b8498]" data-i18n-ignore>
              {expertDepartment}
            </span>
          )}
          {expertDirection && (
            <span className="max-w-[120px] truncate rounded-full bg-[#eef8f7] px-[7px] py-[2px] text-[#39766f]" data-i18n-ignore>
              {expertDirection}
            </span>
          )}
          {sourceUrl && (
            <a
              href={sourceUrl}
              target="_blank"
              rel="noreferrer"
              aria-label="查看原始来源"
              className="ml-auto text-[11px] font-medium text-[var(--gg-cobalt)] underline decoration-[#b8c6f4] underline-offset-2 hover:decoration-[var(--gg-cobalt)]"
              onPointerDown={(event) => event.stopPropagation()}
              onClick={(event) => event.stopPropagation()}
            >
              查看原始来源
            </a>
          )}
        </div>
      )}

      {/* Work style tags */}
      <div className={cn('flex flex-wrap items-center gap-[6px]', isExpert ? 'my-[8px]' : 'my-[10px]')}>
        {profile.workStyles.slice(0, 3).map((item) => (
          <span
            key={item}
            className="rounded-[20px] border border-[#dce5f7] bg-[#f7f9fe] px-[8px] py-[2px] text-[11px] leading-[15px] text-[var(--gg-slate)]"
          >
            <span data-i18n-ignore>{item}</span>
          </span>
        ))}
      </div>

      {/* Stats — pinned to the bottom of the card */}
      <div className="mt-auto grid grid-cols-3 rounded-[12px] border border-[var(--gg-border)] bg-[#fbfcff]">
        {stats.map((stat, index) => (
          <div
            key={stat.label}
            className={cn(
              'flex flex-col justify-center gap-[4px] px-[20px] py-[6px]',
              index < stats.length - 1 && 'border-r border-[var(--gg-border)]',
            )}
          >
            <strong className="text-[18px] font-semibold leading-[24px] text-[var(--gg-ink)]">{stat.value}</strong>
            <em className="text-[11px] not-italic text-[var(--gg-slate)]">{stat.label}</em>
          </div>
        ))}
      </div>
    </div>
  );
}
