import { useState, type KeyboardEvent } from 'react';

import { api, LOGIN_TENANT_ID } from '../api/client';
import { setEnterpriseAuthSession, type EnterpriseAuthSession } from '../auth';
import AppHeader from '../components/AppHeader';
import BrandLogo from '../components/BrandLogo';
import IconFieldClear from '../assets/icons/field-clear.svg?react';
import IconFieldEye from '../assets/icons/field-eye.svg?react';
import IconFieldEyeOn from '../assets/icons/field-eye-on.svg?react';
import CapabilityPipeline from '../components/login/CapabilityPipeline';

export type LoginPageProps = {
  onLogin: (session: EnterpriseAuthSession) => void;
};

/**
 * Signed-out landing and login page for the 共格·序伴 brand family.
 * The capability blueprint replaces the former mock-product screenshot while the
 * existing credential flow and API contract remain unchanged.
 */
export default function LoginPage({ onLogin }: LoginPageProps) {
  const [showForm, setShowForm] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [usernameError, setUsernameError] = useState('');
  const [passwordError, setPasswordError] = useState('');
  const [loading, setLoading] = useState(false);

  async function login() {
    const trimmedUsername = username.trim();
    const trimmedPassword = password.trim();
    setUsernameError(trimmedUsername ? '' : '请输入账号');
    setPasswordError(trimmedPassword ? '' : '请输入密码');
    if (!trimmedUsername || !trimmedPassword) return;

    setLoading(true);
    try {
      const session = await api.post<EnterpriseAuthSession>('/api/auth/login', {
        tenant_id: LOGIN_TENANT_ID,
        username: trimmedUsername,
        password: trimmedPassword,
      });
      setEnterpriseAuthSession(session);
      onLogin(session);
    } catch (error) {
      const messageText = error instanceof Error ? error.message : '登录失败';
      setUsernameError('账号输入错误');
      setPasswordError(messageText || '密码输入错误');
    } finally {
      setLoading(false);
    }
  }

  function onFieldKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter') void login();
  }

  const inputBaseClass =
    'flex h-[46px] w-full items-center gap-[8px] rounded-[12px] border bg-white/96 px-[16px] transition-colors';

  return (
    <div className="gongge-login gg-typography-scope relative flex min-h-screen flex-col overflow-hidden text-white" data-typography-contract="v1">
      <div className="gongge-login-aurora" aria-hidden="true" />
      <AppHeader
        className="relative z-20 h-[72px] shrink-0 items-center border-b border-white/10 px-[clamp(20px,4vw,64px)]"
        left={<BrandLogo context="login" markSize={32} className="[&_strong]:text-white!" />}
        right={<span className="hidden gg-type-meta tracking-[0.14em] text-blue-100/75 sm:inline">中国联通团队打造</span>}
      />

      <main className="relative z-10 mx-auto grid w-full max-w-[1480px] flex-1 items-center gap-[clamp(42px,6vw,96px)] px-[clamp(20px,5vw,76px)] py-[48px] lg:grid-cols-[minmax(360px,0.82fr)_minmax(560px,1.18fr)]">
        <section className="gongge-login-copy max-w-[560px]">
          <span className="gongge-login-eyebrow"><i /> 企业数字员工平台</span>
          <h1 className="mt-[22px] gg-type-display font-semibold text-white">
            让经验<br />进入工作<span className="text-[#7BE7F5]">。</span>
          </h1>
          <p className="mt-[24px] max-w-[500px] gg-type-body text-blue-100/72">
            把岗位知识、流程、记忆与工具汇聚成可信赖的数字员工，帮助团队在真实业务中协同执行、持续沉淀。
          </p>

          {!showForm ? (
            <button
              type="button"
              onClick={() => setShowForm(true)}
              className="gongge-login-cta mt-[34px] flex h-[48px] items-center justify-center rounded-[12px] bg-[#3157E8] px-[32px] gg-type-body font-semibold text-white shadow-[0_14px_32px_rgba(17,56,188,0.38)] hover:-translate-y-0.5 hover:bg-[#3d66f0]"
            >
              进入平台
              <span aria-hidden="true" className="ml-[18px] gg-type-section-title">→</span>
            </button>
          ) : (
            <form
              className="mt-[32px] flex w-full max-w-[380px] flex-col rounded-[20px] border border-white/14 bg-white/8 p-[18px] shadow-[0_24px_64px_rgba(0,10,42,0.24)] backdrop-blur-xl duration-300 ease-out animate-in fade-in slide-in-from-top-4"
              onSubmit={(event) => {
                event.preventDefault();
                void login();
              }}
            >
              <div
                className={`${inputBaseClass} ${usernameError ? 'border-[#f54a45]' : username ? 'border-[#7BE7F5]' : 'border-white/18'}`}
              >
                <input
                  value={username}
                  autoComplete="username"
                  placeholder="请输入账号（首次使用请输入admin）"
                  aria-label="账号"
                  onChange={(event) => {
                    setUsername(event.target.value);
                    if (usernameError) setUsernameError('');
                  }}
                  onKeyDown={onFieldKeyDown}
                  className="min-w-0 flex-1 border-0 bg-transparent gg-type-body text-[#18213D] outline-none placeholder:text-[#68738E]"
                />
                {username && (
                  <button
                    type="button"
                    aria-label="清空账号"
                    onClick={() => {
                      setUsername('');
                      setUsernameError('');
                    }}
                    className="grid size-[18px] shrink-0 place-items-center text-[#68738E] outline-none transition-colors hover:text-[#18213D]"
                  >
                    <IconFieldClear className="size-[18px]" />
                  </button>
                )}
              </div>

              <div
                className={`mt-[14px] ${inputBaseClass} ${passwordError ? 'border-[#f54a45]' : password ? 'border-[#7BE7F5]' : 'border-white/18'}`}
              >
                <input
                  value={password}
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  placeholder="请输入密码（首次使用请输入admin）"
                  aria-label="密码"
                  onChange={(event) => {
                    setPassword(event.target.value);
                    if (passwordError) setPasswordError('');
                  }}
                  onKeyDown={onFieldKeyDown}
                  className="min-w-0 flex-1 border-0 bg-transparent gg-type-body text-[#18213D] outline-none placeholder:text-[#68738E]"
                />
                <button
                  type="button"
                  aria-label={showPassword ? '隐藏密码' : '显示密码'}
                  onClick={() => setShowPassword((prev) => !prev)}
                  className="grid size-[18px] shrink-0 place-items-center text-[#68738E] outline-none transition-colors hover:text-[#18213D]"
                >
                  {showPassword ? (
                    <IconFieldEyeOn className="size-[18px]" />
                  ) : (
                    <IconFieldEye className="size-[18px]" />
                  )}
                </button>
              </div>

              {(passwordError || usernameError) && (
                <p role="alert" className="mt-[8px] gg-type-meta  text-[#ffb4b1]">
                  {passwordError || usernameError}
                </p>
              )}

              <button
                type="submit"
                disabled={loading}
                className="mt-[18px] flex h-[46px] w-full items-center justify-center rounded-[12px] bg-[#3157E8] gg-type-body font-semibold text-white transition-colors hover:bg-[#3d66f0] disabled:cursor-not-allowed disabled:opacity-60"
              >
                {loading ? '登录中…' : '登录'}
              </button>
            </form>
          )}
          <div className="mt-[38px] flex flex-wrap gap-x-[24px] gap-y-[10px] gg-type-meta tracking-[0.03em] text-blue-100/55">
            <span>经验可复用</span><span>过程可治理</span><span>结果可追溯</span>
          </div>
        </section>

        <CapabilityPipeline />
      </main>
    </div>
  );
}
