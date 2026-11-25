import { Suspense, lazy, useCallback, useEffect, useState } from 'react';
import { ApiError, apiFetch } from './lib/api';
import type { AuthConfigResponse, AuthMeResponse, ThemeMode } from './types/api';

const AuthPage = lazy(() => import('./components/AuthPage'));
const LandingPage = lazy(() => import('./components/LandingPage'));
const WorkspaceShell = lazy(() => import('./components/WorkspaceShell'));

const DEFAULT_AUTH_CONFIG: AuthConfigResponse = {
  allow_registration: false,
  invite_required: false,
  vault_timeout_minutes: 15,
};

function readStoredTheme(): ThemeMode {
  try {
    return localStorage.getItem('theme') === 'light' ? 'light' : 'dark';
  } catch {
    return 'dark';
  }
}

function isAuthRoute(pathname: string): boolean {
  return (
    pathname.startsWith('/login') ||
    pathname.startsWith('/register') ||
    pathname.startsWith('/reset-password')
  );
}

export default function App() {
  const [auth, setAuth] = useState<AuthMeResponse | null>(null);
  const [authConfig, setAuthConfig] = useState<AuthConfigResponse>(DEFAULT_AUTH_CONFIG);
  const [theme, setTheme] = useState<ThemeMode>(readStoredTheme);
  const [pathname, setPathname] = useState(() => window.location.pathname);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme !== 'light');
    try {
      localStorage.setItem('theme', theme);
    } catch {
      // ignore storage errors
    }
  }, [theme]);

  useEffect(() => {
    const onPopState = () => setPathname(window.location.pathname);
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  const loadSession = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await apiFetch<AuthMeResponse>('/api/auth/me', { signal });
      setAuth(res);
    } catch (err) {
      if (signal?.aborted) return;
      if (err instanceof ApiError && err.status === 401) {
        setAuth({ authenticated: false });
      } else {
        setAuth({ authenticated: false });
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    void loadSession(controller.signal);

    apiFetch<AuthConfigResponse>('/api/auth/config', { signal: controller.signal })
      .then((cfg) => setAuthConfig(cfg))
      .catch(() => {});

    return () => controller.abort();
  }, [loadSession]);

  const handleToggleTheme = useCallback(() => {
    setTheme((prev) => (prev === 'light' ? 'dark' : 'light'));
  }, []);

  const handleAuthenticated = useCallback(async () => {
    await loadSession();
    if (isAuthRoute(window.location.pathname)) {
      window.history.replaceState({}, '', '/');
      setPathname('/');
    }
  }, [loadSession]);

  const handleLoggedOut = useCallback(() => {
    setAuth({ authenticated: false });
    if (window.location.pathname !== '/') {
      window.history.replaceState({}, '', '/');
      setPathname('/');
    }
  }, []);

  if (!auth) {
    return <div className="bp-shell" aria-busy="true" />;
  }

  const onAuthRoute = isAuthRoute(pathname);

  return (
    <Suspense fallback={<div className="bp-shell" aria-busy="true" />}>
      {onAuthRoute && !auth.authenticated ? (
        <AuthPage
          allowRegistration={authConfig.allow_registration}
          inviteRequired={authConfig.invite_required}
          theme={theme}
          onToggleTheme={handleToggleTheme}
          onAuthenticated={handleAuthenticated}
        />
      ) : auth.authenticated ? (
        <WorkspaceShell
          auth={auth}
          authConfig={authConfig}
          theme={theme}
          onToggleTheme={handleToggleTheme}
          onLoggedOut={handleLoggedOut}
        />
      ) : (
        <LandingPage
          allowRegistration={authConfig.allow_registration}
          theme={theme}
          onToggleTheme={handleToggleTheme}
        />
      )}
    </Suspense>
  );
}
