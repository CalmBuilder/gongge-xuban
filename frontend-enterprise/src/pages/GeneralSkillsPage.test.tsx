import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';

import { I18nProvider } from '../i18n';
import { SecureSkillImportDialog } from './GeneralSkillsPage';

function renderDialog(
  sourceKind: 'upload' | 'folder' | 'github' | 'https' = 'github',
  job: Parameters<typeof SecureSkillImportDialog>[0]['job'] = null,
) {
  /** 用受控属性渲染安全导入对话框，隔离验证来源契约和无障碍名称。 */

  const callbacks = {
    onFileChange: vi.fn(),
    onFolderFilesChange: vi.fn(),
    onSourceKindChange: vi.fn(),
    onSourceUrlChange: vi.fn(),
    onRevisionChange: vi.fn(),
    onSourceSubpathChange: vi.fn(),
    onSelectedIdsChange: vi.fn(),
    onDependencyDecisionChange: vi.fn(),
    onPreview: vi.fn(),
    onConfirm: vi.fn(),
    onReset: vi.fn(),
    onClose: vi.fn(),
  };
  render(
    <I18nProvider>
      <SecureSkillImportDialog
        open
        loading={false}
        sourceKind={sourceKind}
        file={null}
        folderFiles={[]}
        sourceUrl="https://github.com/mattpocock/skills"
        revision=""
        sourceSubpath="skills"
        job={job}
        selectedIds={[]}
        dependencyDecisions={{}}
        {...callbacks}
      />
    </I18nProvider>,
  );
  return callbacks;
}

it('collects a fixed GitHub revision and explicit repository subpath before preview', async () => {
  /** 验证 GitHub 导入向用户显式呈现 URL、完整 SHA 和子树三个审核字段。 */

  const user = userEvent.setup();
  const callbacks = renderDialog();

  fireEvent.change(screen.getByLabelText('完整 commit SHA'), {
    target: { value: '84fdeffd12f2ee307994d1eb6feb48173b6e0502' },
  });
  expect(screen.getByLabelText('仓库内 Skill 目录')).toHaveValue('skills');
  await user.click(screen.getByRole('button', { name: '生成安全预览' }));

  expect(screen.getByLabelText('GitHub 仓库地址')).toHaveValue(
    'https://github.com/mattpocock/skills',
  );
  expect(callbacks.onRevisionChange).toHaveBeenLastCalledWith(
    '84fdeffd12f2ee307994d1eb6feb48173b6e0502',
  );
  expect(callbacks.onPreview).toHaveBeenCalledOnce();
});

it('switches source adapters without hiding the fail-closed network explanation', async () => {
  /** 验证 HTTPS 与上传适配器可选，远程抓取边界在操作前保持可见。 */

  const user = userEvent.setup();
  const callbacks = renderDialog();
  expect(screen.getByText(/每次重定向都会重新检查 HTTPS 主机与 DNS/)).toBeVisible();

  await user.click(screen.getByRole('tab', { name: 'HTTPS ZIP' }));
  expect(callbacks.onSourceKindChange).toHaveBeenCalledWith('https');
  await user.click(screen.getByRole('tab', { name: 'SkillHub' }));
  expect(callbacks.onSourceKindChange).toHaveBeenCalledWith('skillhub');
  await user.click(screen.getByRole('tab', { name: '上传文件' }));
  expect(callbacks.onSourceKindChange).toHaveBeenCalledWith('upload');
});

it('offers a directory picker that uses the same fail-closed preview action', () => {
  /** 验证文件夹不是旧编辑器旁路，而是安全导入对话框中的明确来源适配器。 */

  renderDialog('folder');
  expect(screen.getByText('选择完整 Skill 文件夹')).toBeVisible();
  expect(document.querySelector('input[webkitdirectory]')).toBeInstanceOf(HTMLInputElement);
  expect(screen.getByRole('button', { name: '生成安全预览' })).toBeDisabled();
  expect(screen.getByText(/SKILL.md、ZIP 与文件夹共用完整检查/)).toBeVisible();
});

it('accepts a single SKILL.md through the reviewed upload adapter', () => {
  /** 验证单文件 Skill 与 ZIP 共用一个用户入口及安全预览动作。 */

  renderDialog('upload');
  expect(screen.getByText('选择 SKILL.md 或 ZIP Skill 包')).toBeVisible();
  const input = document.querySelector('input[type="file"]');
  expect(input).toHaveAttribute('accept', expect.stringContaining('.md'));
});

it('hides arbitrary HTTPS when the deployment capability does not allow it', () => {
  /** 验证生产端未配置主机白名单时不会向用户展示任意 HTTPS 来源入口。 */

  const callbacks = {
    onFileChange: vi.fn(), onFolderFilesChange: vi.fn(), onSourceKindChange: vi.fn(),
    onSourceUrlChange: vi.fn(), onRevisionChange: vi.fn(), onSourceSubpathChange: vi.fn(),
    onSelectedIdsChange: vi.fn(), onDependencyDecisionChange: vi.fn(), onPreview: vi.fn(),
    onConfirm: vi.fn(), onReset: vi.fn(), onClose: vi.fn(),
  };
  render(
    <I18nProvider>
      <SecureSkillImportDialog
        open loading={false} availableSourceKinds={['upload', 'github']} sourceKind="upload"
        file={null} folderFiles={[]} sourceUrl="" revision="" sourceSubpath="skills"
        job={null} selectedIds={[]} dependencyDecisions={{}} {...callbacks}
      />
    </I18nProvider>,
  );
  expect(screen.queryByRole('tab', { name: 'HTTPS ZIP' })).not.toBeInTheDocument();
  expect(screen.getByRole('tab', { name: '上传文件' })).toBeVisible();
});

it('shows a resumable background-processing state without exposing confirm action', () => {
  /** 验证异步作业尚未完成检查时有明确反馈，且不能越过预览直接绑定。 */

  renderDialog('upload', {
    id: 'gsjob_processing', tenant_id: 'tenant-a', target_agent_id: 'agent-a',
    source_kind: 'upload', status: 'fetched', attempt: 1, quota_bytes: 128,
    candidates: [], expires_at: '2026-08-13T00:00:00Z', row_version: 2,
    installed_revision_ids: [],
  });

  expect(screen.getByRole('status')).toHaveTextContent('后台正在安全检查 Skill 包');
  expect(screen.getByText(/关闭不会丢失作业/)).toBeVisible();
  expect(screen.queryByRole('button', { name: '固定版本并绑定' })).not.toBeInTheDocument();
});

it('offers user-owned private credentials without rendering the token as plain text', () => {
  /** 验证远程来源可选择本人凭据，Token 输入使用密码语义且公开来源仍是默认值。 */

  renderDialog('github');

  expect(screen.getByLabelText('本次导入使用的私有来源凭据')).toHaveValue('');
  expect(screen.getByLabelText('私有来源 Token')).toHaveAttribute('type', 'password');
  expect(screen.getByRole('option', { name: '公开来源（不发送 Token）' })).toBeVisible();
  expect(screen.getByRole('button', { name: '加密保存并用于本次导入' })).toBeDisabled();
});
