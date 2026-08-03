import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import { I18nProvider } from '../i18n';
import LoginPage from './LoginPage';

vi.mock('../api/client', () => ({
  LOGIN_TENANT_ID: 'test-tenant',
  api: { post: vi.fn() },
}));

beforeEach(() => {
  vi.mocked(api.post).mockReset();
});

function renderLoginPage(onLogin = vi.fn()) {
  return render(
    <I18nProvider>
      <LoginPage onLogin={onLogin} />
    </I18nProvider>,
  );
}

it('reveals the existing credential form from the primary CTA', async () => {
  const user = userEvent.setup();
  renderLoginPage();

  await user.click(screen.getByRole('button', { name: '进入平台' }));

  expect(screen.getByRole('textbox', { name: '账号' })).toBeInTheDocument();
  expect(screen.getByLabelText('密码')).toBeInTheDocument();
});

it('submits credentials through the existing login API', async () => {
  const user = userEvent.setup();
  const onLogin = vi.fn();
  const session = { access_token: 'token', user: { id: '1' } };
  vi.mocked(api.post).mockResolvedValue(session);
  renderLoginPage(onLogin);

  await user.click(screen.getByRole('button', { name: '进入平台' }));
  await user.type(screen.getByRole('textbox', { name: '账号' }), 'admin');
  await user.type(screen.getByLabelText('密码'), 'admin');
  await user.click(screen.getByRole('button', { name: '登录' }));

  expect(api.post).toHaveBeenCalledWith('/api/auth/login', {
    tenant_id: 'test-tenant',
    username: 'admin',
    password: 'admin',
  });
  expect(onLogin).toHaveBeenCalledWith(session);
});
