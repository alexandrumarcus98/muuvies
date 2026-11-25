import { FormEvent, useEffect, useMemo, useState } from 'react';
import { ApiError, apiFetch } from '../lib/api';
import {
  isPasskeySupported,
  normalizeWebAuthnError,
  prepareRequestOptions,
  serializeAuthenticationCredential,
} from '../lib/passkeys';
import type { AuthActionResponse, ThemeMode } from '../types/api';

type AuthMode = 'login' | 'register' | 'forgot' | 'reset';

interface AuthPageProps {
  allowRegistration: boolean;
  inviteRequired: boolean;
  theme: ThemeMode;
  onToggleTheme: () => void;
  onAuthenticated: () => Promise<void>;
}

interface PasskeyLoginOptionsResponse {
  challenge_id: string;
  options: Record<string, unknown>;
}

function getSafeNextUrl(): string {
  const params = new URLSearchParams(window.location.search);
  const raw = params.get('next') || '/';
  if (
    raw.startsWith('/') &&
    !raw.startsWith('//') &&
    !raw.startsWith('/login') &&
    !raw.startsWith('/register') &&
    !raw.startsWith('/reset-password')
  ) {
    return raw;
  }
  return '/';
}

function getInitialMode(allowRegistration: boolean): AuthMode {
  const path = window.location.pathname;
  if (path.startsWith('/reset-password')) return 'reset';
  if (path.startsWith('/register') && allowRegistration) return 'register';
  return 'login';
}

async function postJsonWithStatus(
  path: string,
  body?: unknown,
): Promise<{ ok: boolean; status: number; data: AuthActionResponse | null }> {
  const resp = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });

  let data: AuthActionResponse | null = null;
  try {
    data = (await resp.json()) as AuthActionResponse;
  } catch {
    data = null;
  }

  return { ok: resp.ok, status: resp.status, data };
}

export default function AuthPage({
  allowRegistration,
  inviteRequired,
  theme,
  onToggleTheme,
  onAuthenticated,
}: AuthPageProps) {
  const initialParams = useMemo(() => new URLSearchParams(window.location.search), []);
  const [mode, setMode] = useState<AuthMode>(() => getInitialMode(allowRegistration));
  const [resetToken, setResetToken] = useState<string>(() => initialParams.get('token') || '');

  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [inviteCode, setInviteCode] = useState('');
  const [showPassword, setShowPassword] = useState(false);

  const [loading, setLoading] = useState(false);
  const [passkeyLoading, setPasskeyLoading] = useState(false);
  const [error, setError] = useState<string>(() => {
    if (initialParams.get('verify_error') === '1') {
      return 'Verification link is invalid or has expired. Please request a new one.';
    }
    if (window.location.pathname.startsWith('/reset-password') && !initialParams.get('token')) {
      return 'Password reset link is missing or invalid.';
    }
    return '';
  });
  const [verifiedBanner, setVerifiedBanner] = useState<boolean>(
    () => initialParams.get('verified') === '1',
  );
  const [verificationSentEmail, setVerificationSentEmail] = useState('');
  const [showResend, setShowResend] = useState<boolean>(
    () => initialParams.get('verify_error') === '1',
  );
  const [resendEmail, setResendEmail] = useState('');
  const [resendLoading, setResendLoading] = useState(false);
  const [statusMessage, setStatusMessage] = useState('');

  const passkeySupported = isPasskeySupported();

  useEffect(() => {
    if (!allowRegistration && mode === 'register') {
      setMode('login');
    }
  }, [allowRegistration, mode]);

  const switchMode = (nextMode: AuthMode, keepStatus = false) => {
    if (nextMode === 'register' && !allowRegistration) return;
    setMode(nextMode);
    setError('');
    setVerifiedBanner(false);
    setVerificationSentEmail('');
    setShowResend(false);
    if (!keepStatus) setStatusMessage('');
    setShowPassword(false);
  };

  const completeSignIn = async () => {
    await onAuthenticated();
    window.location.assign(getSafeNextUrl());
  };

  const handleResendVerification = async (e: FormEvent) => {
    e.preventDefault();
    const targetEmail = (resendEmail || verificationSentEmail).trim();
    if (!targetEmail || !targetEmail.includes('@')) {
      setError('Please enter the email address associated with your account.');
      return;
    }
    if (resendLoading) return;

    setResendLoading(true);
    setStatusMessage('');
    try {
      const res = await apiFetch<AuthActionResponse>('/api/auth/resend-verification', {
        method: 'POST',
        body: JSON.stringify({ email: targetEmail }),
      });
      setError('');
      setStatusMessage(res.message || 'Verification email sent. Please check your inbox.');
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : 'Could not resend verification email right now.',
      );
    } finally {
      setResendLoading(false);
    }
  };

  const handlePasskeyLogin = async () => {
    if (!passkeySupported || passkeyLoading || loading) return;
    setPasskeyLoading(true);
    setError('');
    setVerifiedBanner(false);
    setShowResend(false);
    setStatusMessage('');

    try {
      const optData = await apiFetch<PasskeyLoginOptionsResponse>('/api/auth/passkeys/login/options', {
        method: 'POST',
      });

      const credential = await navigator.credentials.get({
        publicKey: prepareRequestOptions(optData.options),
      });

      if (!credential || !(credential instanceof PublicKeyCredential)) {
        setError('No passkey credential selected.');
        return;
      }

      const { ok, status, data } = await postJsonWithStatus('/api/auth/passkeys/login/verify', {
        challenge_id: optData.challenge_id,
        credential: serializeAuthenticationCredential(credential),
      });

      if (ok) {
        await completeSignIn();
        return;
      }

      if (status === 403 && data?.code === 'EMAIL_UNVERIFIED') {
        setShowResend(true);
        setError('Email not verified. Check your inbox or resend the verification email.');
        return;
      }

      setError(data?.detail || 'Passkey sign-in failed.');
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError(normalizeWebAuthnError(err));
      }
    } finally {
      setPasskeyLoading(false);
    }
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (loading) return;

    setLoading(true);
    setError('');
    setVerifiedBanner(false);
    setVerificationSentEmail('');
    setShowResend(false);
    setStatusMessage('');

    try {
      if (mode === 'forgot') {
        const trimmedEmail = email.trim();
        const res = await apiFetch<AuthActionResponse>('/api/auth/forgot-password', {
          method: 'POST',
          body: JSON.stringify({ email: trimmedEmail }),
        });
        setStatusMessage(
          res.message || 'If an account matches that email, a password reset link has been sent.',
        );
        return;
      }

      if (mode === 'reset') {
        if (!resetToken) {
          setError('Password reset link is missing or invalid.');
          return;
        }
        if (password.length < 8) {
          setError('Password must be at least 8 characters long.');
          return;
        }
        if (confirmPassword && password !== confirmPassword) {
          setError('Passwords do not match.');
          return;
        }
        const res = await apiFetch<AuthActionResponse>('/api/auth/reset-password', {
          method: 'POST',
          body: JSON.stringify({ token: resetToken, new_password: password }),
        });
        setPassword('');
        setConfirmPassword('');
        setResetToken('');
        setStatusMessage(res.message || 'Password reset successfully. Please sign in.');
        switchMode('login', true);
        if (window.location.pathname.startsWith('/reset-password')) {
          window.history.replaceState({}, '', '/login');
        }
        return;
      }

      if (mode === 'register') {
        const payload: Record<string, string> = {
          username: username.trim(),
          password,
          email: email.trim(),
          invite_code: inviteCode.trim(),
        };
        const res = await apiFetch<AuthActionResponse>('/api/auth/register', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        if (res.status === 'verification_sent') {
          const sentTo = res.email || payload.email;
          setVerificationSentEmail(sentTo);
          setResendEmail(sentTo);
          setPassword('');
          return;
        }
        await completeSignIn();
        return;
      }

      // mode === 'login'
      const { ok, status, data } = await postJsonWithStatus('/api/auth/login', {
        username: username.trim(),
        password,
      });

      if (ok) {
        await completeSignIn();
        return;
      }

      if (status === 403 && data?.code === 'EMAIL_UNVERIFIED') {
        setShowResend(true);
        setResendEmail(username.includes('@') ? username.trim() : email.trim());
        setError('Email not verified. Check your inbox or resend the verification email.');
        return;
      }

      setError(data?.detail || 'Authentication failed. Please check your credentials.');
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : 'Network error connecting to authentication service.',
      );
    } finally {
      setLoading(false);
    }
  };

  const heading =
    mode === 'register'
      ? 'Create an account'
      : mode === 'forgot'
        ? 'Reset your password'
        : mode === 'reset'
          ? 'Choose a new password'
          : 'Sign in';

  const subtitle =
    mode === 'register'
      ? 'You will need an approved email address.'
      : mode === 'forgot'
        ? 'We will email you a link. It works for 30 minutes.'
        : mode === 'reset'
          ? 'At least 8 characters.'
          : 'Access your private cinema workspace.';

  const submitLabel = loading
    ? 'Submitting…'
    : mode === 'register'
      ? 'Create account'
      : mode === 'forgot'
        ? 'Send reset link'
        : mode === 'reset'
          ? 'Update password'
          : 'Sign in';

  return (
    <div className="bp-shell">
      <header className="bp-nav">
        <a href="/" className="bp-brand">
          <span className="bp-brand-dot" aria-hidden="true" />
          <span>Muuvies</span>
        </a>
        <nav className="bp-nav-actions" aria-label="Authentication navigation">
          <button
            type="button"
            onClick={onToggleTheme}
            className="bp-pill bp-pill-ghost"
            aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
          >
            {theme === 'dark' ? 'Light' : 'Dark'}
          </button>
          <a href="/" className="bp-pill bp-pill-ghost">
            Home
          </a>
        </nav>
      </header>

      <main className="bp-container" style={{ maxWidth: 520 }}>
        <section className="bp-card">
          <p className="bp-kicker" style={{ marginBottom: '0.5rem' }}>
            Access
          </p>
          <h1 className="bp-slider-title" style={{ fontSize: 'clamp(1.75rem, 4vw, 2.5rem)' }}>
            {heading}
          </h1>
          <p className="bp-muted" style={{ marginTop: 0, marginBottom: '1.25rem' }}>
            {subtitle}
          </p>

          {(mode === 'login' || (mode === 'register' && allowRegistration)) && (
            <div className="bp-nav-actions" style={{ marginBottom: '1.25rem' }}>
              <button
                type="button"
                className={`bp-pill ${mode === 'login' ? 'bp-pill-active' : 'bp-pill-ghost'}`}
                onClick={() => switchMode('login')}
              >
                Sign in
              </button>
              {allowRegistration && (
                <button
                  type="button"
                  className={`bp-pill ${mode === 'register' ? 'bp-pill-active' : 'bp-pill-ghost'}`}
                  onClick={() => switchMode('register')}
                >
                  Register
                </button>
              )}
            </div>
          )}

          {verifiedBanner && (
            <div
              className="bp-alert"
              role="status"
              style={{ borderColor: 'var(--bp-ok)', background: 'rgba(46, 125, 84, 0.12)', marginBottom: '1rem' }}
            >
              Email verified. You can sign in now.
            </div>
          )}

          {verificationSentEmail && (
            <div
              className="bp-alert"
              role="status"
              style={{ borderColor: 'var(--bp-ok)', background: 'rgba(46, 125, 84, 0.12)', marginBottom: '1rem' }}
            >
              Verification link sent to <strong>{verificationSentEmail}</strong>. Check your inbox before signing in.
            </div>
          )}

          {statusMessage && (
            <div
              className="bp-alert"
              role="status"
              style={{ borderColor: 'var(--bp-ok)', background: 'rgba(46, 125, 84, 0.12)', marginBottom: '1rem' }}
            >
              {statusMessage}
            </div>
          )}

          {error && (
            <div className="bp-alert" role="alert" style={{ marginBottom: '1rem' }}>
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="bp-list">
            {(mode === 'login' || mode === 'register') && (
              <div>
                <label htmlFor="auth-username" className="bp-kicker" style={{ display: 'block', marginBottom: '0.4rem' }}>
                  Username
                </label>
                <input
                  id="auth-username"
                  type="text"
                  className="bp-input"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoComplete="username"
                  required
                />
              </div>
            )}

            {(mode === 'register' || mode === 'forgot') && (
              <div>
                <label htmlFor="auth-email" className="bp-kicker" style={{ display: 'block', marginBottom: '0.4rem' }}>
                  Email
                </label>
                <input
                  id="auth-email"
                  type="email"
                  className="bp-input"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  autoComplete="email"
                  required
                />
              </div>
            )}

            {(mode === 'login' || mode === 'register' || mode === 'reset') && (
              <div>
                <div className="bp-row" style={{ marginBottom: '0.4rem' }}>
                  <label htmlFor="auth-password" className="bp-kicker">
                    {mode === 'reset' ? 'New password' : 'Password'}
                  </label>
                  <button
                    type="button"
                    className="bp-tag"
                    style={{ background: 'transparent', color: 'var(--bp-muted)', cursor: 'pointer' }}
                    onClick={() => setShowPassword((prev) => !prev)}
                  >
                    {showPassword ? 'Hide' : 'Show'}
                  </button>
                </div>
                <input
                  id="auth-password"
                  type={showPassword ? 'text' : 'password'}
                  className="bp-input"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                  minLength={mode === 'reset' ? 8 : undefined}
                  required
                />
              </div>
            )}

            {mode === 'reset' && (
              <div>
                <label htmlFor="auth-confirm-password" className="bp-kicker" style={{ display: 'block', marginBottom: '0.4rem' }}>
                  Confirm password
                </label>
                <input
                  id="auth-confirm-password"
                  type={showPassword ? 'text' : 'password'}
                  className="bp-input"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  autoComplete="new-password"
                  minLength={8}
                  required
                />
              </div>
            )}

            {mode === 'register' && (inviteRequired || inviteCode.length > 0) && (
              <div>
                <label htmlFor="auth-invite" className="bp-kicker" style={{ display: 'block', marginBottom: '0.4rem' }}>
                  Invite code{inviteRequired ? '' : ' (optional)'}
                </label>
                <input
                  id="auth-invite"
                  type="text"
                  className="bp-input"
                  value={inviteCode}
                  onChange={(e) => setInviteCode(e.target.value)}
                  required={inviteRequired}
                />
              </div>
            )}

            <button
              type="submit"
              disabled={loading || passkeyLoading}
              className="bp-pill bp-pill-solid"
              style={{ width: '100%', height: 48, marginTop: '0.25rem' }}
            >
              {submitLabel}
            </button>
          </form>

          {mode === 'login' && passkeySupported && (
            <>
              <hr className="bp-hr" />
              <button
                type="button"
                disabled={passkeyLoading || loading}
                onClick={handlePasskeyLogin}
                className="bp-pill bp-pill-ghost"
                style={{ width: '100%', height: 48 }}
              >
                {passkeyLoading ? 'Waiting for passkey…' : 'Sign in with passkey'}
              </button>
            </>
          )}

          {(showResend || Boolean(verificationSentEmail)) && (
            <>
              <hr className="bp-hr" />
              <form onSubmit={handleResendVerification} className="bp-list">
                <p className="bp-kicker">Resend verification email</p>
                <input
                  type="email"
                  className="bp-input"
                  placeholder="you@example.com"
                  value={resendEmail}
                  onChange={(e) => setResendEmail(e.target.value)}
                  required
                />
                <button
                  type="submit"
                  disabled={resendLoading}
                  className="bp-pill bp-pill-ghost"
                >
                  {resendLoading ? 'Sending…' : 'Resend verification'}
                </button>
              </form>
            </>
          )}

          <hr className="bp-hr" />
          <div className="bp-row">
            {mode === 'login' ? (
              <button
                type="button"
                className="bp-pill bp-pill-ghost"
                onClick={() => switchMode('forgot')}
              >
                Forgot password?
              </button>
            ) : (
              <button
                type="button"
                className="bp-pill bp-pill-ghost"
                onClick={() => switchMode('login')}
              >
                ← BACK TO SIGN IN
              </button>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
