import type { FormEvent } from 'react';
import { useEffect, useRef, useState } from 'react';

import EmployeeAvatar from '@/components/EmployeeAvatar';
import ProductIcon from '@/components/ProductIcon';
import IconAdd from '@/assets/icons/add.svg?react';
import { notify } from '@/components/ui/app-toast';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { Button as UIButton } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { HoverCard, HoverCardContent, HoverCardTrigger } from '@/components/ui/hover-card';
import { employeeDisplayName } from '@/employee';
import { cn } from '@/lib/utils';
import { useI18n } from '@/i18n';

import {
  CHAT_COMPOSER_ACTIONS_ROW_CLASS,
  CHAT_COMPOSER_ATTACHMENT_CHIP_CLASS,
  CHAT_COMPOSER_ATTACHMENT_COPY_CLASS,
  CHAT_COMPOSER_ATTACHMENT_ERROR_CLASS,
  CHAT_COMPOSER_ATTACHMENT_IMG_CLASS,
  CHAT_COMPOSER_ATTACHMENT_NAME_CLASS,
  CHAT_COMPOSER_ATTACHMENT_REMOVE_CLASS,
  CHAT_COMPOSER_ATTACHMENT_STATUS_CLASS,
  CHAT_COMPOSER_ATTACHMENTS_CLASS,
  CHAT_COMPOSER_AVATAR_CLASS,
  CHAT_COMPOSER_CONTEXT_ROW_CLASS,
  CHAT_COMPOSER_DROP_HINT_CLASS,
  CHAT_COMPOSER_ENGINE_BTN_ACTIVE_CLASS,
  CHAT_COMPOSER_ENGINE_BTN_CLASS,
  CHAT_COMPOSER_FORM_CLASS,
  CHAT_COMPOSER_FORM_DRAG_CLASS,
  CHAT_COMPOSER_HINT_CLASS,
  CHAT_COMPOSER_INTENT_CHIP_CLASS,
  CHAT_COMPOSER_MODEL_BTN_CLASS,
  CHAT_COMPOSER_NEW_SESSION_BTN_CLASS,
  CHAT_COMPOSER_PLUS_BTN_CLASS,
  CHAT_COMPOSER_SEND_BTN_CLASS,
  CHAT_COMPOSER_STAGE_CLASS,
  CHAT_COMPOSER_STOP_BTN_CLASS,
  CHAT_COMPOSER_TEXTAREA_CLASS,
  CHAT_INPUT_SHELL_CLASS,
  CHAT_MENU_CONTENT_CLASS,
  CHAT_MENU_ITEM_CLASS,
  CHAT_MODEL_MENU_COPY_CLASS,
  CHAT_MODEL_MENU_DETAIL_CLASS,
  CHAT_MODEL_MENU_ITEM_CLASS,
  CHAT_MODEL_MENU_NAME_CLASS,
} from '../chatPageStyles';
import { attachmentTypeLabel, modelDetailText, modelDisplayName } from '../chatHelpers';
import type { UseChatSession } from '../useChatSession';
import type { GeneralSkillInstallIntentRead } from '../chatTypes';

export default function Composer({ chat }: { chat: UseChatSession }) {
  const { t } = useI18n();
  const {
    input,
    setInput,
    composerAttachments,
    composerDragActive,
    composerPlusOpen,
    setComposerPlusOpen,
    composerIntent,
    setComposerIntent,
    executionEngine,
    setExecutionEngine,
    sessionGeneralSkills,
    generalSkillCatalogLoading,
    generalSkillCatalogError,
    selectedGeneralSkillIds,
    selectSessionGeneralSkill,
    clearSelectedGeneralSkill,
    generalSkillInstallOpen,
    setGeneralSkillInstallOpen,
    generalSkillInstallIntents,
    createGeneralSkillInstallIntent,
    resolveGeneralSkillInstallIntent,
    readyComposerAttachments,
    uploadingComposerAttachment,
    currentSessionRunning,
    composerActive,
    showComposerAvatar,
    displayedProfile,
    displayedAgent,
    startNewSession,
    emptyRoleSummary,
    emptyProfileTags,
    emptyStats,
    activeConversationId,
    enabledModelConfigs,
    selectedModelConfig,
    modelConfigsLoading,
    modelConfigsLoadError,
    changeModelConfig,
    showModelSetupNotice,
    modelSetupNoticeText,
    canConfigureModels,
    setModelSetupOpen,
    isComposing,
    setIsComposing,
    fileInputRef,
    send,
    abortStream,
    handleComposerPaste,
    handleComposerFileChange,
    handleComposerDragEnter,
    handleComposerDragOver,
    handleComposerDragLeave,
    handleComposerDrop,
    removeComposerAttachment,
    handleComposerPlusAction,
  } = chat;

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [scheduleIntentHovered, setScheduleIntentHovered] = useState(false);
  const [previewAttachment, setPreviewAttachment] = useState<UseChatSession['composerAttachments'][number] | null>(null);

  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = 'auto';
    element.style.height = `${Math.min(element.scrollHeight, 200)}px`;
  }, [input]);

  useEffect(() => {
    if (composerIntent !== 'scheduled_task') {
      setScheduleIntentHovered(false);
    }
  }, [composerIntent]);

  const hasSendContent = Boolean(input.trim() || readyComposerAttachments.length > 0);
  const sendDisabled = (
    !hasSendContent
    || uploadingComposerAttachment
    || modelConfigsLoading
    || Boolean(modelConfigsLoadError)
    || !selectedModelConfig
    || !activeConversationId
  );
  const selectedGeneralSkills = sessionGeneralSkills.filter(
    (item) => selectedGeneralSkillIds.includes(item.skill_id),
  );

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void send();
  };

  const handleNewSession = () => {
    setPreviewAttachment(null);
    startNewSession();
  };

  return (
    <div className={CHAT_INPUT_SHELL_CLASS}>
      <div className={CHAT_COMPOSER_STAGE_CLASS}>
        {showModelSetupNotice && (
          <div className="mb-[10px] flex flex-col items-start justify-between gap-[10px] rounded-[12px] border border-[#f3d28b] bg-[#fff8e8] px-[14px] py-[10px] text-[#6f4500] shadow-[0_8px_24px_rgba(92,62,0,0.08)] sm:flex-row sm:items-center">
            <div className="flex min-w-0 items-center gap-[9px]">
              <span className="flex size-[26px] shrink-0 items-center justify-center rounded-[8px] bg-[#ffe7ad] text-[#8a4b00]">
                <ProductIcon name="model" size={14} />
              </span>
              <span className="min-w-0 gg-type-meta ">{modelSetupNoticeText}</span>
            </div>
            {canConfigureModels && (
              <button
                type="button"
                onClick={() => setModelSetupOpen(true)}
                className="h-[30px] shrink-0 rounded-[var(--gg-radius-control)] bg-[var(--gg-cobalt)] px-[12px] gg-type-meta font-semibold text-white transition-colors hover:bg-[#244bc7]"
              >
                {t('配置模型')}
              </button>
            )}
          </div>
        )}
        {showComposerAvatar && displayedProfile && (
          <HoverCard openDelay={80} closeDelay={80}>
            <HoverCardTrigger asChild>
              <button
                type="button"
                aria-label="员工信息"
                className={cn(CHAT_COMPOSER_AVATAR_CLASS, 'block cursor-pointer outline-none')}
              >
                <EmployeeAvatar profile={displayedProfile} size={44} className="size-full" />
              </button>
            </HoverCardTrigger>
            <HoverCardContent
              side="left"
              align="end"
              sideOffset={10}
              className="flex w-[220px] flex-col items-start gap-[8px] rounded-[20px] border-0 bg-white p-0 py-[4px] shadow-[0px_16px_15px_rgba(0,0,0,0.1)] ring-0"
            >
              <div className="flex w-full flex-col px-[6px]">
                <div className="flex h-[46px] w-full flex-col items-center justify-end rounded-[14px] bg-[#f6f6f6] pb-[4px] pl-[8px] pr-[16px] pt-[8px]">
                  <div className="flex w-full items-end justify-between gap-[8px]">
                    <div className="flex min-w-0 items-end gap-[4px]">
                      <EmployeeAvatar
                        profile={displayedProfile}
                        agent={displayedAgent ?? undefined}
                        width={60}
                        height={60}
                        radius={30}
                        objectPosition="bottom"
                      />
                      <div className="flex min-w-0 h-[36px] flex-col justify-center gap-[2px] whitespace-nowrap pb-[2px] gg-type-caption capitalize ">
                        <p className="truncate gg-type-meta font-medium text-[#464c5e]">
                          {displayedAgent ? employeeDisplayName(displayedAgent) : displayedProfile.roleName}
                        </p>
                        <p className="truncate gg-type-meta text-[#757f9c]">{displayedProfile.roleName}</p>
                      </div>
                    </div>
                    <button
                      type="button"
                      aria-label={t('新建会话')}
                      title={t('新建会话')}
                      className={CHAT_COMPOSER_NEW_SESSION_BTN_CLASS}
                      onClick={handleNewSession}
                    >
                      <IconAdd className="size-[14px]!" />
                    </button>
                  </div>
                </div>
              </div>

              <div className="flex w-full flex-col gap-[8px] px-[8px]">
                <p className="w-full gg-type-caption capitalize  text-[#757f9c]">
                  {emptyRoleSummary}
                </p>
                {emptyProfileTags.length > 0 && (
                  <div className="flex w-full flex-wrap content-center items-center gap-[4px]">
                    {emptyProfileTags.map((tag, index) => (
                      <div
                        key={`${tag}-${index}`}
                        className="flex h-[16px] items-center justify-center rounded-[10px] border-[0.5px] border-[#e3e7f1] px-[8px] py-[2px]"
                      >
                        <span className="whitespace-nowrap gg-type-caption capitalize  text-[#757f9c]">
                          {tag}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="flex w-full flex-col px-[8px] pb-[8px]">
                <div className="flex w-full items-start whitespace-nowrap capitalize ">
                  {emptyStats.map((item, index) => (
                    <div
                      key={item.label}
                      className={cn(
                        'flex flex-1 flex-col justify-center gap-[4px] border-[0.5px] border-[#e3e7f1] px-[12px] py-[6px]',
                        index === 0 && 'rounded-l-[14px]',
                        index === emptyStats.length - 1 && 'rounded-r-[14px]',
                      )}
                    >
                      <p className="gg-type-card-title font-medium text-[#18181a]">{item.value}</p>
                      <p className="gg-type-caption text-[#464c5e]">{item.label}</p>
                    </div>
                  ))}
                </div>
              </div>
            </HoverCardContent>
          </HoverCard>
        )}
        <form
          className={cn(CHAT_COMPOSER_FORM_CLASS, composerDragActive && CHAT_COMPOSER_FORM_DRAG_CLASS)}
          onDragEnter={handleComposerDragEnter}
          onDragOver={handleComposerDragOver}
          onDragLeave={handleComposerDragLeave}
          onDrop={handleComposerDrop}
          onSubmit={handleSubmit}
        >
          <input
            ref={fileInputRef}
            className="hidden"
            type="file"
            multiple
            onChange={handleComposerFileChange}
          />
          {composerDragActive && <div className={CHAT_COMPOSER_DROP_HINT_CLASS}>松开上传文件</div>}

          {composerAttachments.length > 0 && (
            <div className={CHAT_COMPOSER_ATTACHMENTS_CLASS}>
              {composerAttachments.map((attachment) => (
                <div
                  className={cn(
                    CHAT_COMPOSER_ATTACHMENT_CHIP_CLASS,
                    attachment.uploadStatus === 'error' && CHAT_COMPOSER_ATTACHMENT_ERROR_CLASS,
                  )}
                  key={attachment.uploadKey}
                >
                  {attachment.kind === 'image' && attachment.data_url ? (
                    <img className={CHAT_COMPOSER_ATTACHMENT_IMG_CLASS} src={attachment.data_url} alt={attachment.filename} />
                  ) : (
                    <ProductIcon name={attachment.kind === 'pdf' ? 'file' : 'folder'} size={16} />
                  )}
                  <span className={CHAT_COMPOSER_ATTACHMENT_COPY_CLASS}>
                    <span className={CHAT_COMPOSER_ATTACHMENT_NAME_CLASS}>{attachment.filename}</span>
                    <span className={CHAT_COMPOSER_ATTACHMENT_STATUS_CLASS}>
                      {attachment.uploadStatus === 'uploading' && '解析中'}
                      {attachment.uploadStatus === 'ready' && `${attachmentTypeLabel(attachment)} · 可分析`}
                      {attachment.uploadStatus === 'error' && (attachment.error || '上传失败')}
                    </span>
                  </span>
                  {attachment.uploadStatus === 'ready' && attachment.preview && (
                    <button
                      type="button"
                      className="shrink-0 rounded-md px-1.5 py-1 gg-type-caption font-medium text-[var(--gg-cobalt)] hover:bg-[#edf2ff]"
                      onClick={() => setPreviewAttachment(attachment)}
                    >
                      查看解析内容
                    </button>
                  )}
                  <button
                    type="button"
                    className={CHAT_COMPOSER_ATTACHMENT_REMOVE_CLASS}
                    onClick={() => removeComposerAttachment(attachment.uploadKey)}
                    aria-label="移除附件"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}

          <Dialog open={Boolean(previewAttachment)} onOpenChange={(open) => !open && setPreviewAttachment(null)}>
            <DialogContent className="max-w-[720px] rounded-[20px] p-0">
              <div className="border-b border-[#e8ebf2] px-6 py-5">
                <DialogTitle className="gg-type-card-title font-semibold text-[#18181a]">
                  {previewAttachment?.filename || '附件解析内容'}
                </DialogTitle>
                <p className="mt-1 gg-type-caption text-[#757f9c]">内容来自服务端固定解析版本，仅作为不可信数据读取。</p>
              </div>
              <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-words px-6 py-5 gg-type-code text-[#303746]" data-i18n-ignore>
                {previewAttachment?.preview || '暂无可预览内容'}
              </pre>
            </DialogContent>
          </Dialog>

          <textarea
            ref={textareaRef}
            className={CHAT_COMPOSER_TEXTAREA_CLASS}
            data-chat-composer-input
            aria-label={t('输入消息，按 Enter 发送...')}
            value={input}
            rows={2}
            placeholder={t('输入消息，按 Enter 发送...')}
            onChange={(event) => setInput(event.target.value)}
            onPaste={handleComposerPaste}
            onCompositionStart={() => setIsComposing(true)}
            onCompositionEnd={() => window.setTimeout(() => setIsComposing(false), 0)}
            onKeyDown={(event) => {
              const nativeEvent = event.nativeEvent as KeyboardEvent & { isComposing?: boolean };
              if (
                event.key === 'Enter'
                && !event.shiftKey
                && !isComposing
                && !nativeEvent.isComposing
                && nativeEvent.keyCode !== 229
              ) {
                event.preventDefault();
                void send();
              }
            }}
          />

          <div className={cn('flex items-center justify-between gap-[10px]', !composerActive && 'opacity-95')}>
            <div className={CHAT_COMPOSER_CONTEXT_ROW_CLASS}>
              <DropdownMenu open={composerPlusOpen} onOpenChange={setComposerPlusOpen}>
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    className={CHAT_COMPOSER_PLUS_BTN_CLASS}
                    aria-label="添加"
                    title="添加"
                  >
                    <ProductIcon name="plus" size={16} />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" side="top" className={cn(CHAT_MENU_CONTENT_CLASS, 'min-w-[160px]')}>
                  <DropdownMenuItem className={CHAT_MENU_ITEM_CLASS} onSelect={() => handleComposerPlusAction('upload')}>
                    <ProductIcon name="upload" size={16} />
                    <span>上传文件</span>
                  </DropdownMenuItem>
                  <DropdownMenuItem className={CHAT_MENU_ITEM_CLASS} onSelect={() => handleComposerPlusAction('scheduled_task')}>
                    <ProductIcon name="clock" size={16} />
                    <span>定时任务</span>
                  </DropdownMenuItem>
                  <DropdownMenuItem className={CHAT_MENU_ITEM_CLASS} onSelect={() => setGeneralSkillInstallOpen(true)}>
                    <ProductIcon name="spark" size={16} />
                    <span>安装 Skill</span>
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              {composerIntent === 'scheduled_task' && (
                <button
                  type="button"
                  className={CHAT_COMPOSER_INTENT_CHIP_CLASS}
                  onMouseEnter={() => setScheduleIntentHovered(true)}
                  onMouseLeave={() => setScheduleIntentHovered(false)}
                  onFocus={() => setScheduleIntentHovered(true)}
                  onBlur={() => setScheduleIntentHovered(false)}
                  onClick={() => setComposerIntent(null)}
                  aria-label="取消定时任务"
                  title="取消定时任务"
                >
                  <span className={cn(
                    'relative inline-grid size-[16px] shrink-0 place-items-center rounded-full transition-colors',
                    scheduleIntentHovered ? 'text-[#18181a]' : 'text-[#858b9c]',
                  )}
                  >
                    <ProductIcon
                      name="clock"
                      size={14}
                      className={cn('transition-opacity', scheduleIntentHovered ? 'opacity-0' : 'opacity-100')}
                    />
                    <ProductIcon
                      name="close"
                      size={9}
                      className={cn('absolute transition-opacity', scheduleIntentHovered ? 'opacity-100' : 'opacity-0')}
                      style={{ width: 9, height: 9 }}
                    />
                  </span>
                  <span>定时任务</span>
                </button>
              )}
              {(
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <button
                      type="button"
                      className={CHAT_COMPOSER_MODEL_BTN_CLASS}
                      aria-label={t('选择本轮 Skill')}
                    >
                      <ProductIcon name="spark" size={14} />
                      <span>{selectedGeneralSkills.length
                        ? t('已选 {1} 个 Skill', { 1: selectedGeneralSkills.length })
                        : sessionGeneralSkills.length ? t('使用 Skill') : t('添加 Skill')}</span>
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent
                    align="start"
                    side="top"
                    className={cn(CHAT_MENU_CONTENT_CLASS, 'max-h-[320px] min-w-[260px] overflow-y-auto')}
                  >
                    {generalSkillCatalogLoading && <DropdownMenuItem disabled>正在加载 Skill…</DropdownMenuItem>}
                    {generalSkillCatalogError && <DropdownMenuItem disabled>Skill 加载失败，请刷新重试</DropdownMenuItem>}
                    {!generalSkillCatalogLoading && !generalSkillCatalogError && !sessionGeneralSkills.length && (
                      <DropdownMenuItem disabled>当前分身还没有 Skill</DropdownMenuItem>
                    )}
                    {sessionGeneralSkills.map((item) => (
                      <DropdownMenuItem
                        key={item.skill_id}
                        className={CHAT_MODEL_MENU_ITEM_CLASS}
                        onSelect={() => void selectSessionGeneralSkill(item.skill_id)}
                      >
                        <span className={CHAT_MODEL_MENU_COPY_CLASS}>
                          <span className={CHAT_MODEL_MENU_NAME_CLASS}>
                            {item.name}{!item.enabled ? t('（已静音）') : ''}
                          </span>
                          <span className={CHAT_MODEL_MENU_DETAIL_CLASS}>
                            v{item.revision_number} · {item.invocation_policy === 'user_only' ? t('仅手动') : t('可自动')}
                          </span>
                        </span>
                        {selectedGeneralSkillIds.includes(item.skill_id) && <ProductIcon name="check" size={15} />}
                      </DropdownMenuItem>
                    ))}
                    <DropdownMenuItem className={CHAT_MENU_ITEM_CLASS} onSelect={() => setGeneralSkillInstallOpen(true)}>
                      <ProductIcon name="plus" size={15} />
                      <span>安装 Skill 到当前分身</span>
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
              <button
                type="button"
                className={cn(
                  CHAT_COMPOSER_ENGINE_BTN_CLASS,
                  executionEngine === 'dynamic_task' && CHAT_COMPOSER_ENGINE_BTN_ACTIVE_CLASS,
                )}
                aria-pressed={executionEngine === 'dynamic_task'}
                aria-label={t('选择 DynamicTaskAgent 复杂任务引擎')}
                title={t('选中后，当前没有正在执行的 SOP 时，本轮默认使用 DynamicTaskAgent；活动 SOP 会继续原流程')}
                onClick={() => setExecutionEngine(executionEngine === 'dynamic_task' ? 'auto' : 'dynamic_task')}
              >
                <ProductIcon name="tool" size={14} />
                <span className="truncate">DynamicTaskAgent</span>
                {executionEngine === 'dynamic_task' && <ProductIcon name="check" size={13} />}
              </button>
              {selectedGeneralSkills.map((selectedSkill) => (
                <button
                  key={selectedSkill.skill_id}
                  type="button"
                  className={CHAT_COMPOSER_INTENT_CHIP_CLASS}
                  onClick={() => clearSelectedGeneralSkill(selectedSkill.skill_id)}
                  aria-label={t('取消本轮 Skill {1}', { 1: selectedSkill.name })}
                  title={t('只取消本轮选择')}
                >
                  <ProductIcon name="close" size={10} />
                  <span>{selectedSkill.name}</span>
                </button>
              ))}
              <div className={CHAT_COMPOSER_HINT_CLASS}>Enter 发送 / Shift+Enter 换行</div>
            </div>
            <div className={CHAT_COMPOSER_ACTIONS_ROW_CLASS}>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    className={CHAT_COMPOSER_MODEL_BTN_CLASS}
                    disabled={!enabledModelConfigs.length}
                  >
                    <span>{selectedModelConfig ? modelDisplayName(selectedModelConfig) : '默认模型'}</span>
                    <ProductIcon name="arrow" size={14} style={{ transform: 'rotate(90deg)' }} />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" side="top" className={cn(CHAT_MENU_CONTENT_CLASS, 'max-h-[360px] min-w-[240px] overflow-y-auto')}>
                  {enabledModelConfigs.length === 0 ? (
                    <DropdownMenuItem className={CHAT_MENU_ITEM_CLASS} disabled>暂无可用模型</DropdownMenuItem>
                  ) : (
                    enabledModelConfigs.map((model) => (
                      <DropdownMenuItem
                        key={model.id}
                        className={CHAT_MODEL_MENU_ITEM_CLASS}
                        onSelect={() => changeModelConfig(model.id)}
                      >
                        <span className={CHAT_MODEL_MENU_COPY_CLASS}>
                          <span className={CHAT_MODEL_MENU_NAME_CLASS}>{modelDisplayName(model)}</span>
                          <span className={CHAT_MODEL_MENU_DETAIL_CLASS}>{modelDetailText(model)}</span>
                        </span>
                        {selectedModelConfig?.id === model.id && <ProductIcon name="check" size={15} />}
                      </DropdownMenuItem>
                    ))
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
              {currentSessionRunning && (
                <button
                  type="button"
                  className={cn(CHAT_COMPOSER_SEND_BTN_CLASS, CHAT_COMPOSER_STOP_BTN_CLASS)}
                  onClick={abortStream}
                  aria-label="停止生成"
                  title="停止生成"
                >
                  <ProductIcon name="stop" size={18} />
                </button>
              )}
              <button
                type="submit"
                className={CHAT_COMPOSER_SEND_BTN_CLASS}
                disabled={sendDisabled}
                aria-label={currentSessionRunning ? '加入发送队列' : '发送'}
                title={currentSessionRunning ? '加入发送队列' : '发送'}
              >
                <ProductIcon name="send" size={18} />
              </button>
            </div>
          </div>
        </form>
        <GeneralSkillInstallDialog
          open={generalSkillInstallOpen}
          sessionId={chat.sessionId || ''}
          intents={generalSkillInstallIntents}
          onClose={() => setGeneralSkillInstallOpen(false)}
          onCreate={createGeneralSkillInstallIntent}
          onResolve={resolveGeneralSkillInstallIntent}
        />
      </div>
    </div>
  );
}

function GeneralSkillInstallDialog({
  open,
  sessionId,
  intents,
  onClose,
  onCreate,
  onResolve,
}: {
  open: boolean;
  sessionId: string;
  intents: GeneralSkillInstallIntentRead[];
  onClose: () => void;
  onCreate: (source: { source_url: string; revision: string; source_subpath: string }) => Promise<GeneralSkillInstallIntentRead>;
  onResolve: (intent: GeneralSkillInstallIntentRead, command: 'confirm' | 'cancel') => Promise<void>;
}) {
  const [sourceUrl, setSourceUrl] = useState('https://github.com/mattpocock/skills');
  const [revision, setRevision] = useState('84fdeffd12f2ee307994d1eb6feb48173b6e0502');
  const [subpath, setSubpath] = useState('skills/engineering/diagnosing-bugs');
  const [loading, setLoading] = useState(false);
  const activeIntent = [...intents].reverse().find((item) => item.status === 'awaiting_owner_confirmation') || intents[intents.length - 1];

  async function createIntent() {
    if (!sessionId) {
      notify.warning('请先发送一条消息创建正式会话，再安装 Skill');
      return;
    }
    setLoading(true);
    try {
      await onCreate({ source_url: sourceUrl.trim(), revision: revision.trim(), source_subpath: subpath.trim() });
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建安装预览失败');
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && !loading && onClose()}>
      <DialogContent aria-describedby={undefined} className="max-h-[88vh] overflow-y-auto sm:max-w-[680px]">
        <DialogTitle>安装 Skill 到当前分身</DialogTitle>
        {!activeIntent ? (
          <div className="grid gap-3">
            <p className="gg-type-meta text-[#757f9c]">只接受固定 40 位 commit 和明确目录；系统先生成安全预览，不会直接执行包内脚本。</p>
            <label className="grid gap-1 gg-type-meta">GitHub 仓库地址<input aria-label="安装 Skill GitHub 仓库地址" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} className="h-10 rounded-lg border px-3" /></label>
            <label className="grid gap-1 gg-type-meta">完整 commit SHA<input aria-label="安装 Skill 完整 commit SHA" value={revision} onChange={(event) => setRevision(event.target.value)} className="h-10 rounded-lg border px-3 font-mono gg-type-code" /></label>
            <label className="grid gap-1 gg-type-meta">仓库内 Skill 目录<input aria-label="安装 Skill 仓库目录" value={subpath} onChange={(event) => setSubpath(event.target.value)} className="h-10 rounded-lg border px-3 font-mono gg-type-code" /></label>
            <div className="flex justify-end gap-2"><UIButton variant="outline" onClick={onClose}>取消</UIButton><UIButton disabled={loading} onClick={() => void createIntent()}>生成安装预览</UIButton></div>
          </div>
        ) : (
          <section aria-label="Skill 安装确认卡" className="grid gap-4 rounded-xl border border-[#cbd8ff] bg-[#f8faff] p-4">
            <div><strong className="gg-type-control">{activeIntent.candidates.map((item) => item.name).join('、') || 'Skill 安装'}</strong><p className="mt-1 break-all font-mono gg-type-caption text-[#596078]">{activeIntent.source_reference_redacted}</p></div>
            <dl className="grid grid-cols-2 gap-2 gg-type-meta"><div><dt>固定 commit</dt><dd className="font-mono gg-type-code">{activeIntent.source_revision?.slice(0, 12)}…</dd></div><div><dt>包 checksum</dt><dd className="font-mono gg-type-code">{activeIntent.raw_checksum?.slice(0, 12)}…</dd></div></dl>
            {activeIntent.candidates.map((candidate) => <div key={candidate.candidate_id} className="rounded-lg bg-white p-3"><div className="gg-type-body">{candidate.description}</div><div className="mt-2 gg-type-caption text-[#b45b00]">风险：{candidate.risk_findings.join('、') || '未发现'}</div><div className="mt-1 gg-type-caption">文件：{candidate.resources.length} 个</div></div>)}
            <div className="gg-type-control" role="status">状态：{activeIntent.status}</div>
            {activeIntent.status === 'awaiting_owner_confirmation' ? <div className="flex justify-end gap-2"><UIButton variant="outline" disabled={loading} onClick={() => void onResolve(activeIntent, 'cancel')}>取消安装</UIButton><UIButton disabled={loading} onClick={() => void onResolve(activeIntent, 'confirm')}>确认安装到当前分身</UIButton></div> : null}
            {activeIntent.status === 'installed' ? <UIButton onClick={onClose}>安装完成，开始使用</UIButton> : null}
          </section>
        )}
      </DialogContent>
    </Dialog>
  );
}
