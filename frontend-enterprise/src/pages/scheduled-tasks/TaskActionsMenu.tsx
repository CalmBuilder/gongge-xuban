import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui';
import { ShieldCheck } from 'lucide-react';

import IconEdit from '../../assets/icons/edit.svg?react';
import IconListBulleted from '../../assets/icons/list-bulleted.svg?react';
import IconMore from '../../assets/icons/more.svg?react';
import IconPause from '../../assets/icons/pause.svg?react';
import IconPlay from '../../assets/icons/play.svg?react';
import IconTrash from '../../assets/icons/trash.svg?react';
import type { ScheduledTaskRead } from '../../types';

const MENU_ITEM_CLASS =
  'w-[110px] cursor-pointer gap-[4px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[#858b9c] focus:text-[#18181a] [&_svg]:size-[14px]';
const MENU_ITEM_DANGER_CLASS =
  'w-[110px] cursor-pointer gap-[4px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[#d20b0b] focus:bg-[#fce7e7] focus:text-[#d20b0b] focus:[&_svg]:text-[#d20b0b]! [&_svg]:size-[14px]';

export type TaskActionsMenuProps = {
  task: ScheduledTaskRead;
  onViewRuns: (task: ScheduledTaskRead) => void;
  onEdit: (task: ScheduledTaskRead) => void;
  onRunNow: (task: ScheduledTaskRead) => void;
  onToggleStatus: (task: ScheduledTaskRead) => void;
  onStandingApproval: (task: ScheduledTaskRead) => void;
  onDelete: (task: ScheduledTaskRead) => void;
};

/** Row action dropdown for a scheduled task (共格 node 281:2178). */
export function TaskActionsMenu({
  task,
  onViewRuns,
  onEdit,
  onRunNow,
  onToggleStatus,
  onStandingApproval,
  onDelete,
}: TaskActionsMenuProps) {
  const isArchived = task.status === 'archived';
  const isCompleted = task.status === 'completed';
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label="操作"
        className="grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
      >
        <IconMore className="size-3.5" />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="flex w-auto min-w-0 flex-col gap-[4px] rounded-[14px] border-0 bg-white p-[4px] shadow-[0px_0px_8px_rgba(0,0,0,0.1)] ring-0 [--accent:#F6F6F6] [--accent-foreground:#18181A]"
      >
        <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => onViewRuns(task)}>
          <IconListBulleted />
          查看记录
        </DropdownMenuItem>
        {!isArchived && (
          <>
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => onEdit(task)}>
              <IconEdit />
              编辑
            </DropdownMenuItem>
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => onRunNow(task)}>
              <IconPlay />
              立即执行
            </DropdownMenuItem>
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => onStandingApproval(task)}>
              <ShieldCheck />
              长期批准
            </DropdownMenuItem>
            {!isCompleted && (
              <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => onToggleStatus(task)}>
                {task.status === 'active' ? <IconPause /> : <IconPlay />}
                {task.status === 'active' ? '暂停' : '启用'}
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator className="my-[2px] bg-[#eef0f4]" />
            <DropdownMenuItem
              variant="destructive"
              className={MENU_ITEM_DANGER_CLASS}
              onSelect={() => onDelete(task)}
            >
              <IconTrash />
              删除
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
