import { cn } from '@/lib/utils';

/**
 * Tailwind class tokens for the migrated chat conversation page.
 *
 * Palette + metrics are derived from the 共格 Figma (nodes 38:3773 empty state
 * and 38:4962 active conversation): background #fcfcfc, 220px sidebar with a
 * #f4f4f4 right border, ink #18181a, muted #858b9c / #757f9c, hairline borders
 * #e3e7f1 (0.5px), user bubble #f6f6f6, 56px header, and a white composer card
 * with a #18181a 0.5px border, 14px radius and a dark send button.
 */

// ---------------------------------------------------------------------------
// Shared chat chrome (the sidebar itself now lives in the reusable AppSidebar
// component; see AppSidebar.tsx `variant="chat"`).
// ---------------------------------------------------------------------------
export const CHAT_ICON_BUTTON_CLASS =
  'inline-grid size-[30px] shrink-0 place-items-center rounded-[8px] border-0 bg-transparent p-0 text-[#757f9c] transition-colors hover:bg-[#f1f2f5] hover:text-[#18181a]';

// ---------------------------------------------------------------------------
// Main column + header
// ---------------------------------------------------------------------------
export const CHAT_MAIN_CLASS = 'flex min-h-0 min-w-0 flex-col bg-[var(--gg-cloud)]';
export const CHAT_HEADER_CLASS =
  'flex h-[60px] shrink-0 items-center justify-between gap-[12px] border-b border-[var(--gg-border)] bg-white/90 pl-[20px] pr-[24px] backdrop-blur-xl';
export const CHAT_HEADER_TITLE_STACK_CLASS = 'flex min-w-0 items-end gap-[8px]';
export const CHAT_HEADER_TITLE_NAME_CLASS = 'truncate gg-type-body capitalize text-[#18181a]';
export const CHAT_HEADER_TITLE_META_CLASS = 'shrink-0 truncate gg-type-caption text-[#757f9c]';
export const CHAT_HEADER_ACTIONS_CLASS = 'flex shrink-0 items-center gap-[6px]';

// ---------------------------------------------------------------------------
// Message scroller
// ---------------------------------------------------------------------------
export const CHAT_MESSAGES_CLASS = 'min-h-0 flex-1 overflow-y-auto px-[24px] pt-[22px] pb-[62px]';
export const CHAT_MESSAGE_STACK_CLASS = 'mx-auto flex w-full max-w-[820px] flex-col gap-[20px]';

export const CHAT_MESSAGE_ITEM_CLASS = 'flex min-w-0 flex-col';
export const CHAT_QUEUED_MESSAGE_ITEM_CLASS = 'order-last';
export const CHAT_MESSAGE_ROW_BASE_CLASS = 'flex min-w-0';
export const CHAT_MESSAGE_ROW_USER_CLASS = 'justify-end';
export const CHAT_MESSAGE_ROW_ASSISTANT_CLASS = 'justify-start';

export const CHAT_BUBBLE_BASE_CLASS =
  'relative box-border min-w-0 max-w-[min(680px,92%)] gg-type-body  wrap-anywhere text-[#18181a]';
export const CHAT_BUBBLE_ASSISTANT_CLASS =
  'w-full max-w-full rounded-[14px] border border-[var(--gg-border)] bg-white px-[18px] py-[14px] shadow-[0_8px_24px_rgba(24,33,61,0.04)]';
export const CHAT_BUBBLE_USER_CLASS =
  'rounded-[14px] border border-[color-mix(in_srgb,var(--gg-cobalt)_18%,white)] bg-[color-mix(in_srgb,var(--gg-cobalt)_8%,white)] px-[16px] py-[11px] text-[var(--gg-ink)]';
export const CHAT_BUBBLE_ERROR_CLASS = 'border-[#f38989] bg-[#fce7e7] text-[#d20b0b]';
export function chatRowClass(role: 'user' | 'assistant' | 'system' | 'tool'): string {
  return cn(
    CHAT_MESSAGE_ROW_BASE_CLASS,
    role === 'user' ? CHAT_MESSAGE_ROW_USER_CLASS : CHAT_MESSAGE_ROW_ASSISTANT_CLASS,
  );
}

export function chatBubbleClass(role: 'user' | 'assistant' | 'system' | 'tool', isError?: boolean): string {
  return cn(
    CHAT_BUBBLE_BASE_CLASS,
    role === 'user' ? CHAT_BUBBLE_USER_CLASS : CHAT_BUBBLE_ASSISTANT_CLASS,
    isError && CHAT_BUBBLE_ERROR_CLASS,
  );
}

// User plain answer + scheduled chip
export const CHAT_PLAIN_ANSWER_CLASS = 'flex flex-col items-end gap-[6px] whitespace-pre-wrap';
export const CHAT_MESSAGE_MODE_CHIP_CLASS =
  'inline-flex items-center gap-[4px] rounded-full bg-[#eef0f4] px-[9px] py-[2px] gg-type-caption font-medium text-[#464c5e]';
export const CHAT_QUEUED_STATUS_ROW_CLASS = 'mt-[5px] flex justify-end pr-[2px]';
export const CHAT_QUEUED_STATUS_CLASS =
  'inline-flex items-center gap-[4px] gg-type-caption font-medium  text-[#858b9c]';
// ---------------------------------------------------------------------------
// Markdown answer (styled via child selectors, code delegates to CodeBlock)
// ---------------------------------------------------------------------------
export const CHAT_MARKDOWN_CLASS = cn(
  'min-w-0 max-w-full gg-type-body gg-type-markdown text-[#18181a]',
  '[&>*:first-child]:mt-0 [&>*:last-child]:mb-0',
  '[&_p]:my-[8px] [&_p]:wrap-anywhere',
  '[&_h1]:mt-[16px] [&_h1]:mb-[8px] [&_h1]:font-semibold',
  '[&_h2]:mt-[16px] [&_h2]:mb-[8px] [&_h2]:font-semibold',
  '[&_h3]:mt-[14px] [&_h3]:mb-[6px] [&_h3]:font-semibold',
  '[&_h4]:mt-[12px] [&_h4]:mb-[6px] [&_h4]:font-semibold',
  '[&_ul]:my-[8px] [&_ul]:pl-[22px] [&_ul]:list-disc',
  '[&_ol]:my-[8px] [&_ol]:pl-[22px] [&_ol]:list-decimal',
  '[&_li]:my-[3px]',
  '[&_a]:text-[#0b6cf5] [&_a]:underline [&_a]:underline-offset-2',
  '[&_.md-link-label]:text-[#0b6cf5] [&_.md-link-label]:underline [&_.md-link-label]:underline-offset-2',
  '[&_blockquote]:my-[10px] [&_blockquote]:border-l-[3px] [&_blockquote]:border-[#dfe5f2] [&_blockquote]:pl-[12px] [&_blockquote]:text-[#5b6273]',
  '[&_hr]:my-[12px] [&_hr]:border-0 [&_hr]:border-t [&_hr]:border-[#e3e7f1]',
  '[&_strong]:font-semibold',
  '[&_code]:rounded-[5px] [&_code]:bg-[#f1f2f5] [&_code]:px-[5px] [&_code]:py-[1px] [&_code]:font-mono',
  '[&_.code-block-vscode]:my-[10px]',
);
export const CHAT_MD_TABLE_SCROLL_CLASS = 'my-[10px] max-w-full overflow-x-auto';
export const CHAT_MD_TABLE_CLASS =
  'w-full border-collapse gg-type-control [&_th]:border [&_th]:border-[#e3e7f1] [&_th]:bg-[#f7f8fa] [&_th]:px-[10px] [&_th]:py-[6px] [&_th]:font-semibold [&_td]:border [&_td]:border-[#e3e7f1] [&_td]:px-[10px] [&_td]:py-[6px]';

// ---------------------------------------------------------------------------
// Execution record (执行记录 trace panel)
// ---------------------------------------------------------------------------
export const CHAT_TRACE_WRAP_CLASS = 'mb-[10px] min-w-0';
export const CHAT_TRACE_SUMMARY_CLASS =
  'inline-flex cursor-pointer items-center gap-[7px] border-0 bg-transparent p-0 gg-type-control font-semibold  text-[#464c5e] transition-colors hover:text-[#18181a]';
export const CHAT_TRACE_SUMMARY_RUNNING_CLASS = 'text-[#18181a]';
export const CHAT_TRACE_SUMMARY_FAILED_CLASS = 'text-[#d20b0b]';
export const CHAT_TRACE_ICON_CLASS =
  'inline-flex size-[18px] shrink-0 items-center justify-center text-[#858b9c] [&>svg]:block [&>svg]:size-[14px]';
export const CHAT_TRACE_CHEVRON_CLASS = 'transition-transform duration-150';
export const CHAT_TRACE_CHEVRON_EXPANDED_CLASS = 'rotate-90';
export const CHAT_TRACE_DETAILS_CLASS =
  'mt-[8px] grid gap-[8px] border-l-[1.5px] border-[#eef0f4] pl-[14px]';
export const CHAT_TRACE_LINE_CLASS = 'grid grid-cols-[18px_minmax(0,1fr)] items-start gap-[8px]';
export const CHAT_TRACE_LINE_CONTENT_CLASS = 'grid grid-cols-[minmax(0,1fr)] min-w-0 gap-[4px]';
export const CHAT_TRACE_LINE_TEXT_CLASS = 'gg-type-control  text-[#464c5e] wrap-anywhere';
export const CHAT_TRACE_FLOW_TEXT_CLASS = 'gongge-trace-flow-text';
export const CHAT_TRACE_LINE_TEXT_FAILED_CLASS = 'text-[#d20b0b]';
export const CHAT_TRACE_LINE_DETAIL_CLASS = 'gg-type-meta  text-[#858b9c] wrap-anywhere';
export const CHAT_TRACE_CODE_SUMMARY_CLASS =
  'cursor-pointer gg-type-meta font-medium text-[#757f9c] hover:text-[#18181a]';
export const CHAT_TRACE_CODE_DETAILS_CLASS = 'block min-w-0 max-w-full overflow-hidden';
export const CHAT_TRACE_CODE_BLOCK_CLASS =
  'mt-[6px] max-h-[420px] w-full max-w-full overflow-auto overscroll-contain';

// ---------------------------------------------------------------------------
// Citations
// ---------------------------------------------------------------------------
export const CHAT_CITATIONS_CLASS =
  'mt-[12px] grid min-w-0 max-w-full gap-[8px] overflow-hidden border-t border-[#f0f1f4] pt-[10px]';
export const CHAT_CITATION_HEADING_CLASS =
  'inline-flex items-center gap-[6px] gg-type-meta font-semibold text-[#757f9c]';
export const CHAT_CITATION_LIST_CLASS = 'flex min-w-0 max-w-full flex-wrap gap-[6px] overflow-hidden';
export const CHAT_CITATION_CHIP_CLASS =
  'inline-flex min-w-0 max-w-full items-center gap-[6px] overflow-hidden rounded-[8px] border border-[#e3e7f1] bg-[#fafbfc] px-[9px] py-[5px] text-left gg-type-meta text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:bg-white';
export const CHAT_CITATION_INDEX_CLASS = 'shrink-0 font-semibold text-[#18181a]';
export const CHAT_CITATION_TITLE_CLASS = 'min-w-0 truncate';

// ---------------------------------------------------------------------------
// Message attachments (in-bubble)
// ---------------------------------------------------------------------------
export const CHAT_ATTACHMENT_LIST_CLASS = 'mt-[10px] grid gap-[8px]';
export const CHAT_ATTACHMENT_CARD_CLASS =
  'grid min-h-[46px] w-[min(280px,100%)] grid-cols-[36px_minmax(0,1fr)] items-center gap-[10px] rounded-[10px] border border-[#e3e7f1] bg-[#fafbfc] p-[7px]';
export const CHAT_ATTACHMENT_IMG_CLASS = 'size-[36px] rounded-[8px] object-cover';
export const CHAT_ATTACHMENT_FILE_ICON_CLASS =
  'inline-grid size-[36px] place-items-center rounded-[8px] bg-[#eef0f4] text-[#464c5e]';
export const CHAT_ATTACHMENT_COPY_CLASS = 'grid min-w-0 gap-px';
export const CHAT_ATTACHMENT_NAME_CLASS = 'truncate gg-type-meta font-medium text-[#18181a]';
export const CHAT_ATTACHMENT_META_CLASS = 'truncate gg-type-caption text-[#858b9c]';

// ---------------------------------------------------------------------------
// Feedback actions
// ---------------------------------------------------------------------------
export const CHAT_FEEDBACK_CLASS = 'mt-[10px] flex items-center gap-[4px]';
export const CHAT_FEEDBACK_BTN_CLASS =
  'inline-grid size-[28px] place-items-center rounded-[8px] border-0 bg-transparent p-0 text-[#a2a8b8] transition-colors hover:bg-[#f1f2f5] hover:text-[#18181a]';
export const CHAT_FEEDBACK_BTN_ACTIVE_CLASS = 'bg-[#eef0f4] text-[#18181a]';
export const CHAT_FEEDBACK_BTN_DISLIKE_ACTIVE_CLASS = 'bg-[#fce7e7] text-[#d20b0b] hover:bg-[#fce7e7] hover:text-[#d20b0b]';

// ---------------------------------------------------------------------------
// Empty state (Hello {name})
// ---------------------------------------------------------------------------
export const CHAT_EMPTY_CLASS =
  'mx-auto flex min-h-full w-full max-w-[820px] flex-col justify-center gap-[14px] py-[40px]';
export const CHAT_EMPTY_GREETING_CARD_CLASS =
  'grid min-h-[132px] w-full grid-cols-[132px_minmax(0,1fr)] items-end gap-[22px] rounded-[20px] border border-white/80 bg-white/72 px-[20px] shadow-[0_14px_40px_rgba(24,33,61,0.06)] max-[560px]:grid-cols-[88px_minmax(0,1fr)] max-[560px]:gap-[14px] max-[560px]:px-[14px]';
export const CHAT_EMPTY_TITLE_CLASS =
  'wrap-anywhere gg-type-page-title font-semibold text-[var(--gg-ink)]';
export const CHAT_EMPTY_SUBTITLE_CLASS = 'gg-type-card-title font-medium text-[var(--gg-slate)]';
export const CHAT_EMPTY_CARD_CLASS =
  'grid w-full grid-cols-[minmax(0,1.2fr)_minmax(300px,0.8fr)] items-stretch gap-[18px] rounded-[20px] border border-white/80 bg-white/58 p-[16px] shadow-[0_10px_30px_rgba(24,33,61,0.04)] max-[700px]:grid-cols-1';
export const CHAT_EMPTY_ROLE_CLASS = 'line-clamp-2 gg-type-meta  text-[var(--gg-slate)]';
export const CHAT_EMPTY_TAGS_CLASS =
  'flex flex-wrap items-center gap-[8px] gg-type-children-caption [&>span]:rounded-full [&>span]:border [&>span]:border-[var(--gg-border)] [&>span]:bg-white/70 [&>span]:px-[10px] [&>span]:py-[4px] [&>span]:text-[var(--gg-slate)]';
export const CHAT_EMPTY_STAT_CELL_CLASS =
  'flex min-h-[72px] flex-1 flex-col justify-center gap-[6px] border border-[var(--gg-border)] bg-white/65 px-[18px] py-[10px] text-[var(--gg-slate)] first:rounded-l-[14px] last:rounded-r-[14px] [&:not(:first-child)]:ml-[-1px]';

// ---------------------------------------------------------------------------
// Composer
// ---------------------------------------------------------------------------
export const CHAT_INPUT_SHELL_CLASS = 'shrink-0 px-[24px] pb-[20px] pt-[6px]';
export const CHAT_COMPOSER_STAGE_CLASS = 'relative mx-auto w-full max-w-[820px]';
export const CHAT_COMPOSER_AVATAR_CLASS =
  'absolute left-[16px] top-0 z-10 size-[44px] -translate-y-[calc(100%-8px)] shrink-0 overflow-hidden rounded-[10px] border-[3px] border-[var(--gg-cloud)] bg-[var(--gg-cloud)] shadow-[0_8px_18px_rgba(24,33,61,0.14)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-cobalt)] focus-visible:ring-offset-2';
export const CHAT_COMPOSER_FORM_CLASS =
  'relative flex min-w-0 flex-1 flex-col gap-[10px] rounded-[14px] border border-[var(--gg-cobalt)] bg-white p-[12px] shadow-[0_10px_32px_rgba(49,87,232,0.10)] transition-colors';
export const CHAT_COMPOSER_FORM_DRAG_CLASS = 'border-dashed border-[#0b6cf5] bg-[#f5f9ff]';
export const CHAT_COMPOSER_DROP_HINT_CLASS =
  'pointer-events-none absolute inset-0 z-[2] grid place-items-center rounded-[14px] bg-white/85 gg-type-body font-medium text-[#18181a] backdrop-blur-sm';
export const CHAT_COMPOSER_TEXTAREA_CLASS =
  'min-h-[48px] max-h-[200px] w-full resize-none border-0 bg-transparent px-[4px] py-[2px] gg-type-body  text-[#18181a] shadow-none outline-none placeholder:text-[#b3b8c4] focus-visible:ring-0';
export const CHAT_COMPOSER_ATTACHMENTS_CLASS = 'flex flex-wrap gap-[8px]';
export const CHAT_COMPOSER_ATTACHMENT_CHIP_CLASS =
  'inline-flex max-w-[240px] items-center gap-[7px] rounded-[10px] border border-[#e3e7f1] bg-[#fafbfc] py-[5px] pl-[7px] pr-[6px] gg-type-meta text-[#464c5e]';
export const CHAT_COMPOSER_ATTACHMENT_ERROR_CLASS = 'border-[#f38989] bg-[#fce7e7] text-[#d20b0b]';
export const CHAT_COMPOSER_ATTACHMENT_IMG_CLASS = 'size-[24px] rounded-[6px] object-cover';
export const CHAT_COMPOSER_ATTACHMENT_COPY_CLASS = 'grid min-w-0 gap-px';
export const CHAT_COMPOSER_ATTACHMENT_NAME_CLASS = 'truncate gg-type-meta font-medium text-[#18181a]';
export const CHAT_COMPOSER_ATTACHMENT_STATUS_CLASS = 'truncate gg-type-caption text-[#858b9c]';
export const CHAT_COMPOSER_ATTACHMENT_REMOVE_CLASS =
  'inline-grid size-[18px] shrink-0 place-items-center rounded-full border-0 bg-transparent p-0 gg-type-body text-[#a2a8b8] hover:text-[#18181a]';

export const CHAT_COMPOSER_TOOLBAR_CLASS = 'flex items-center justify-between gap-[10px]';
export const CHAT_COMPOSER_CONTEXT_ROW_CLASS = 'flex min-w-0 items-center gap-[8px]';
export const CHAT_COMPOSER_ACTIONS_ROW_CLASS = 'flex shrink-0 items-center gap-[8px]';
export const CHAT_COMPOSER_PLUS_BTN_CLASS =
  'inline-grid size-[32px] place-items-center rounded-[9px] border border-[#e3e7f1] bg-white p-0 text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a] disabled:cursor-not-allowed disabled:opacity-45';
export const CHAT_COMPOSER_INTENT_CHIP_CLASS =
  'inline-flex cursor-pointer items-center gap-[5px] rounded-full border border-[#e3e7f1] bg-[#f4f5f7] py-[4px] pl-[6px] pr-[10px] gg-type-meta font-medium text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#c9d2e4] focus-visible:ring-offset-1';
export const CHAT_COMPOSER_ENGINE_BTN_CLASS =
  'inline-flex h-[32px] max-w-[190px] items-center gap-[5px] rounded-[9px] border border-[#e3e7f1] bg-white px-[10px] gg-type-meta font-medium text-[#757f9c] shadow-none transition-colors hover:border-[#c9d2e4] hover:text-[#18181a] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--gg-cobalt)] focus-visible:ring-offset-1';
export const CHAT_COMPOSER_ENGINE_BTN_ACTIVE_CLASS =
  'border-[color-mix(in_srgb,var(--gg-cobalt)_34%,white)] bg-[#eef3ff] text-[var(--gg-cobalt)] shadow-[0_4px_12px_rgba(49,87,232,0.12)]';
export const CHAT_COMPOSER_HINT_CLASS = 'truncate gg-type-caption text-[#b3b8c4] max-[560px]:hidden';
export const CHAT_COMPOSER_MODEL_BTN_CLASS =
  'inline-flex h-[32px] max-w-[200px] items-center gap-[5px] rounded-[9px] border border-[#e3e7f1] bg-white px-[12px] gg-type-meta font-normal text-[#757f9c] shadow-none transition-colors hover:border-[#c9d2e4] hover:text-[#18181a] disabled:cursor-not-allowed disabled:opacity-45 aria-expanded:border-[#c9d2e4] aria-expanded:text-[#18181a] [&>span:first-child]:min-w-0 [&>span:first-child]:truncate';
export const CHAT_COMPOSER_SEND_BTN_CLASS =
  'inline-grid size-[36px] place-items-center rounded-[10px] bg-[var(--gg-cobalt)] p-0 text-white shadow-[0_8px_18px_rgba(49,87,232,0.24)] transition-colors hover:bg-[#244bc7] disabled:cursor-not-allowed disabled:opacity-40';
export const CHAT_COMPOSER_STOP_BTN_CLASS = 'bg-[#d20b0b] hover:bg-[#b40a0a]';

export const CHAT_MENU_CONTENT_CLASS =
  'flex flex-col gap-[6px] rounded-[14px] border-0 bg-white p-[6px] shadow-[0px_16px_15px_rgba(0,0,0,0.1)] ring-0 [--accent:#F6F6F6] [--accent-foreground:#18181A]';
export const CHAT_MENU_ITEM_CLASS =
  'h-[36px] cursor-pointer gap-[8px] rounded-[10px] px-[12px] gg-type-body text-[#464C5E]';

export const CHAT_MODEL_MENU_ITEM_CLASS =
  'flex h-auto cursor-pointer items-center justify-between gap-[16px] rounded-[10px] px-[12px] py-[8px] gg-type-control text-[#464C5E]';
export const CHAT_MODEL_MENU_COPY_CLASS = 'grid min-w-0 gap-px';
export const CHAT_MODEL_MENU_NAME_CLASS = 'truncate gg-type-control font-medium text-[#18181a]';
export const CHAT_MODEL_MENU_DETAIL_CLASS = 'truncate gg-type-caption text-[#858b9c]';

// ---------------------------------------------------------------------------
// Scheduled task draft card
// ---------------------------------------------------------------------------
export const CHAT_DRAFT_CARD_CLASS =
  'mt-[12px] grid gap-[12px] rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-[#fafbfc] p-[14px]';
export const CHAT_DRAFT_CARD_CREATED_CLASS = 'border-[#96d9b0] bg-[#f2fbf5]';
export const CHAT_DRAFT_HEADER_CLASS = 'flex items-start justify-between gap-[12px]';
export const CHAT_DRAFT_IDENTITY_CLASS = 'flex min-w-0 items-center gap-[10px]';
export const CHAT_DRAFT_ICON_CLASS =
  'inline-grid size-[34px] shrink-0 place-items-center rounded-[10px] bg-[#eef0f4] text-[#464c5e]';
export const CHAT_DRAFT_KICKER_CLASS = 'gg-type-caption font-medium text-[#858b9c]';
export const CHAT_DRAFT_TITLE_CLASS = 'gg-type-body font-semibold text-[#18181a]';
export const CHAT_DRAFT_TOP_ACTIONS_CLASS = 'flex shrink-0 items-center gap-[6px]';
export const CHAT_DRAFT_CREATED_BADGE_CLASS =
  'inline-flex items-center gap-[4px] rounded-full bg-[#e9f7ef] px-[10px] py-[3px] gg-type-meta font-medium text-[#018434]';
export const CHAT_DRAFT_META_GRID_CLASS = 'grid grid-cols-3 gap-[10px] max-[520px]:grid-cols-1';
export const CHAT_DRAFT_META_ITEM_CLASS =
  'grid gap-[3px] rounded-[10px] bg-white px-[10px] py-[8px] gg-type-children-caption gg-type-children-control [&>span]:text-[#858b9c] [&>strong]:font-medium [&>strong]:text-[#18181a]';
export const CHAT_DRAFT_PROMPT_CLASS =
  'grid gap-[4px] gg-type-children-caption gg-type-children-control [&>span]:text-[#858b9c] [&>p]:text-[#464c5e]';
export const CHAT_DRAFT_EDITOR_CLASS =
  'grid grid-cols-2 gap-[10px] max-[520px]:grid-cols-1 gg-type-children-meta [&_label]:grid [&_label]:gap-[5px] [&_label>span]:text-[#757f9c]';
export const CHAT_DRAFT_EDITOR_FULL_CLASS = 'col-span-full';
export const CHAT_DRAFT_FOOTER_CLASS = 'flex justify-end gap-[8px]';

// ---------------------------------------------------------------------------
// Dialogs (handoff inbox + citation detail + rename)
// ---------------------------------------------------------------------------
export const CHAT_HANDOFF_LIST_CLASS = 'grid max-h-[60vh] gap-[14px] overflow-y-auto';
export const CHAT_HANDOFF_CARD_CLASS = 'grid gap-[12px] rounded-[14px] border border-[#e3e7f1] bg-[#fafbfc] p-[16px]';
export const CHAT_HANDOFF_HEAD_CLASS =
  'flex items-center gap-[10px] gg-type-children-body gg-type-children-meta [&_strong]:block [&_strong]:font-semibold [&_strong]:text-[#18181a] [&_span]:text-[#858b9c]';
export const CHAT_HANDOFF_BLOCK_CLASS =
  'grid gap-[4px] gg-type-children-meta gg-type-children-control [&>span]:font-medium [&>span]:text-[#757f9c] [&>p]:text-[#464c5e]';
export const CHAT_HANDOFF_ACTIONS_CLASS = 'flex justify-end gap-[8px]';
export const CHAT_HANDOFF_EMPTY_CLASS = 'py-[36px] text-center gg-type-control text-[#858b9c]';

export const CHAT_CITATION_DETAIL_CLASS = 'grid w-full min-w-0 gap-[14px]';
export const CHAT_CITATION_DETAIL_EYEBROW_CLASS = 'gg-type-meta font-medium text-[#858b9c]';
export const CHAT_CITATION_DETAIL_TITLE_CLASS = 'gg-type-section-title font-semibold text-[#18181a]';
export const CHAT_CITATION_DETAIL_SECTION_CLASS =
  'grid w-full min-w-0 gap-[5px] gg-type-children-meta gg-type-children-control [&>span]:font-medium [&>span]:text-[#757f9c] [&>p]:text-[#464c5e]';
export const CHAT_CITATION_DETAIL_QUOTE_CLASS =
  'm-0 max-h-[min(52vh,520px)] overflow-y-auto overscroll-contain rounded-[10px] border-l-[3px] border-[#e3e7f1] bg-[#fafbfc] px-[12px] py-[8px] gg-type-control  whitespace-pre-wrap wrap-anywhere text-[#464c5e]';
export const CHAT_CITATION_DETAIL_MARKDOWN_CLASS =
  'max-h-[min(52vh,520px)] w-full min-w-0 max-w-full overflow-y-auto overscroll-contain rounded-[10px] border-l-[3px] border-[#e3e7f1] bg-[#fafbfc] px-[12px] py-[8px] gg-type-citation-markdown [&>div]:w-full [&>div]:min-w-0 [&>div]:max-w-none [&>div]:text-[#464c5e] [&_p]:wrap-anywhere [&_li]:wrap-anywhere [&_code]:wrap-anywhere [&_a]:wrap-anywhere';
export const CHAT_CITATION_DETAIL_GRID_CLASS =
  'grid grid-cols-2 gap-[12px] max-[520px]:grid-cols-1 gg-type-children-caption gg-type-children-control [&>div]:grid [&>div]:gap-[3px] [&_span]:text-[#858b9c] [&_strong]:font-medium [&_strong]:text-[#18181a]';
export const CHAT_CITATION_DETAIL_NOTE_CLASS = 'gg-type-meta  text-[#858b9c]';

export const CHAT_DEBUG_PANEL_CLASS =
  'mx-auto mt-[16px] max-w-[820px] overflow-auto rounded-[10px] bg-[#1e1e1e] p-[12px] gg-type-meta text-[#d4d4d4]';
