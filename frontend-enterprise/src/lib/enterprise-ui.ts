import { formatClientDateTime } from './timezone';

/**
 * Shared Tailwind class tokens for the enterprise list pages (SOP, 技能, 定时任务,
 * 员工记忆, 对话日志 …). Keeping them in one place avoids copy-pasting the exact
 * same dropdown / select / card styling into every page.
 */

/** Dropdown menu item (icon + label, 12px muted text). */
export const MENU_ITEM_CLASS =
  'cursor-pointer gap-[6px] rounded-[var(--gg-radius-control)] px-[12px] py-[6px] text-[12px] text-[var(--gg-text-muted)] focus:bg-[var(--gg-interaction-soft)] focus:text-[var(--gg-text-primary)] [&_svg]:size-[14px]';

/** Destructive (red) dropdown menu item. */
export const MENU_ITEM_DANGER_CLASS =
  'cursor-pointer gap-[6px] rounded-[var(--gg-radius-control)] px-[12px] py-[6px] text-[12px] text-[var(--gg-state-danger)] focus:bg-[var(--gg-state-danger-soft)] focus:text-[var(--gg-state-danger)] focus:[&_svg]:text-[var(--gg-state-danger)]! [&_svg]:size-[14px]';

/** Dropdown menu popover container (rounded white card + soft shadow). */
export const MENU_CONTENT_CLASS =
  'flex w-auto min-w-[140px] flex-col gap-[4px] rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-[var(--gg-paper)] p-[4px] shadow-[var(--gg-shadow-card)] ring-0 [--accent:var(--gg-cloud)] [--accent-foreground:var(--gg-ink)]';

/** shadcn `Select` trigger styled to match the 34px filter controls. */
export const SELECT_TRIGGER_CLASS =
  'h-[34px] data-[size=default]:h-[34px] rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-[var(--gg-surface)] text-[12px] text-[var(--gg-text-primary)] shadow-none data-placeholder:text-[var(--gg-text-muted)] hover:border-[var(--gg-interaction)] focus-visible:border-[var(--gg-interaction)] focus-visible:ring-2 focus-visible:ring-[color-mix(in_srgb,var(--gg-interaction)_16%,transparent)]';

/** Mobile (<768px) list card wrapper. */
export const MOBILE_CARD_CLASS =
  'min-w-0 rounded-[var(--gg-radius-card)] border border-[var(--gg-border)] bg-[var(--gg-surface)] p-[16px] shadow-[var(--gg-shadow-card)]';

/** Dialog footer bar — white background, top border, right-aligned actions. */
export const DIALOG_FOOTER_CLASS =
  'flex items-center justify-end gap-[8px] border-t border-[var(--gg-border)] bg-[var(--gg-paper)] px-[24px] py-[12px]';

/** Standard dialog cancel button. */
export const DIALOG_CANCEL_BUTTON_CLASS =
  'h-[36px] min-w-[80px] rounded-[var(--gg-radius-control)] border-[var(--gg-border)] bg-[var(--gg-surface)] px-[14px] text-[14px] font-medium text-[var(--gg-text-muted)] hover:border-[var(--gg-interaction)] hover:bg-[var(--gg-interaction-soft)] hover:text-[var(--gg-text-primary)]';

/** Standard dialog primary confirm button. */
export const DIALOG_PRIMARY_BUTTON_CLASS =
  'h-[36px] min-w-[80px] rounded-[var(--gg-radius-control)] bg-[var(--gg-interaction)] px-[14px] text-[14px] font-semibold text-white shadow-[0_8px_18px_rgba(49,87,232,0.22)] hover:bg-[var(--gg-interaction-hover)]';

/** Standard outline action button (toolbar refresh, card actions, etc.). */
export const OUTLINE_ACTION_BUTTON_CLASS =
  'h-[34px] gap-[4px] rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-[var(--gg-surface)] px-[20px] text-[12px] font-medium text-[var(--gg-text-muted)] hover:border-[var(--gg-interaction)] hover:bg-[var(--gg-interaction-soft)] hover:text-[var(--gg-interaction)]';

/** Compact outline action button for inline card headers. */
export const OUTLINE_ACTION_BUTTON_SM_CLASS =
  'h-[32px] gap-[4px] rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-[var(--gg-surface)] px-[12px] text-[12px] font-medium text-[var(--gg-text-muted)] hover:border-[var(--gg-interaction)] hover:bg-[var(--gg-interaction-soft)] hover:text-[var(--gg-interaction)] [&_svg:not([class*="size-"])]:size-[14px]';

/** Integrated search combo wrapper (input + submit button). */
export const SEARCH_COMBO_CLASS =
  'flex h-[34px] min-w-0 items-stretch overflow-hidden rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-[var(--gg-surface)] transition-colors focus-within:border-[var(--gg-interaction)] focus-within:ring-2 focus-within:ring-[color-mix(in_srgb,var(--gg-interaction)_16%,transparent)]';

/** Integrated search combo input field. */
export const SEARCH_COMBO_INPUT_CLASS =
  'min-w-0 flex-1 bg-transparent px-[14px] text-[14px] text-[var(--gg-text-primary)] outline-none placeholder:text-[var(--gg-text-muted)]';

/** Integrated search combo submit button. */
export const SEARCH_COMBO_BUTTON_CLASS =
  'shrink-0 bg-[var(--gg-interaction)] px-[20px] text-[14px] font-semibold text-white transition-colors hover:bg-[var(--gg-interaction-hover)] disabled:pointer-events-none disabled:opacity-50';

/** Fixed resource-card contract used by the open platform and catalog grids. */
export const RESOURCE_CARD_CLASS =
  'group relative flex min-h-[288px] h-full w-full min-w-0 flex-col rounded-[var(--gg-radius-card)] border bg-[var(--gg-surface)] p-[16px] text-left transition-[border-color,box-shadow,transform] duration-150 hover:-translate-y-[2px] hover:shadow-[var(--gg-shadow-card)] motion-reduce:transition-none motion-reduce:hover:transform-none';

/** Resource-card identity strip: artwork, title and accent metric. */
export const RESOURCE_CARD_IDENTITY_CLASS =
  'mt-[24px] flex h-[72px] w-full shrink-0 items-center gap-[12px] rounded-[var(--gg-radius-card)] px-[12px]';

/** Resource-card description uses the shared body size and fixed three-line slot. */
export const RESOURCE_CARD_DESCRIPTION_CLASS =
  'gg-type-body mt-[14px] line-clamp-3 min-h-[60px] w-full';

/** Resource-card metadata strip remains visually subordinate to the content. */
export const RESOURCE_CARD_FOOTER_CLASS =
  'mt-auto flex min-h-[48px] w-full items-center gap-[6px] overflow-hidden rounded-[var(--gg-radius-control)] border border-[var(--gg-line)] bg-[var(--gg-surface-subtle)] px-[10px]';

/** Shared fact tile for drawer, dialog and detail-page relationship metadata. */
export const DETAIL_FACT_CARD_CLASS =
  'min-w-0 rounded-[var(--gg-radius-card)] border border-[var(--gg-line)] bg-[var(--gg-surface-subtle)]';

/** Standard detail-page section container. */
export const DETAIL_PANEL_CLASS =
  'rounded-[var(--gg-radius-panel)] border border-[var(--gg-line)] bg-[var(--gg-surface)] p-[16px] shadow-[var(--gg-shadow-card)]';

/** Bottom action row shared by dialogs and detail surfaces. */
export const DETAIL_ACTIONS_CLASS =
  'flex shrink-0 items-center justify-end gap-[8px] border-t border-[var(--gg-line)] pt-[12px]';

/** Four-column desktop grid contract: one card width/height rhythm for all catalogs. */
export const RESOURCE_GRID_CLASS = 'gg-resource-grid';

/** Format a backend timestamp in the active UI locale, or `-` when empty/invalid. */
export function formatDateTime(value?: string): string {
  return formatClientDateTime(value, '-');
}
