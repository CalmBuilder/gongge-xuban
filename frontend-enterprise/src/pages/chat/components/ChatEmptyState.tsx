import EmployeeAvatar from '@/components/EmployeeAvatar';
import { employeeDisplayName } from '@/employee';

import {
  CHAT_EMPTY_CARD_CLASS,
  CHAT_EMPTY_CLASS,
  CHAT_EMPTY_GREETING_CARD_CLASS,
  CHAT_EMPTY_ROLE_CLASS,
  CHAT_EMPTY_STAT_CELL_CLASS,
  CHAT_EMPTY_SUBTITLE_CLASS,
  CHAT_EMPTY_TAGS_CLASS,
  CHAT_EMPTY_TITLE_CLASS,
} from '../chatPageStyles';
import type { UseChatSession } from '../useChatSession';

export default function ChatEmptyState({ chat }: { chat: UseChatSession }) {
  const { displayedAgent, displayedProfile, emptyRoleSummary, emptyProfileTags, emptyStats } = chat;

  return (
    <div className={CHAT_EMPTY_CLASS}>
      <div className={CHAT_EMPTY_GREETING_CARD_CLASS}>
        <div className="relative h-[132px] w-[132px] max-[560px]:h-[112px] max-[560px]:w-[88px]">
          <div className="absolute bottom-0 left-0 h-[156px] w-[132px] max-[560px]:h-[120px] max-[560px]:w-[88px]">
            <EmployeeAvatar
              profile={displayedProfile ?? undefined}
              agent={displayedAgent ?? undefined}
              width={132}
              height={156}
              radius={0}
              fit="cover"
              objectPosition="bottom"
              className="bg-transparent! max-[560px]:h-[120px]! max-[560px]:w-[88px]!"
            />
          </div>
        </div>
        <div className="flex min-w-0 flex-col justify-center gap-[10px] self-stretch py-[22px]">
          <strong className={CHAT_EMPTY_TITLE_CLASS}>
            Hello {displayedAgent ? employeeDisplayName(displayedAgent) : ''}！
          </strong>
          <span className={CHAT_EMPTY_SUBTITLE_CLASS}>我们来做什么？</span>
        </div>
      </div>

      <div className={CHAT_EMPTY_CARD_CLASS}>
        <div className="flex min-w-0 flex-col justify-center gap-[8px] px-[4px]">
          <p className={CHAT_EMPTY_ROLE_CLASS}>{emptyRoleSummary}</p>
          <div className={CHAT_EMPTY_TAGS_CLASS}>
            {emptyProfileTags.map((tag, index) => (
              <span key={`${tag}-${index}`}>{tag}</span>
            ))}
          </div>
        </div>
        <div className="flex min-w-0 items-stretch">
          {emptyStats.map((item) => (
            <div key={item.label} className={CHAT_EMPTY_STAT_CELL_CLASS}>
              <span className="text-[18px] font-medium leading-none">{item.value}</span>
              <span className="text-[10px] leading-none">{item.label}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
