import type { HTMLAttributes, ReactNode } from 'react';

import { cn } from '@/lib/utils';

export type EditorFooterProps = HTMLAttributes<HTMLDivElement> & {
  children: ReactNode;
};

/** 表单/编辑器底部操作栏，保证取消与提交动作的固定顺序和间距。 */
export function EditorFooter({ className, children, ...props }: EditorFooterProps) {
  return (
    <div {...props} className={cn('gg-editor-footer', className)}>
      {children}
    </div>
  );
}
