import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';

import type { EnterpriseAuthUser } from '../auth';
import { EnterpriseContextProvider } from '../enterprise-context';
import { I18nProvider } from '../i18n';
import type { KnowledgeIngestJobRead } from '../types';
import KnowledgeManagePage from './KnowledgePage';

const client = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  post: vi.fn(),
}));

let sourceUnavailable = false;
let sourceAccessRevoked = false;
let knowledgeAgents: Array<Record<string, unknown>> = [];
let documentScope = 'overall';
let knowledgeBaseContentRevisions: Record<string, number> = {};
let reconciliationResponses: Array<KnowledgeIngestJobRead | null> = [];

vi.mock('../api/client', () => ({
  api: client,
  ApiError: class ApiError extends Error {
    status: number;

    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  getRequestTenantId: () => 'tenant_sd5_ui',
}));

const administrator: EnterpriseAuthUser = {
  id: 'user_sd5_ui',
  tenant_id: 'tenant_sd5_ui',
  username: 'sd5-ui-admin',
  role: 'admin',
  membership_status: 'active',
  member_category_code: 'employee',
  governance_permission_codes: ['knowledge.read', 'knowledge.manage'],
};

const knowledgeBase = {
  id: 'kb_sd5_ui',
  tenant_id: 'tenant_sd5_ui',
  name: '正文编辑库',
  description: '正文编辑测试',
  status: 'active',
  owner_user_id: 'user_sd5_ui',
  access_scope: 'owner' as const,
  download_policy: 'restricted' as const,
  revision: 1,
  content_revision: 1,
  organization_access: [],
  content_access_allowed: true,
  content_access_reason: 'owner',
  version: '1.0.0',
  metadata: { creator_name: 'sd5-ui-admin' },
  document_count: 1,
  bucket_count: 0,
  chunk_count: 0,
  created_at: '2026-09-29T00:00:00',
  updated_at: '2026-09-29T00:00:00',
};

const document = {
  id: 'doc_sd5_ui',
  tenant_id: 'tenant_sd5_ui',
  knowledge_base_id: 'kb_sd5_ui',
  knowledge_base_version_id: 'kbver_sd5_ui',
  filename: 'policy.md',
  file_type: 'md',
  title: '编辑前标题',
  status: 'ready',
  bucket_count: 0,
  chunk_count: 0,
  metadata: {
    document_card: { title: '编辑前标题', summary: '旧摘要' },
    section_tree: [],
  },
  error: null,
  created_at: '2026-09-29T00:00:00',
  updated_at: '2026-09-29T00:00:00',
};

const job = {
  id: 'job_sd5_ui',
  tenant_id: 'tenant_sd5_ui',
  knowledge_base_id: 'kb_sd5_ui',
  document_id: 'doc_sd5_ui',
  filename: 'policy.md',
  job_kind: 'revision',
  scope: 'overall',
  source_version_id: 'kbver_sd5_ui',
  expected_revision: 1,
  status: 'queued',
  stage: 'prepare',
  progress: 0,
  created_at: '2026-09-29T00:00:00',
  updated_at: '2026-09-29T00:00:00',
};

let polledJob: KnowledgeIngestJobRead = { ...job };

const publishedDocument = {
  ...document,
  id: 'doc_sd5_ui_published',
  knowledge_base_version_id: 'kbver_sd5_ui_published',
  title: '发布后标题',
};

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  client.get.mockReset();
  client.put.mockReset();
  client.post.mockReset();
  sourceUnavailable = false;
  sourceAccessRevoked = false;
  knowledgeAgents = [];
  documentScope = 'overall';
  knowledgeBaseContentRevisions = {};
  reconciliationResponses = [];
  polledJob = { ...job };
  client.get.mockImplementation(async (path: string) => {
    if (path.startsWith('/api/enterprise/agents?')) return knowledgeAgents;
    if (path.startsWith('/api/enterprise/model-configs?')) return [];
    if (path.includes('/documents/doc_sd5_ui/content?')) {
      if (sourceAccessRevoked) {
        throw Object.assign(new Error('当前正文已不可见'), { status: 403 });
      }
      if (sourceUnavailable) {
        throw Object.assign(new Error('历史文档没有可验证的完整正文，请粘贴替换正文。'), {
          status: 409,
          body: JSON.stringify({ detail: { code: 'KNOWLEDGE_SOURCE_UNAVAILABLE' } }),
        });
      }
      return {
        document_id: document.id,
        lineage_id: 'lineage_sd5_ui',
        knowledge_base_version_id: 'kbver_sd5_ui',
        scope: documentScope,
        head_version_id: 'kbver_sd5_ui',
        content_revision: 1,
        content_md: '# 编辑前正文\n旧事实',
        source_checksum: 'a'.repeat(64),
        source_kind: 'normalized',
      };
    }
    if (path.includes('/jobs/by-client-request?')) {
      return reconciliationResponses.shift() ?? null;
    }
    if (path.includes('/documents/doc_sd5_ui/buckets?')) return [];
    if (path.startsWith('/api/enterprise/knowledge/documents?')) return [document];
    if (path.includes('/okf/concepts?')) return [];
    if (path.includes('/documents/doc_sd5_ui_published?')) return publishedDocument;
    if (path.includes('/jobs/job_sd5_ui?')) return polledJob;
    if (path.startsWith('/api/enterprise/knowledge-bases?')) {
      const scopedAgentId = new URL(`http://local${path}`).searchParams.get('agent_id') || 'overall';
      return [{
        ...knowledgeBase,
        content_revision: knowledgeBaseContentRevisions[scopedAgentId] || knowledgeBase.content_revision,
      }];
    }
    if (path.startsWith('/api/organization/unit-children?')) return [];
    throw new Error(`unexpected request: ${path}`);
  });
  client.put.mockImplementation(async (path: string) => {
    if (path.includes('/documents/doc_sd5_ui')) {
      return {
        job_id: job.id,
        status: 'queued',
        source_version_id: 'kbver_sd5_ui',
        expected_revision: 1,
      };
    }
    throw new Error(`unexpected write: ${path}`);
  });
  client.post.mockImplementation(async (path: string) => {
    if (path.includes('/jobs/job_sd5_ui/cancel?')) {
      const scopedAgentId = new URL(`http://local${path}`).searchParams.get('agent_id');
      return {
        ...job,
        scope: scopedAgentId ? `agent:${scopedAgentId}` : 'overall',
        status: 'cancelled',
      };
    }
    throw new Error(`unexpected post: ${path}`);
  });
});

it('读取正文并以冻结 head/CAS 受理持久修订任务', async () => {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  expect(editor).toHaveValue('# 编辑前正文\n旧事实');
  await user.clear(editor);
  await user.type(editor, '# 编辑后正文\n新事实');
  await user.click(screen.getByRole('button', { name: '保存正文' }));

  await waitFor(() => expect(client.put).toHaveBeenCalledWith(
    '/api/enterprise/knowledge/documents/doc_sd5_ui',
    expect.objectContaining({
      tenant_id: 'tenant_sd5_ui',
      content_md: '# 编辑后正文\n新事实',
      expected_version_id: 'kbver_sd5_ui',
      expected_revision: 1,
      client_request_id: expect.any(String),
    }),
  ));
  expect(await screen.findByRole('status')).toHaveTextContent('正文重建：prepare');
});

/** 验证迟到员工scope不会改写整体正文的CAS、轮询或取消范围。 */
it('平台治理编辑正文时不把编辑期间恢复的员工scope串入overall修订', async () => {
  const user = userEvent.setup();
  knowledgeAgents = [{
    id: 'agent_scope_race',
    tenant_id: 'tenant_sd5_ui',
    name: '并发作用域员工',
    description: '',
    status: 'active',
    is_overall: false,
    metadata: {},
  }];
  window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_scope_race');
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await user.click(await screen.findByRole('button', { name: '平台知识治理' }));
  await waitFor(() => expect(window.localStorage.getItem('gongge_enterprise_agent_scope')).toBeNull());
  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(editor);
  await user.type(editor, '# 平台治理修订\n保持overall CAS');

  // 模拟宿主在编辑器保持打开时迟到恢复员工scope；已读取正文仍绑定overall。
  act(() => {
    window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_scope_race');
    window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', {
      detail: { agentId: 'agent_scope_race' },
    }));
  });
  await waitFor(() => expect(client.get.mock.calls.some(([path]) =>
    String(path).includes('/api/enterprise/knowledge-bases?')
    && String(path).includes('agent_id=agent_scope_race'))).toBe(true));

  await user.click(screen.getByRole('button', { name: '保存正文' }));
  await waitFor(() => expect(client.put).toHaveBeenCalledWith(
    '/api/enterprise/knowledge/documents/doc_sd5_ui',
    expect.objectContaining({
      tenant_id: 'tenant_sd5_ui',
      content_md: '# 平台治理修订\n保持overall CAS',
      expected_version_id: 'kbver_sd5_ui',
      expected_revision: 1,
      client_request_id: expect.any(String),
    }),
  ));
  await waitFor(() => expect(client.get.mock.calls.some(([path]) =>
    String(path).includes('/jobs/job_sd5_ui?tenant_id=tenant_sd5_ui')
    && !String(path).includes('agent_id='))).toBe(true));
  await user.click(screen.getByRole('button', { name: '取消重建' }));
  await waitFor(() => expect(client.post).toHaveBeenCalledWith(
    '/api/enterprise/knowledge/jobs/job_sd5_ui/cancel?tenant_id=tenant_sd5_ui',
  ));
});

/** 验证员工正文的 PUT、持久任务查询和取消均使用已读取的原分支。 */
it('员工正文修订在scope切换后仍沿原分支读取任务并取消', async () => {
  const user = userEvent.setup();
  const branchAgent = {
    id: 'agent_scope_branch',
    tenant_id: 'tenant_sd5_ui',
    name: '分支员工',
    description: '',
    status: 'active',
    is_overall: false,
    metadata: {},
  };
  knowledgeAgents = [branchAgent, { ...branchAgent, id: 'agent_scope_other', name: '另一员工' }];
  documentScope = 'agent:agent_scope_branch';
  polledJob = { ...job, scope: 'agent:agent_scope_branch' };
  window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_scope_branch');
  const firstRender = render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(editor);
  await user.type(editor, '# 员工分支修订\n原scope CAS');
  await user.click(screen.getByRole('button', { name: '保存正文' }));

  expect(client.get.mock.calls.some(([path]) =>
    String(path).includes('/documents/doc_sd5_ui/content?tenant_id=tenant_sd5_ui&agent_id=agent_scope_branch'))).toBe(true);
  await waitFor(() => expect(client.put).toHaveBeenCalledWith(
    '/api/enterprise/knowledge/documents/doc_sd5_ui?agent_id=agent_scope_branch',
    expect.objectContaining({
      content_md: '# 员工分支修订\n原scope CAS',
      expected_version_id: 'kbver_sd5_ui',
      expected_revision: 1,
    }),
  ));
  await waitFor(() => expect(client.get.mock.calls.some(([path]) =>
    String(path).includes('/jobs/job_sd5_ui?tenant_id=tenant_sd5_ui&agent_id=agent_scope_branch'))).toBe(true));

  firstRender.unmount();
  window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_scope_branch');
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );
  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  expect(await screen.findByRole('textbox', { name: '文档正文' })).toBeDisabled();
  expect(client.put).toHaveBeenCalledTimes(1);

  act(() => {
    window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_scope_other');
    window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', {
      detail: { agentId: 'agent_scope_other' },
    }));
  });
  await user.click(screen.getByRole('button', { name: '取消重建' }));
  await waitFor(() => expect(client.post).toHaveBeenCalledWith(
    '/api/enterprise/knowledge/jobs/job_sd5_ui/cancel?tenant_id=tenant_sd5_ui&agent_id=agent_scope_branch',
  ));
});

/** 验证历史正文手动替换使用打开编辑器时冻结的员工分支。 */
it('历史正文粘贴替换不会被编辑期间的scope切换重定向', async () => {
  sourceUnavailable = true;
  const user = userEvent.setup();
  const branchAgent = {
    id: 'agent_scope_fallback',
    tenant_id: 'tenant_sd5_ui',
    name: '历史分支员工',
    description: '',
    status: 'active',
    is_overall: false,
    metadata: {},
  };
  knowledgeAgents = [branchAgent];
  polledJob = { ...job, scope: 'agent:agent_scope_fallback' };
  window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_scope_fallback');
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  await screen.findByRole('alert');
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.type(editor, '# 历史替换正文\n保留打开时scope');
  act(() => {
    window.localStorage.removeItem('gongge_enterprise_agent_scope');
    window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', {
      detail: { agentId: '' },
    }));
  });
  await user.click(screen.getByRole('button', { name: '保存正文' }));

  await waitFor(() => expect(client.put).toHaveBeenCalledWith(
    '/api/enterprise/knowledge/documents/doc_sd5_ui?agent_id=agent_scope_fallback',
    expect.objectContaining({
      content_md: '# 历史替换正文\n保留打开时scope',
      expected_version_id: 'kbver_sd5_ui',
      expected_revision: 1,
    }),
  ));
});

/** 验证正文读取被撤权后不会继续展示缓存的编辑内容。 */
it('正文读取撤权时清除私有编辑器并且不提交修订', async () => {
  sourceAccessRevoked = true;
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));

  await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑文档' })).not.toBeInTheDocument());
  expect(screen.queryByRole('textbox', { name: '文档正文' })).not.toBeInTheDocument();
  expect(client.put).not.toHaveBeenCalled();
});

/** 验证任务轮询或取消期间撤权会清除正文并保留同一job对账身份。 */
it('正文任务取消撤权时清除私有正文并保留恢复指针', async () => {
  client.post.mockRejectedValue(Object.assign(new Error('当前任务不可见'), { status: 403 }));
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(editor);
  await user.type(editor, '# 待撤权修订\n不能继续展示');
  await user.click(screen.getByRole('button', { name: '保存正文' }));
  await waitFor(() => expect(client.put).toHaveBeenCalledTimes(1));

  await user.click(screen.getByRole('button', { name: '取消重建' }));
  await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑文档' })).not.toBeInTheDocument());
  expect(screen.queryByRole('textbox', { name: '文档正文' })).not.toBeInTheDocument();
  expect(window.sessionStorage.getItem(
    'gongge-knowledge-revision:tenant_sd5_ui:overall:doc_sd5_ui:job',
  )).toBe(job.id);
  expect(window.sessionStorage.getItem(
    'gongge-knowledge-revision:tenant_sd5_ui:overall:doc_sd5_ui',
  )).toBeTruthy();
});

it('历史正文不可重建时允许显式粘贴替换并沿当前 head 受理', async () => {
  sourceUnavailable = true;
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  expect(editor).toHaveValue('');
  await user.type(editor, '# 替换后的完整正文\n新事实');
  await user.click(screen.getByRole('button', { name: '保存正文' }));

  await waitFor(() => expect(client.put).toHaveBeenCalledWith(
    '/api/enterprise/knowledge/documents/doc_sd5_ui',
    expect.objectContaining({
      content_md: '# 替换后的完整正文\n新事实',
      expected_version_id: 'kbver_sd5_ui',
      expected_revision: 1,
      client_request_id: expect.any(String),
    }),
  ));
});

it('正文快速重复提交复用同一持久 client request identity', async () => {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(editor);
  await user.type(editor, '# 快速重复提交\n同一正文');
  const saveButton = screen.getByRole('button', { name: '保存正文' });
  (saveButton as HTMLButtonElement).click();
  (saveButton as HTMLButtonElement).click();

  await waitFor(() => expect(client.put).toHaveBeenCalledTimes(2));
  const firstBody = client.put.mock.calls[0]?.[1] as { client_request_id?: string };
  const secondBody = client.put.mock.calls[1]?.[1] as { client_request_id?: string };
  expect(firstBody.client_request_id).toBeTruthy();
  expect(secondBody.client_request_id).toBe(firstBody.client_request_id);
});

it('完成终态自动关闭编辑器并加载发布后的物理文档', async () => {
  polledJob = {
    ...job,
    status: 'completed',
    stage: 'publish',
    progress: 1,
    published_document_id: publishedDocument.id,
  };
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(editor);
  await user.type(editor, '# 已发布正文\n新事实');
  await user.click(screen.getByRole('button', { name: '保存正文' }));

  await waitFor(() => expect(client.put).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.queryByRole('textbox', { name: '文档正文' })).not.toBeInTheDocument());
  expect(client.get.mock.calls.some(([path]) => String(path).includes('/documents/doc_sd5_ui_published?'))).toBe(true);
});

it('失败终态后重新编辑同一文档会生成新 identity', async () => {
  polledJob = {
    ...job,
    status: 'failed',
    stage: 'validate',
    error: '模拟失败',
  };
  const user = userEvent.setup();
  const renderPage = () => render(
      <MemoryRouter initialEntries={['/enterprise/knowledge']}>
        <I18nProvider>
          <EnterpriseContextProvider
            value={{
              tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
              member: administrator,
              is_administrator: true,
            }}
          >
            <KnowledgeManagePage currentUser={administrator} />
          </EnterpriseContextProvider>
        </I18nProvider>
      </MemoryRouter>,
    );
  const { unmount } = renderPage();

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  let editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(editor);
  await user.type(editor, '# 第一次失败\n失败事实');
  await user.click(screen.getByRole('button', { name: '保存正文' }));
  await waitFor(() => expect(screen.getByText('模拟失败')).toBeInTheDocument());
  const firstBody = client.put.mock.calls[0]?.[1] as { client_request_id?: string };
  expect(firstBody.client_request_id).toBeTruthy();

  unmount();
  renderPage();
  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(editor);
  await user.type(editor, '# 第二次编辑\n新事实');
  await user.click(screen.getByRole('button', { name: '保存正文' }));

  await waitFor(() => expect(client.put).toHaveBeenCalledTimes(2));
  const secondBody = client.put.mock.calls[1]?.[1] as { client_request_id?: string };
  expect(secondBody.client_request_id).toBeTruthy();
  expect(secondBody.client_request_id).not.toBe(firstBody.client_request_id);
});

it('未决修订卸载后重新打开仍复用同一 identity', async () => {
  const user = userEvent.setup();
  const renderPage = () => render(
      <MemoryRouter initialEntries={['/enterprise/knowledge']}>
        <I18nProvider>
          <EnterpriseContextProvider
            value={{
              tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
              member: administrator,
              is_administrator: true,
            }}
          >
            <KnowledgeManagePage currentUser={administrator} />
          </EnterpriseContextProvider>
        </I18nProvider>
      </MemoryRouter>,
    );
  const { unmount } = renderPage();

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  let editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(editor);
  await user.type(editor, '# 未决第一次\n等待事实');
  await user.click(screen.getByRole('button', { name: '保存正文' }));
  await waitFor(() => expect(client.put).toHaveBeenCalledTimes(1));
  const firstBody = client.put.mock.calls[0]?.[1] as { client_request_id?: string };
  expect(firstBody.client_request_id).toBeTruthy();

  unmount();
  renderPage();
  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  editor = await screen.findByRole('textbox', { name: '文档正文' });
  expect(editor).toBeDisabled();
  expect(window.sessionStorage.getItem(
    'gongge-knowledge-revision:tenant_sd5_ui:overall:doc_sd5_ui',
  )).toBe(firstBody.client_request_id);
  expect(window.sessionStorage.getItem(
    'gongge-knowledge-revision:tenant_sd5_ui:overall:doc_sd5_ui:job',
  )).toBe(job.id);
  expect(client.put).toHaveBeenCalledTimes(1);
});

/** 验证正文修订提交被撤权时清除正在编辑的私密正文并保留对账身份。 */
it('正文修订PUT撤权时清除私密正文并保留job恢复指针', async () => {
  client.put.mockRejectedValueOnce(Object.assign(new Error('当前知识范围不可写'), { status: 403 }));
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(editor);
  await user.type(editor, '# 撤权私密草稿\n不可继续显示');
  await user.click(screen.getByRole('button', { name: '保存正文' }));

  await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑文档' })).not.toBeInTheDocument());
  expect(screen.queryByText('撤权私密草稿')).not.toBeInTheDocument();
  expect(window.sessionStorage.getItem(
    'gongge-knowledge-revision:tenant_sd5_ui:overall:doc_sd5_ui',
  )).toBeTruthy();
});

/** 验证历史正文替换使用打开编辑器时冻结的scope head revision。 */
it('历史正文替换在scope切换后继续使用打开时冻结的revision', async () => {
  sourceUnavailable = true;
  knowledgeBaseContentRevisions = { overall: 1, agent_scope_fallback_revision: 9 };
  knowledgeAgents = [{
    id: 'agent_scope_fallback_revision',
    tenant_id: 'tenant_sd5_ui',
    name: '历史正文修订分支',
    description: '',
    status: 'active',
    is_overall: false,
    metadata: {},
  }];
  window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_scope_fallback_revision');
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await user.click(await screen.findByRole('button', { name: '平台知识治理' }));
  await waitFor(() => expect(window.localStorage.getItem('gongge_enterprise_agent_scope')).toBeNull());
  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.type(editor, '# 历史整体来源替换\nrevision来自overall');

  act(() => {
    window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_scope_fallback_revision');
    window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', {
      detail: { agentId: 'agent_scope_fallback_revision' },
    }));
  });
  await waitFor(() => expect(client.get.mock.calls.some(([path]) =>
    String(path).includes('/api/enterprise/knowledge-bases?')
    && String(path).includes('agent_id=agent_scope_fallback_revision'))).toBe(true));
  await user.click(screen.getByRole('button', { name: '保存正文' }));

  await waitFor(() => expect(client.put).toHaveBeenCalledWith(
    '/api/enterprise/knowledge/documents/doc_sd5_ui',
    expect.objectContaining({
      content_md: '# 历史整体来源替换\nrevision来自overall',
      expected_version_id: 'kbver_sd5_ui',
      expected_revision: 1,
    }),
  ));
});

/** 验证轮询返回错误scope时停止任务展示并清除私密编辑内容。 */
it('正文任务轮询scope不匹配时清除私密内容并保留恢复指针', async () => {
  polledJob = { ...job, scope: 'agent:agent_scope_wrong' };
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(editor);
  await user.type(editor, '# 错scope私密内容\n必须清除');
  await user.click(screen.getByRole('button', { name: '保存正文' }));

  await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑文档' })).not.toBeInTheDocument());
  expect(screen.queryByText('错scope私密内容')).not.toBeInTheDocument();
  expect(window.sessionStorage.getItem(
    'gongge-knowledge-revision:tenant_sd5_ui:overall:doc_sd5_ui:job',
  )).toBe(job.id);
});

/** 验证取消返回不同scope的非终态任务时不切换到错误scope继续轮询。 */
it('取消响应scope不匹配时停止正文任务并清除私密编辑内容', async () => {
  const branchAgent = {
    id: 'agent_scope_cancel_guard',
    tenant_id: 'tenant_sd5_ui',
    name: '取消范围员工',
    description: '',
    status: 'active',
    is_overall: false,
    metadata: {},
  };
  knowledgeAgents = [branchAgent];
  documentScope = 'agent:agent_scope_cancel_guard';
  polledJob = { ...job, scope: documentScope };
  window.localStorage.setItem('gongge_enterprise_agent_scope', branchAgent.id);
  client.post.mockResolvedValueOnce({ ...job, status: 'queued', scope: 'overall' });
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(editor);
  await user.type(editor, '# 取消scope错误私密内容\n不能误轮询');
  await user.click(screen.getByRole('button', { name: '保存正文' }));
  await waitFor(() => expect(client.put).toHaveBeenCalledTimes(1));
  await user.click(await screen.findByRole('button', { name: '取消重建' }));

  await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑文档' })).not.toBeInTheDocument());
  expect(screen.queryByText('取消scope错误私密内容')).not.toBeInTheDocument();
  expect(client.get.mock.calls.some(([path]) =>
    String(path).includes('/jobs/job_sd5_ui?tenant_id=tenant_sd5_ui')
    && !String(path).includes('agent_id=agent_scope_cancel_guard'))).toBe(false);
});

/** 验证scope相同但job指向另一文档时也按完整身份失败关闭。 */
it('正文任务polling文档身份不匹配时清除私密编辑状态', async () => {
  polledJob = { ...job, document_id: 'doc_foreign', scope: 'overall' };
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(editor);
  await user.type(editor, '# 错文档任务响应\n必须清除');
  await user.click(screen.getByRole('button', { name: '保存正文' }));

  await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑文档' })).not.toBeInTheDocument());
  expect(screen.queryByText('错文档任务响应')).not.toBeInTheDocument();
  expect(window.sessionStorage.getItem(
    'gongge-knowledge-revision:tenant_sd5_ui:overall:doc_sd5_ui:job',
  )).toBe(job.id);
});

/** 验证回执丢失后先按原身份对账，重启恢复命中任务且不重新提交。 */
it('正文修订回执丢失后先对账并在重启后恢复原job且不盲重发', async () => {
  client.put.mockRejectedValueOnce(new Error('连接在响应前断开'));
  reconciliationResponses = [null, { ...job }];
  const user = userEvent.setup();
  const renderPage = () => render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );
  const firstPage = renderPage();

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const firstEditor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(firstEditor);
  await user.type(firstEditor, '# 回执未知修订\n按原request id核对');
  await user.click(screen.getByRole('button', { name: '保存正文' }));

  await screen.findByRole('alert');
  expect(screen.getByRole('button', { name: '核对修订状态' })).toBeEnabled();
  expect(client.put).toHaveBeenCalledTimes(1);
  expect(client.get.mock.calls.some(([path]) =>
    String(path).includes('/jobs/by-client-request?')
    && String(path).includes('client_request_id='))).toBe(true);

  firstPage.unmount();
  const secondPage = renderPage();
  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));

  await waitFor(() => expect(window.sessionStorage.getItem(
    'gongge-knowledge-revision:tenant_sd5_ui:overall:doc_sd5_ui:job',
  )).toBe(job.id));
  expect(client.put).toHaveBeenCalledTimes(1);
  expect(await screen.findByRole('textbox', { name: '文档正文' })).toBeDisabled();
  secondPage.unmount();
});

/** 验证只改标题状态时也按打开编辑器时冻结的scope提交。 */
it('文档字段保存不会被编辑期间切换的scope重定向', async () => {
  knowledgeAgents = [{
    id: 'agent_scope_metadata_race',
    tenant_id: 'tenant_sd5_ui',
    name: '字段编辑范围员工',
    description: '',
    status: 'active',
    is_overall: false,
    metadata: {},
  }];
  window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_scope_metadata_race');
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await user.click(await screen.findByRole('button', { name: '平台知识治理' }));
  await waitFor(() => expect(window.localStorage.getItem('gongge_enterprise_agent_scope')).toBeNull());
  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  await screen.findByRole('textbox', { name: '文档正文' });
  const title = screen.getByPlaceholderText('文档标题');
  await user.clear(title);
  await user.type(title, '整体字段改名');
  act(() => {
    window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_scope_metadata_race');
    window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', {
      detail: { agentId: 'agent_scope_metadata_race' },
    }));
  });
  await waitFor(() => expect(client.get.mock.calls.some(([path]) =>
    String(path).includes('/api/enterprise/knowledge-bases?')
    && String(path).includes('agent_id=agent_scope_metadata_race'))).toBe(true));
  await user.click(screen.getByRole('button', { name: '保存正文' }));

  await waitFor(() => expect(client.put).toHaveBeenCalledWith(
    '/api/enterprise/knowledge/documents/doc_sd5_ui',
    expect.objectContaining({ title: '整体字段改名', status: 'ready' }),
  ));
});

/** 验证整体修订完成时不会沿切换后的员工scope读取原文档分桶。 */
it('整体修订完成时scope已切换则不向新scope加载整体发布结果', async () => {
  knowledgeAgents = [{
    id: 'agent_scope_publish_race',
    tenant_id: 'tenant_sd5_ui',
    name: '发布期间切换员工',
    description: '',
    status: 'active',
    is_overall: false,
    metadata: {},
  }];
  const jobReadGate: { release?: () => void } = {};
  const pausedJobReads = new Promise<void>((resolve) => {
    jobReadGate.release = resolve;
  });
  const originalGet = client.get.getMockImplementation();
  if (!originalGet) throw new Error('knowledge API mock implementation is unavailable');
  client.get.mockImplementation(async (path: string) => {
    if (path.includes('/jobs/job_sd5_ui?')) {
      await pausedJobReads;
      return polledJob;
    }
    return originalGet(path);
  });
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/enterprise/knowledge']}>
      <I18nProvider>
        <EnterpriseContextProvider
          value={{
            tenant: { id: 'tenant_sd5_ui', name: 'SD-5 测试租户' },
            member: administrator,
            is_administrator: true,
          }}
        >
          <KnowledgeManagePage currentUser={administrator} />
        </EnterpriseContextProvider>
      </I18nProvider>
    </MemoryRouter>,
  );

  await screen.findByText('编辑前标题');
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await user.click(await screen.findByRole('button', { name: '修改' }));
  const editor = await screen.findByRole('textbox', { name: '文档正文' });
  await user.clear(editor);
  await user.type(editor, '# 整体发布完成\n结果留在原scope');
  await user.click(screen.getByRole('button', { name: '保存正文' }));
  await waitFor(() => expect(client.get.mock.calls.some(([path]) =>
    String(path).includes('/jobs/job_sd5_ui?tenant_id=tenant_sd5_ui'))).toBe(true));
  act(() => {
    window.localStorage.setItem('gongge_enterprise_agent_scope', 'agent_scope_publish_race');
    window.dispatchEvent(new CustomEvent('gongge-enterprise-agent-scope-change', {
      detail: { agentId: 'agent_scope_publish_race' },
    }));
  });
  await waitFor(() => expect(client.get.mock.calls.some(([path]) =>
    String(path).includes('/api/enterprise/knowledge-bases?')
    && String(path).includes('agent_id=agent_scope_publish_race'))).toBe(true));
  await waitFor(() => expect(client.get.mock.calls.filter(([path]) =>
    String(path).includes('/documents/doc_sd5_ui/buckets?')
    && String(path).includes('agent_id=agent_scope_publish_race')).length).toBeGreaterThan(0));
  const branchDocumentBucketReadsBeforeCompletion = client.get.mock.calls.filter(([path]) =>
    String(path).includes('/documents/doc_sd5_ui/buckets?')
    && String(path).includes('agent_id=agent_scope_publish_race')).length;
  polledJob = {
    ...job,
    status: 'completed',
    stage: 'publish',
    progress: 1,
    published_document_id: publishedDocument.id,
  };
  if (!jobReadGate.release) throw new Error('knowledge job read gate is unavailable');
  jobReadGate.release();

  await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑文档' })).not.toBeInTheDocument());
  expect(client.get.mock.calls.some(([path]) =>
    String(path).includes(`/documents/${publishedDocument.id}?tenant_id=tenant_sd5_ui`)
    && !String(path).includes('agent_id='))).toBe(true);
  expect(client.get.mock.calls.filter(([path]) =>
    String(path).includes('/documents/doc_sd5_ui/buckets?')
    && String(path).includes('agent_id=agent_scope_publish_race')).length
  ).toBe(branchDocumentBucketReadsBeforeCompletion);
});
