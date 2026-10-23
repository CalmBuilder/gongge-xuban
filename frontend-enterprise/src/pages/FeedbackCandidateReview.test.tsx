import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { I18nProvider } from '@/i18n';
import type { SkillCard } from '@/types';
import type { SkillCandidateRead } from '@/types/skill-candidate';
import { FeedbackCandidateReview } from './FeedbackCandidateReview';

const card: SkillCard = { skill_id: 'skill-a', name: '隔离测试', version: '1.0.0',
  description: '当前原版', trigger_intents: [], user_utterance_examples: [], goal: [], required_info: [],
  nodes: [], edges: [], start_node_id: '', terminal_node_ids: [], interruption_policy: {}, response_rules: [] };
const candidate: SkillCandidateRead = { id: 'candidate-a', tenant_id: 'tenant-demo', skill_id: 'skill-a',
  scope_agent_id: 'agent-a', revision: 2, status: 'ready', base_skill_version_id: 'version-a',
  base_checksum: 'checksum-a', base_version: '1.0.0', base_difference: true,
  base_difference_confirmed: false, base_content: card, content: { ...card, description: '候选改进' },
  materials: [], changed_paths: ['/description'], published: null };

it('候选保存与人工发布是独立动作，发布必须明确确认原轮与当前基础差异', async () => {
  const user = userEvent.setup();
  const save = vi.fn(); const publish = vi.fn();
  render(<I18nProvider><FeedbackCandidateReview candidate={candidate} draft={candidate.content}
    error="" busy={false} onRefresh={vi.fn()} onSave={save} onReject={vi.fn()}
    onReset={vi.fn()} onPublish={publish} /></I18nProvider>);
  await user.click(screen.getByRole('button', { name: '仅保存隔离候选' }));
  expect(save).toHaveBeenCalledOnce(); expect(publish).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '人工确认发布候选' }));
  expect(screen.getByRole('button', { name: /^确认发布$/ })).toBeDisabled();
  await user.click(screen.getByRole('checkbox', { name: '我已核对原反馈版本与当前基础的差异' }));
  await user.click(screen.getByRole('button', { name: /^确认发布$/ }));
  expect(publish).toHaveBeenCalledWith(true);
});

it('未保存编辑禁止发布，失效状态清除原版/候选与发布操作', async () => {
  const props = { candidate, draft: { ...card, description: '未保存修改' }, error: '', busy: false,
    onRefresh: vi.fn(), onSave: vi.fn(), onReject: vi.fn(), onReset: vi.fn(), onPublish: vi.fn() };
  const view = render(<I18nProvider><FeedbackCandidateReview {...props} /></I18nProvider>);
  expect(screen.getByRole('button', { name: '人工确认发布候选' })).toBeDisabled();
  view.rerender(<I18nProvider><FeedbackCandidateReview {...props} candidate={null} draft={null}
    error="候选已失效" /></I18nProvider>);
  expect(screen.queryByText('当前原版')).not.toBeInTheDocument();
  expect(screen.queryByText('候选改进')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '人工确认发布候选' })).not.toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('候选已失效');
  expect(screen.getByRole('button', { name: '丢弃并加载当前范围原版' })).toBeEnabled();
});

it('人工确认弹窗绑定候选和修订，失效或切换后必须重新打开并核对差异', async () => {
  const user = userEvent.setup();
  const props = { candidate, draft: candidate.content, error: '', busy: false,
    onRefresh: vi.fn(), onSave: vi.fn(), onReject: vi.fn(), onReset: vi.fn(), onPublish: vi.fn() };
  const view = render(<I18nProvider><FeedbackCandidateReview {...props} /></I18nProvider>);
  await user.click(screen.getByRole('button', { name: '人工确认发布候选' }));
  await user.click(screen.getByRole('checkbox', { name: '我已核对原反馈版本与当前基础的差异' }));
  view.rerender(<I18nProvider><FeedbackCandidateReview {...props} candidate={null} error="候选已失效" /></I18nProvider>);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  const newer = { ...candidate, id: 'candidate-b', revision: 3 };
  view.rerender(<I18nProvider><FeedbackCandidateReview {...props} candidate={newer} /></I18nProvider>);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '人工确认发布候选' }));
  expect(screen.getByRole('checkbox', { name: '我已核对原反馈版本与当前基础的差异' })).not.toBeChecked();
  expect(screen.getByRole('button', { name: /^确认发布$/ })).toBeDisabled();
});
