import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from '../lib/api';
import type {
  AuthConfigResponse,
  AuthMeResponse,
  MaintenanceReport,
  PlaybackProgressItem,
  QueueTorrentItem,
  StreamManifestResponse,
  TelemetryResponse,
  ThemeMode,
  VaultStatusResponse,
  WorkspaceTab,
} from '../types/api';
import DownloadsView from './DownloadsView';
import IdleLibraryView from './IdleLibraryView';
import MaintenanceView from './MaintenanceView';
import AdminView from './AdminView';
import SearchView from './SearchView';
import TheaterModal from './TheaterModal';
import { LoadingBar } from './LoadingBar';
import { gsap, useGSAP, prefersReducedMotion } from '../motion/gsap';
import '../styles/motion.css';
import '../styles/workspace.css';
import {
  HistoryModal,
  PasskeysModal,
  SettingsModal,
  VaultModal,
} from './VaultPasskeyModals';

interface WorkspaceShellProps {
  auth: AuthMeResponse;
  authConfig: AuthConfigResponse;
  theme: ThemeMode;
  onToggleTheme: () => void;
  onLoggedOut: () => void;
}

interface ManifestCacheEntry {
  data: StreamManifestResponse;
  fetchedAt: number;
}

const POLL_INTERVAL_MS = 5000;
const MANIFEST_CACHE_TTL_MS = 60_000;

function readInitialQuery(): string {
  try {
    const fromUrl = new URLSearchParams(window.location.search).get('q')?.trim();
    if (fromUrl) {
      sessionStorage.removeItem('muuvies_pending_q');
      return fromUrl;
    }
    const pending = sessionStorage.getItem('muuvies_pending_q')?.trim() || '';
    if (pending) {
      sessionStorage.removeItem('muuvies_pending_q');
    }
    return pending;
  } catch {
    return '';
  }
}

function formatCountdown(totalSeconds: number): string {
  const clamped = Math.max(0, Math.floor(totalSeconds));
  const mins = Math.floor(clamped / 60);
  const secs = clamped % 60;
  return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

export default function WorkspaceShell({
  auth,
  authConfig,
  theme,
  onToggleTheme,
  onLoggedOut,
}: WorkspaceShellProps) {
  const isAdmin = auth.role === 'admin';

  const [activeTab, setActiveTab] = useState<Exclude<WorkspaceTab, 'settings'>>('search');
  const [searchQuery, setSearchQuery] = useState<string>(readInitialQuery);
  const [searchRunKey, setSearchRunKey] = useState(0);

  const [telemetry, setTelemetry] = useState<TelemetryResponse | null>(null);
  const [queue, setQueue] = useState<QueueTorrentItem[]>([]);
  const [queueLoading, setQueueLoading] = useState(true);
  const [playbackItems, setPlaybackItems] = useState<PlaybackProgressItem[]>([]);

  const [maintenanceReport, setMaintenanceReport] = useState<MaintenanceReport | null>(null);
  const [maintenanceLoading, setMaintenanceLoading] = useState(false);

  const [vaultUnlocked, setVaultUnlocked] = useState<boolean>(Boolean(auth.vault_unlocked));
  const [vaultExpiresIn, setVaultExpiresIn] = useState<number>(0);

  const [theaterTarget, setTheaterTarget] = useState<{ itemId: string; title: string } | null>(null);
  const [vaultModalOpen, setVaultModalOpen] = useState(false);
  const [passkeysModalOpen, setPasskeysModalOpen] = useState(false);
  const [historyModalOpen, setHistoryModalOpen] = useState(false);
  const [settingsModalOpen, setSettingsModalOpen] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);

  const manifestCacheRef = useRef<Map<string, ManifestCacheEntry>>(new Map());
  const manifestInFlightRef = useRef<Map<string, Promise<StreamManifestResponse | null>>>(new Map());

  const fetchTelemetryAndQueue = useCallback(async (signal?: AbortSignal) => {
    const [telRes, queueRes] = await Promise.allSettled([
      apiFetch<TelemetryResponse>('/api/telemetry', { signal }),
      apiFetch<QueueTorrentItem[]>('/api/queue', { signal }),
    ]);

    if (signal?.aborted) return;

    if (telRes.status === 'fulfilled') {
      setTelemetry(telRes.value);
      setVaultUnlocked(Boolean(telRes.value.vault_unlocked));
    }
    if (queueRes.status === 'fulfilled') {
      setQueue(Array.isArray(queueRes.value) ? queueRes.value : []);
    }
    setQueueLoading(false);
  }, []);

  const fetchPlaybackProgress = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await apiFetch<PlaybackProgressItem[] | { items?: PlaybackProgressItem[] }>(
        '/api/playback/progress',
        { signal },
      );
      if (signal?.aborted) return;
      const list = Array.isArray(res) ? res : res.items || [];
      setPlaybackItems(list);
    } catch {
      // ignore playback fetch errors
    }
  }, []);

  const fetchMaintenanceReport = useCallback(async (force = false) => {
    setMaintenanceLoading(true);
    try {
      // The backend parameter is `refresh`; `force` was ignored and served the 120 s cache.
      const path = force ? '/api/maintenance/status?refresh=true' : '/api/maintenance/status';
      const report = await apiFetch<MaintenanceReport>(path);
      setMaintenanceReport(report);
    } catch {
      // ignore maintenance errors
    } finally {
      setMaintenanceLoading(false);
    }
  }, []);

  const syncVaultStatus = useCallback(async (signal?: AbortSignal) => {
    try {
      const status = await apiFetch<VaultStatusResponse>('/api/vault/status', { signal });
      if (signal?.aborted) return;
      setVaultUnlocked(Boolean(status.unlocked));
      setVaultExpiresIn(status.unlocked ? Math.max(0, status.expires_in_seconds || 0) : 0);
    } catch {
      // 404 is expected when user is not vault-eligible (stealth mode)
    }
  }, []);

  // Poll /api/telemetry and /api/queue when document.hidden === false
  useEffect(() => {
    const controller = new AbortController();

    if (!document.hidden) {
      void fetchTelemetryAndQueue(controller.signal);
    }
    void fetchPlaybackProgress(controller.signal);
    void syncVaultStatus(controller.signal);

    const intervalId = window.setInterval(() => {
      if (document.hidden === false) {
        void fetchTelemetryAndQueue(controller.signal);
      }
    }, POLL_INTERVAL_MS);

    const onVisibilityChange = () => {
      if (document.hidden === false) {
        void fetchTelemetryAndQueue(controller.signal);
        void syncVaultStatus(controller.signal);
      }
    };

    document.addEventListener('visibilitychange', onVisibilityChange);

    return () => {
      controller.abort();
      window.clearInterval(intervalId);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [fetchTelemetryAndQueue, fetchPlaybackProgress, syncVaultStatus]);

  // Fetch maintenance report when switching to maintenance tab if not loaded yet
  useEffect(() => {
    if (activeTab === 'maintenance' && !maintenanceReport && !maintenanceLoading) {
      void fetchMaintenanceReport(false);
    }
  }, [activeTab, maintenanceReport, maintenanceLoading, fetchMaintenanceReport]);

  // Vault countdown timer
  useEffect(() => {
    if (!vaultUnlocked || vaultExpiresIn <= 0) return;

    const timerId = window.setInterval(() => {
      setVaultExpiresIn((prev) => {
        if (prev <= 1) {
          setVaultUnlocked(false);
          return 0;
        }
        return prev - 1;
      });
    }, 1000);

    return () => window.clearInterval(timerId);
  }, [vaultUnlocked, vaultExpiresIn > 0]);

  const getCachedManifest = useCallback((itemId: string): StreamManifestResponse | null => {
    const entry = manifestCacheRef.current.get(itemId);
    if (!entry) return null;
    if (Date.now() - entry.fetchedAt > MANIFEST_CACHE_TTL_MS) {
      manifestCacheRef.current.delete(itemId);
      return null;
    }
    return entry.data;
  }, []);

  const handlePrefetchManifest = useCallback(
    (itemId: string) => {
      if (!itemId || getCachedManifest(itemId)) return;
      if (manifestInFlightRef.current.has(itemId)) return;

      const req = apiFetch<StreamManifestResponse>(
        `/api/stream/${encodeURIComponent(itemId)}/manifest`,
      )
        .then((manifest) => {
          manifestCacheRef.current.set(itemId, {
            data: manifest,
            fetchedAt: Date.now(),
          });
          return manifest;
        })
        .catch(() => null)
        .finally(() => {
          manifestInFlightRef.current.delete(itemId);
        });

      manifestInFlightRef.current.set(itemId, req);
    },
    [getCachedManifest],
  );

  const handleOpenPlayer = useCallback(
    (itemId: string, title: string) => {
      handlePrefetchManifest(itemId);
      setTheaterTarget({ itemId, title });
    },
    [handlePrefetchManifest],
  );

  const handleRunSearch = useCallback((q: string) => {
    setSearchQuery(q);
    setSearchRunKey((k) => k + 1);
    setActiveTab('search');
  }, []);

  // Whenever the vault opens or closes, everything that depends on it is
  // dropped and fetched again. On lock, adult rows disappear at once instead
  // of lingering until the next poll.
  const prevVaultRef = useRef(vaultUnlocked);
  useEffect(() => {
    const was = prevVaultRef.current;
    prevVaultRef.current = vaultUnlocked;
    if (was === vaultUnlocked) return;
    if (!vaultUnlocked) {
      setTheaterTarget((target) => {
        if (!target) return target;
        const cached = getCachedManifest(target.itemId);
        const adultOrUnknown =
          target.itemId.startsWith('whisparr:') || (target.itemId.startsWith('qb:') && cached?.is_adult !== false);
        return adultOrUnknown ? null : target;
      });
      setPlaybackItems((items) =>
        items.filter((p) => !p.item_id.startsWith('whisparr:') && !p.item_id.startsWith('qb:')),
      );
      setHistoryModalOpen(false);
    }
    manifestCacheRef.current.clear();
    setMaintenanceReport(null);
    void fetchPlaybackProgress();
    void fetchTelemetryAndQueue();
  }, [vaultUnlocked, getCachedManifest, fetchPlaybackProgress, fetchTelemetryAndQueue]);

  const handleVaultChanged = useCallback(() => {
    void syncVaultStatus();
    void fetchTelemetryAndQueue();
    if (authConfig.vault_timeout_minutes > 0) {
      setVaultExpiresIn((prev) => (prev > 0 ? prev : authConfig.vault_timeout_minutes * 60));
    }
  }, [authConfig.vault_timeout_minutes, fetchTelemetryAndQueue, syncVaultStatus]);

  const handleLogout = useCallback(async () => {
    if (loggingOut) return;
    setLoggingOut(true);
    try {
      await apiFetch('/api/auth/logout', { method: 'POST' });
    } catch {
      // Clear local state regardless of network outcome
    } finally {
      manifestCacheRef.current.clear();
      manifestInFlightRef.current.clear();
      setTelemetry(null);
      setQueue([]);
      setPlaybackItems([]);
      setMaintenanceReport(null);
      setVaultUnlocked(false);
      setVaultExpiresIn(0);
      setTheaterTarget(null);
      setVaultModalOpen(false);
      setPasskeysModalOpen(false);
      setHistoryModalOpen(false);
      setSettingsModalOpen(false);
      setSearchQuery('');
      try {
        sessionStorage.removeItem('muuvies_pending_q');
      } catch {
        // ignore storage errors
      }
      setLoggingOut(false);
      onLoggedOut();
    }
  }, [loggingOut, onLoggedOut]);

  const activeDownloadsCount = telemetry?.active_torrents_count ?? queue.length;

  // Phones: sections live in the bottom bar and the account actions sit behind
  // a Menu button, instead of a header that wraps into ten pills.
  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [menuOpen]);

  // Short fade-and-rise when switching sections.
  const mainRef = useRef<HTMLElement>(null);
  useGSAP(
    () => {
      if (!mainRef.current || prefersReducedMotion()) return;
      gsap.fromTo(
        mainRef.current.children,
        { autoAlpha: 0, y: 16 },
        { autoAlpha: 1, y: 0, duration: 0.5, ease: 'expo.out', stagger: 0.06, clearProps: 'transform,opacity,visibility' },
      );
    },
    { dependencies: [activeTab], scope: mainRef },
  );

  return (
    <div className="bp-shell" style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      {/* Two-tier hairline header (matches malex-dashboard layout) */}
      <header className="ws-header">
        <div className="ws-top">
          <a
            href="/"
            className="bp-brand"
            onClick={(e) => {
              e.preventDefault();
              setActiveTab('search');
            }}
          >
            <span className="bp-brand-dot" aria-hidden="true" />
            <span>Muuvies</span>
          </a>

          <button
            type="button"
            className="ws-link ws-menu-btn"
            aria-expanded={menuOpen}
            aria-controls="ws-account-actions"
            onClick={() => setMenuOpen((v) => !v)}
          >
            {menuOpen ? 'Close' : 'Menu'}
          </button>

          <div
            id="ws-account-actions"
            className={`bp-nav-actions ws-actions${menuOpen ? ' is-open' : ''}`}
            onClick={(e) => {
              if ((e.target as HTMLElement).closest('button')) setMenuOpen(false);
            }}
          >
            <button
              type="button"
              className={`ws-link ws-vault${vaultUnlocked ? ' is-open' : ''}`}
              onClick={() => setVaultModalOpen(true)}
              title={vaultUnlocked ? 'Vault unlocked' : 'Unlock Vault'}
            >
              {vaultUnlocked
                ? `Vault open · ${vaultExpiresIn > 0 ? formatCountdown(vaultExpiresIn) : 'now'}`
                : 'Vault'}
            </button>

            <button
              type="button"
              className="ws-link"
              onClick={() => setHistoryModalOpen(true)}
            >
              History
            </button>

            <button
              type="button"
              className="ws-link"
              onClick={() => setPasskeysModalOpen(true)}
            >
              Passkeys
            </button>

            {isAdmin && (
              <button
                type="button"
                className="ws-link"
                onClick={() => setSettingsModalOpen(true)}
              >
                Settings
              </button>
            )}

            <button
              type="button"
              className="ws-link"
              onClick={onToggleTheme}
              aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} theme`}
            >
              {theme === 'light' ? 'Dark' : 'Light'}
            </button>

            <span className="ws-user" title={auth.email || auth.username || 'Member'}>
              {auth.username || 'Member'}
            </span>

            <button
              type="button"
              className="ws-link ws-signout"
              onClick={() => void handleLogout()}
              disabled={loggingOut}
            >
              {loggingOut ? 'Signing out…' : 'Sign out'}
            </button>
          </div>
        </div>

        <nav className="ws-subnav" aria-label="Workspace">
          <button
            type="button"
            className="ws-link"
            aria-current={activeTab === 'search' ? 'page' : undefined}
            onClick={() => setActiveTab('search')}
          >
            Search
          </button>
          <button
            type="button"
            className="ws-link"
            aria-current={activeTab === 'downloads' ? 'page' : undefined}
            onClick={() => setActiveTab('downloads')}
          >
            Downloads
            {activeDownloadsCount > 0 ? <span className="ws-count">{activeDownloadsCount}</span> : null}
          </button>
          <button
            type="button"
            className="ws-link"
            aria-current={activeTab === 'maintenance' ? 'page' : undefined}
            onClick={() => setActiveTab('maintenance')}
          >
            Maintenance
          </button>
          {isAdmin && (
            <button
              type="button"
              className="ws-link"
              aria-current={activeTab === 'admin' ? 'page' : undefined}
              onClick={() => setActiveTab('admin')}
            >
              Admin
            </button>
          )}
        </nav>
      </header>

      {/* Main Workspace Content */}
      <LoadingBar active={activeTab === 'maintenance' && maintenanceLoading} />

      <main ref={mainRef} className="bp-container" style={{ flex: 1, width: '100%', paddingBottom: '5.5rem' }}>
        {activeTab === 'search' && (
          <div className="bp-list" style={{ gap: '2rem' }}>
            <SearchView
              key={searchRunKey}
              vaultUnlocked={vaultUnlocked}
              initialQuery={searchQuery}
              onOpenPlayer={handleOpenPlayer}
              onPrefetchManifest={handlePrefetchManifest}
              onQueueChanged={() => void fetchTelemetryAndQueue()}
            />
            <IdleLibraryView
              vaultUnlocked={vaultUnlocked}
              playbackItems={playbackItems}
              onOpenPlayer={handleOpenPlayer}
              onPrefetchManifest={handlePrefetchManifest}
              onRunSearch={handleRunSearch}
            />
          </div>
        )}

        {activeTab === 'downloads' && (
          <DownloadsView
            queue={queue}
            loading={queueLoading}
            telemetry={telemetry}
            onRefresh={() => void fetchTelemetryAndQueue()}
            onOpenPlayer={handleOpenPlayer}
          />
        )}

        {activeTab === 'maintenance' && (
          <MaintenanceView
            report={maintenanceReport}
            loading={maintenanceLoading}
            onRefresh={(force) => void fetchMaintenanceReport(Boolean(force))}
            onQueueChanged={() => void fetchTelemetryAndQueue()}
          />
        )}

        {activeTab === 'admin' && isAdmin && (
          <AdminView
            currentUsername={auth.username}
            onOpenSettings={() => setSettingsModalOpen(true)}
          />
        )}
      </main>

      {/* Footer */}
      <footer
        style={{
          borderTop: '1px solid var(--bp-line)',
          padding: '1rem clamp(1rem, 4vw, 3rem) 5rem',
          fontSize: '12px',
          fontVariantNumeric: 'tabular-nums',
          color: 'var(--bp-muted)',
        }}
      >
        <div className="bp-row" style={{ maxWidth: 1240, margin: '0 auto' }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.75rem', alignItems: 'center' }}>
            <span>{telemetry?.seedbox_host || 'SEEDBOX'}</span>
            <span>·</span>
            <span>DL {telemetry?.download_speed_formatted ?? '0 B/s'}</span>
            <span>·</span>
            <span>UL {telemetry?.upload_speed_formatted ?? '0 B/s'}</span>
            <span>·</span>
            <span>Free {telemetry?.free_disk_space_formatted ?? '—'}</span>
          </div>

          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem', alignItems: 'center' }}>
            {(telemetry?.services || []).map((svc) => (
              <span
                key={svc.name}
                className="bp-tag"
                title={svc.detail || (svc.connected ? 'Connected' : 'Disconnected')}
                style={{
                  borderColor: svc.connected ? 'var(--bp-ok)' : 'var(--bp-red)',
                  color: svc.connected ? 'var(--bp-ok)' : 'var(--bp-red)',
                }}
              >
                {svc.name}
              </span>
            ))}
            {telemetry?.jellyfin_web_url && (
              <a
                href={telemetry.jellyfin_web_url}
                target="_blank"
                rel="noopener noreferrer"
                className="bp-pill bp-pill-ghost"
                style={{ minHeight: 28, padding: '0.2rem 0.65rem' }}
              >
                Jellyfin
              </a>
            )}
          </div>
        </div>
      </footer>

      {/* Mobile Bottom Nav */}
      <nav
        aria-label="Mobile navigation"
        className="ws-bottom-nav"
        style={{
          position: 'fixed',
          left: 0,
          right: 0,
          bottom: 0,
          zIndex: 40,
          background: 'var(--bp-surface)',
          borderTop: '1px solid var(--bp-line)',
          padding: '0.5rem 0.75rem',
          display: 'flex',
          justifyContent: 'space-around',
          gap: '0.4rem',
        }}
      >
        <button
          type="button"
          className="ws-tab" aria-current={activeTab === 'search' ? 'page' : undefined}
          onClick={() => setActiveTab('search')}
        >
          Search
        </button>
        <button
          type="button"
          className="ws-tab" aria-current={activeTab === 'downloads' ? 'page' : undefined}
          onClick={() => setActiveTab('downloads')}
        >
          Downloads
          {activeDownloadsCount > 0 ? <span className="ws-count">{activeDownloadsCount}</span> : null}
        </button>
        <button
          type="button"
          className="ws-tab" aria-current={activeTab === 'maintenance' ? 'page' : undefined}
          onClick={() => setActiveTab('maintenance')}
        >
          Maintenance
        </button>
        {isAdmin && (
          <button
            type="button"
            className="ws-tab" aria-current={activeTab === 'admin' ? 'page' : undefined}
            onClick={() => setActiveTab('admin')}
          >
            Admin
          </button>
        )}
        <button
          type="button"
          className={`ws-tab ws-vault${vaultUnlocked ? ' is-open' : ''}`}
          onClick={() => setVaultModalOpen(true)}
        >
          {vaultUnlocked ? formatCountdown(vaultExpiresIn) : 'Vault'}
        </button>
      </nav>

      {/* Modals */}
      <TheaterModal
        itemId={theaterTarget?.itemId ?? null}
        title={theaterTarget?.title ?? ''}
        prefetchedManifest={theaterTarget ? getCachedManifest(theaterTarget.itemId) : null}
        onClose={() => setTheaterTarget(null)}
        onProgressSaved={() => void fetchPlaybackProgress()}
      />

      <VaultModal
        open={vaultModalOpen}
        unlocked={vaultUnlocked}
        onClose={() => setVaultModalOpen(false)}
        onChanged={handleVaultChanged}
      />

      <PasskeysModal open={passkeysModalOpen} onClose={() => setPasskeysModalOpen(false)} />

      <HistoryModal open={historyModalOpen} onClose={() => setHistoryModalOpen(false)} />

      {isAdmin && (
        <SettingsModal
          open={settingsModalOpen}
          isAdmin={isAdmin}
          onClose={() => setSettingsModalOpen(false)}
          onSaved={() => void fetchTelemetryAndQueue()}
        />
      )}
    </div>
  );
}
