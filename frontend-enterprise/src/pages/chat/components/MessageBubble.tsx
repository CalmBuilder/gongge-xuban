import ProductIcon from '@/components/ProductIcon';
import { notify } from '@/components/ui/app-toast';
import IconThumbUp from '@/assets/icons/thumb-up.svg?react';
import IconThumbDown from '@/assets/icons/thumb-down.svg?react';
import { cn } from '@/lib/utils';
import { useI18n } from '@/i18n';
import type {
  ChatAttachmentRead,
  ChatMessage,
  KnowledgeCitation,
  ScheduledTaskDraftRead,
  ScheduledTaskRead,
} from '@/types';

import {
  CHAT_ATTACHMENT_CARD_CLASS,
  CHAT_ATTACHMENT_COPY_CLASS,
  CHAT_ATTACHMENT_FILE_ICON_CLASS,
  CHAT_ATTACHMENT_IMG_CLASS,
  CHAT_ATTACHMENT_LIST_CLASS,
  CHAT_ATTACHMENT_META_CLASS,
  CHAT_ATTACHMENT_NAME_CLASS,
  CHAT_CITATION_CHIP_CLASS,
  CHAT_CITATION_HEADING_CLASS,
  CHAT_CITATION_INDEX_CLASS,
  CHAT_CITATION_LIST_CLASS,
  CHAT_CITATION_TITLE_CLASS,
  CHAT_CITATIONS_CLASS,
  CHAT_FEEDBACK_BTN_ACTIVE_CLASS,
  CHAT_FEEDBACK_BTN_CLASS,
  CHAT_FEEDBACK_BTN_DISLIKE_ACTIVE_CLASS,
  CHAT_MESSAGE_ACTIONS_CLASS,
  CHAT_MESSAGE_ACTIONS_ASSISTANT_CLASS,
  CHAT_MESSAGE_ACTIONS_USER_CLASS,
  CHAT_MESSAGE_ACTION_BTN_CLASS,
  CHAT_MESSAGE_ITEM_CLASS,
  CHAT_MESSAGE_MODE_CHIP_CLASS,
  CHAT_PLAIN_ANSWER_CLASS,
  CHAT_QUEUED_MESSAGE_ITEM_CLASS,
  CHAT_QUEUED_STATUS_CLASS,
  CHAT_QUEUED_STATUS_ROW_CLASS,
  chatBubbleClass,
  chatRowClass,
} from '../chatPageStyles';
import {
  MarkdownMessage,
  attachmentTypeLabel,
  canRateMessage,
  citationDisplayTitle,
} from '../chatHelpers';
import type { TraceLine } from '../chatTypes';
import type { UseChatSession } from '../useChatSession';
import ExecutionRecord from './ExecutionRecord';
import DynamicExecutionControl from './DynamicExecutionControl';
import ScheduledDraftCard from './ScheduledDraftCard';

export type MessageRender = {
  traceTurnId: string;
  summary: { text: string; state: TraceLine['state'] } | null;
  details: TraceLine[];
  expanded: boolean;
  showInlineTrace: boolean;
  visibleContent: string;
  citations: KnowledgeCitation[];
  scheduledDraft: ScheduledTaskDraftRead | null;
  createdTask?: ScheduledTaskRead;
  scheduledTaskPrompt: boolean;
  attachments: ChatAttachmentRead[];
  statusOnly: boolean;
  canEdit: boolean;
  canRetry: boolean;
};

type MessageBubbleProps = {
  chat: UseChatSession;
  item: ChatMessage;
  render: MessageRender;
};

export default function MessageBubble({ chat, item, render }: MessageBubbleProps) {
  const { t } = useI18n();
  const {
    toggleTrace,
    rateMessage,
    setActiveCitation,
    confirmScheduledTask,
    dismissScheduledTaskDraft,
    editMessage,
    retryMessage,
    currentSessionRunning,
  } = chat;
  const {
    traceTurnId,
    summary,
    details,
    expanded,
    showInlineTrace,
    visibleContent,
    citations,
    scheduledDraft,
    createdTask,
    scheduledTaskPrompt,
    attachments,
    statusOnly,
    canEdit,
    canRetry,
  } = render;
  const queuedMessage = item.role === 'user' && item.metadata?.queued === true;
  const canCopy = (
    item.role === 'user' || item.role === 'assistant'
  ) && !item.isStreaming && !statusOnly && Boolean(visibleContent);
  const showEdit = canCopy && item.role === 'user' && canEdit && !queuedMessage && !currentSessionRunning;
  const showRetry = canCopy && item.role === 'assistant' && canRetry && !currentSessionRunning;
  const showFeedback = canRateMessage(item);

  async function copyVisibleMessage() {
    /** 只复制页面实际展示的已完成消息，不复制执行轨迹或隐藏 metadata。 */

    try {
      await copyTextToClipboard(visibleContent);
      notify.success(t(item.role === 'user' ? '消息已复制' : '回答已复制'));
    } catch {
      notify.error(t('复制失败，请手动选择文本'));
    }
  }

  return (
    <div className={cn(CHAT_MESSAGE_ITEM_CLASS, queuedMessage && CHAT_QUEUED_MESSAGE_ITEM_CLASS)}>
      <div className={chatRowClass(item.role)}>
        <div
          className={chatBubbleClass(item.role, item.isError)}
        >
          {statusOnly ? (
            <div className="gg-type-control text-[#858b9c]">{visibleContent}</div>
          ) : showInlineTrace && summary ? (
            <ExecutionRecord
              traceTurnId={traceTurnId}
              summary={summary}
              details={details}
              expanded={expanded}
              onToggle={toggleTrace}
            />
          ) : null}

          {!statusOnly && visibleContent ? (
            item.role === 'assistant' ? (
              <div data-i18n-ignore>
                <MarkdownMessage content={visibleContent} />
              </div>
            ) : (
              <div className={CHAT_PLAIN_ANSWER_CLASS}>
                {scheduledTaskPrompt && (
                  <span className={CHAT_MESSAGE_MODE_CHIP_CLASS}>
                    <ProductIcon name="clock" size={13} />
                    定时任务
                  </span>
                )}
                <span data-i18n-ignore>{visibleContent}</span>
              </div>
            )
          ) : null}

          {typeof item.metadata?.execution_id === 'string' ? (
            <DynamicExecutionControl executionId={item.metadata.execution_id} />
          ) : null}

          {!statusOnly && attachments.length > 0 && (
            <div className={CHAT_ATTACHMENT_LIST_CLASS}>
              {attachments.map((attachment) => (
                <div className={CHAT_ATTACHMENT_CARD_CLASS} key={attachment.id}>
                  {attachment.kind === 'image' && attachment.data_url ? (
                    <img className={CHAT_ATTACHMENT_IMG_CLASS} src={attachment.data_url} alt={attachment.filename} />
                  ) : (
                    <span className={CHAT_ATTACHMENT_FILE_ICON_CLASS}>
                      <ProductIcon name={attachment.kind === 'pdf' ? 'file' : 'folder'} size={18} />
                    </span>
                  )}
                  <span className={CHAT_ATTACHMENT_COPY_CLASS}>
                    <span className={CHAT_ATTACHMENT_NAME_CLASS} data-i18n-ignore>{attachment.filename}</span>
                    <span className={CHAT_ATTACHMENT_META_CLASS} data-i18n-ignore>
                      {attachmentTypeLabel(attachment)}
                      {attachment.error ? ` · ${attachment.error}` : ''}
                    </span>
                  </span>
                </div>
              ))}
            </div>
          )}

          {item.role === 'assistant' && citations.length > 0 && (
            <div className={CHAT_CITATIONS_CLASS} aria-label="知识引用">
              <div className={CHAT_CITATION_HEADING_CLASS}>
                <ProductIcon name="file" size={14} />
                <span>知识来源</span>
              </div>
              <div className={CHAT_CITATION_LIST_CLASS}>
                {citations.map((citation) => (
                  <button
                    key={citation.id}
                    type="button"
                    className={CHAT_CITATION_CHIP_CLASS}
                    onClick={() => setActiveCitation(citation)}
                  >
                    <span className={CHAT_CITATION_INDEX_CLASS} data-i18n-ignore>{citation.label || citation.id}</span>
                    <span className={CHAT_CITATION_TITLE_CLASS} data-i18n-ignore>{citationDisplayTitle(citation)}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {scheduledDraft && (
            <ScheduledDraftCard
              draft={scheduledDraft}
              createdTask={createdTask}
              onConfirm={(nextDraft) => void confirmScheduledTask(nextDraft, item.id)}
              onDismiss={() => dismissScheduledTaskDraft(item.id)}
            />
          )}

        </div>
      </div>
      {(canCopy || showEdit || showRetry || showFeedback) && (
        <div className={cn(
          CHAT_MESSAGE_ACTIONS_CLASS,
          item.role === 'user' ? CHAT_MESSAGE_ACTIONS_USER_CLASS : CHAT_MESSAGE_ACTIONS_ASSISTANT_CLASS,
        )}>
          {canCopy && (
            <button
              type="button"
              className={CHAT_MESSAGE_ACTION_BTN_CLASS}
              aria-label={t('复制')}
              title={t('复制')}
              onClick={() => void copyVisibleMessage()}
            >
              <ProductIcon name="copy" size={15} />
            </button>
          )}
          {showEdit && (
            <button
              type="button"
              className={CHAT_MESSAGE_ACTION_BTN_CLASS}
              aria-label={t('编辑')}
              title={t('编辑')}
              onClick={() => editMessage(item)}
            >
              <ProductIcon name="edit" size={15} />
            </button>
          )}
          {showFeedback && (
            <>
              <button
                type="button"
                className={cn(CHAT_FEEDBACK_BTN_CLASS, item.feedback_rating === 'up' && CHAT_FEEDBACK_BTN_ACTIVE_CLASS)}
                aria-label={t('点赞')}
                title={t('点赞')}
                onClick={() => rateMessage(item, 'up')}
              >
                <IconThumbUp width={15} height={15} />
              </button>
              <button
                type="button"
                className={cn(
                  CHAT_FEEDBACK_BTN_CLASS,
                  item.feedback_rating === 'down' && CHAT_FEEDBACK_BTN_DISLIKE_ACTIVE_CLASS,
                )}
                aria-label={t('点踩')}
                title={t('点踩')}
                onClick={() => rateMessage(item, 'down')}
              >
                <IconThumbDown width={15} height={15} />
              </button>
            </>
          )}
          {showRetry && (
            <button
              type="button"
              className={CHAT_MESSAGE_ACTION_BTN_CLASS}
              aria-label={t('重试')}
              title={t('重试')}
              onClick={() => void retryMessage(item)}
            >
              <ProductIcon name="refresh" size={15} />
            </button>
          )}
        </div>
      )}
      {queuedMessage && (
        <div className={CHAT_QUEUED_STATUS_ROW_CLASS}>
          <span className={CHAT_QUEUED_STATUS_CLASS} role="status">
            <ProductIcon name="clock" size={12} />
            排队中
          </span>
        </div>
      )}
    </div>
  );
}

async function copyTextToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // 浏览器拒绝 Clipboard API 时继续尝试兼容路径。
    }
  }

  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', 'true');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  let copied = false;
  try {
    copied = document.execCommand('copy');
  } finally {
    textarea.remove();
  }
  if (!copied) throw new Error('Clipboard copy failed');
}
