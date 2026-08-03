import { formatClientDateTime } from './timezone';

/**
 * Shared Tailwind class tokens for the enterprise list pages (SOP, 技能, 定时任务,
 * 员工记忆, 对话日志 …). Keeping them in one place avoids copy-pasting the exact
 * same dropdown / select / card styling into every page.
 */

/** Dropdown menu item (icon + label, 12px muted text). */
export const MENU_ITEM_CLASS =
  'cursor-pointer gap-[6px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[var(--gg-slate)] focus:bg-[var(--gg-cloud)] focus:text-[var(--gg-ink)] [&_svg]:size-[14px]';

/** Destructive (red) dropdown menu item. */
export const MENU_ITEM_DANGER_CLASS =
  'cursor-pointer gap-[6px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[#d20b0b] focus:bg-[#fce7e7] focus:text-[#d20b0b] focus:[&_svg]:text-[#d20b0b]! [&_svg]:size-[14px]';

/** Dropdown menu popover container (rounded white card + soft shadow). */
export const MENU_CONTENT_CLASS =
  'flex w-auto min-w-[140px] flex-col gap-[4px] rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-[var(--gg-paper)] p-[4px] shadow-[var(--gg-shadow-card)] ring-0 [--accent:var(--gg-cloud)] [--accent-foreground:var(--gg-ink)]';

/** shadcn `Select` trigger styled to match the 34px filter controls. */
export const SELECT_TRIGGER_CLASS =
  'h-[34px] data-[size=default]:h-[34px] rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-[var(--gg-paper)] text-[12px] text-[var(--gg-ink)] shadow-none data-placeholder:text-[var(--gg-slate)] hover:border-[#bfcbea] focus-visible:border-[var(--gg-cobalt)] focus-visible:ring-2 focus-visible:ring-[color-mix(in_srgb,var(--gg-cobalt)_16%,transparent)]';

/** Mobile (<768px) list card wrapper. */
export const MOBILE_CARD_CLASS =
  'min-w-0 rounded-[var(--gg-radius-card)] border border-[var(--gg-border)] bg-[var(--gg-paper)] p-[14px] shadow-[var(--gg-shadow-card)]';

/** Dialog footer bar — white background, top border, right-aligned actions. */
export const DIALOG_FOOTER_CLASS =
  'flex items-center justify-end gap-[8px] border-t border-[var(--gg-border)] bg-[var(--gg-paper)] px-[24px] py-[12px]';

/** Standard dialog cancel button. */
export const DIALOG_CANCEL_BUTTON_CLASS =
  'h-[36px] min-w-[80px] rounded-[var(--gg-radius-control)] border-[var(--gg-border)] bg-[var(--gg-paper)] px-[14px] text-[14px] font-medium text-[var(--gg-slate)] hover:border-[#bfcbea] hover:bg-[var(--gg-cloud)] hover:text-[var(--gg-ink)]';

/** Standard dialog primary confirm button. */
export const DIALOG_PRIMARY_BUTTON_CLASS =
  'h-[36px] min-w-[80px] rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-[14px] text-[14px] font-semibold text-white shadow-[0_8px_18px_rgba(49,87,232,0.22)] hover:bg-[#244bc7]';

/** Standard outline action button (toolbar refresh, card actions, etc.). */
export const OUTLINE_ACTION_BUTTON_CLASS =
  'h-[34px] gap-[4px] rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-[var(--gg-paper)] px-[20px] text-[12px] font-medium text-[var(--gg-slate)] hover:border-[#bfcbea] hover:bg-[var(--gg-cloud)] hover:text-[var(--gg-cobalt)]';

/** Compact outline action button for inline card headers. */
export const OUTLINE_ACTION_BUTTON_SM_CLASS =
  'h-[32px] gap-[4px] rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-[var(--gg-paper)] px-[12px] text-[12px] font-medium text-[var(--gg-slate)] hover:border-[#bfcbea] hover:bg-[var(--gg-cloud)] hover:text-[var(--gg-cobalt)] [&_svg:not([class*="size-"])]:size-[14px]';

/** Integrated search combo wrapper (input + submit button). */
export const SEARCH_COMBO_CLASS =
  'flex h-[34px] min-w-0 items-stretch overflow-hidden rounded-[var(--gg-radius-control)] border border-[var(--gg-border)] bg-[var(--gg-paper)] transition-colors focus-within:border-[var(--gg-cobalt)] focus-within:ring-2 focus-within:ring-[color-mix(in_srgb,var(--gg-cobalt)_16%,transparent)]';

/** Integrated search combo input field. */
export const SEARCH_COMBO_INPUT_CLASS =
  'min-w-0 flex-1 bg-transparent px-[14px] text-[14px] text-[var(--gg-ink)] outline-none placeholder:text-[var(--gg-slate)]';

/** Integrated search combo submit button. */
export const SEARCH_COMBO_BUTTON_CLASS =
  'shrink-0 bg-[var(--gg-cobalt)] px-[20px] text-[14px] font-semibold text-white transition-colors hover:bg-[#244bc7] disabled:pointer-events-none disabled:opacity-50';

/** Format a backend timestamp in the active UI locale, or `-` when empty/invalid. */
export function formatDateTime(value?: string): string {
  return formatClientDateTime(value, '-');
}
