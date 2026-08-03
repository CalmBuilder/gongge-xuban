import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import type { AgentProfileRead } from '../../types';

import WorkRecordTab from './WorkRecordTab';

const CARD_CASES = [
  ['知识库', 'knowledge', 'bg-[#eaf7f0]'],
  ['技能', 'skill', 'bg-[#eef3ff]'],
  ['SOP', 'sop', 'bg-[#eaf9fc]'],
  ['工具', 'tools', 'bg-[#fff4e5]'],
  ['定时任务', 'tasks', 'bg-[#f3efff]'],
  ['对话日志', 'logs', 'bg-[#fff0f5]'],
] as const;

function renderWorkRecord() {
  return render(
    <MemoryRouter>
      <WorkRecordTab
        selectedAgent={{ id: 'agent-1' } as AgentProfileRead}
        activeKnowledge={[]}
        activeGeneralSkills={[]}
        activeSkills={[]}
        activeTools={[]}
        activeScheduledTasks={[]}
        activeScheduledTaskCount={0}
        employeeSessions={[]}
        replyStats={{ total: 0, today: 0, byDay: {} }}
        activityEvents={[]}
        positiveRate={0}
        negativeRate={0}
      />
    </MemoryRouter>,
  );
}

describe('WorkRecordTab capability cards', () => {
  it.each(CARD_CASES)('renders %s with the unified %s pastel treatment', (title, tone, background) => {
    renderWorkRecord();
    const card = screen.getByRole('button', { name: new RegExp(`^${title}`) });

    expect(card).toHaveClass(background);
    expect(card.querySelector(`[data-capability-kind="${tone}"]`)).toBeInTheDocument();
  });

  it('does not render legacy dark cards or bitmap illustrations', () => {
    const { container } = renderWorkRecord();

    expect(container.querySelector('[data-tone="dark"]')).not.toBeInTheDocument();
    expect(container.querySelector('img')).not.toBeInTheDocument();
  });
});
