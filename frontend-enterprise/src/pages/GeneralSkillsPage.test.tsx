import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';

import { I18nProvider } from '../i18n';
import { SecureSkillImportDialog } from './GeneralSkillsPage';

function renderDialog(sourceKind: 'upload' | 'folder' | 'github' | 'https' = 'github') {
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
        job={null}
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
  await user.click(screen.getByRole('tab', { name: '上传 ZIP' }));
  expect(callbacks.onSourceKindChange).toHaveBeenCalledWith('upload');
});

it('offers a directory picker that uses the same fail-closed preview action', () => {
  /** 验证文件夹不是旧编辑器旁路，而是安全导入对话框中的明确来源适配器。 */

  renderDialog('folder');
  expect(screen.getByText('选择完整 Skill 文件夹')).toBeVisible();
  expect(document.querySelector('input[webkitdirectory]')).toBeInstanceOf(HTMLInputElement);
  expect(screen.getByRole('button', { name: '生成安全预览' })).toBeDisabled();
  expect(screen.getByText(/ZIP 与文件夹共用完整检查/)).toBeVisible();
});
