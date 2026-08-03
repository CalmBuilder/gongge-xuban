import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';

import CapabilityPipeline from './CapabilityPipeline';

it('explains how experience becomes a traceable digital employee capability', () => {
  render(<CapabilityPipeline />);

  for (const label of [
    '岗位知识',
    '业务 SOP',
    '工作记忆',
    '业务工具',
    '全链路可追溯',
    '关键节点人工确认',
  ]) {
    expect(screen.getByText(label)).toBeInTheDocument();
  }
});
