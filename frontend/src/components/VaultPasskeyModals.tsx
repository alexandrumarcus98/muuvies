import { useEffect, useState } from 'react';
import { Modal } from './Modal';
import { ApiError, apiFetch } from '../lib/api';
import {
  isPasskeySupported,
  normalizeWebAuthnError,
  prepareCreationOptions,
  prepareRequestOptions,
  serializeAuthenticationCredential,
  serializeRegistrationCredential,
} from '../lib/passkeys';
import type { AdminUserItem, AdminUsersResponse, GrabHistoryItem, PasskeyItem, VaultStatusResponse } from '../types/api';

interface VaultModalProps {
  open: boolean;
  unlocked?: boolean;
  onClose: () => void;
  onChanged: () => void;
}

export function VaultModal({ open, unlocked = false, onClose, onChanged }: VaultModalProps) {
  const [pin, setPin] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const passkeyReady = isPasskeySupported();

  useEffect(() => {
    if (!open) {
      setPin('');
      setError(null);
    }
  }, [open]);

  async function handlePinUnlock(e: React.FormEvent) {
    e.preventDefault();
    if (!pin.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await apiFetch<VaultStatusResponse>('/api/vault/unlock', {
        method: 'POST',
        body: JSON.stringify({ pin: pin.trim() }),
      });
      setPin('');
      onChanged();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to unlock Vault.');
    } finally {
      setBusy(false);
    }
  }

  async function handlePasskeyUnlock() {
    setBusy(true);
    setError(null);
    try {
      const optRes = await apiFetch<{ challenge_id: string; options: Record<string, unknown> }>(
        '/api/vault/unlock/passkey/options',
        { method: 'POST' },
      );
      const cred = (await navigator.credentials.get({
        publicKey: prepareRequestOptions(optRes.options),
      })) as PublicKeyCredential | null;
      if (!cred) {
        throw new Error('Passkey prompt was cancelled.');
      }
      await apiFetch<VaultStatusResponse>('/api/vault/unlock/passkey/verify', {
        method: 'POST',
        body: JSON.stringify({
          challenge_id: optRes.challenge_id,
          credential: serializeAuthenticationCredential(cred),
        }),
      });
      onChanged();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : normalizeWebAuthnError(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleLock() {
    setBusy(true);
    setError(null);
    try {
      await apiFetch<VaultStatusResponse>('/api/vault/lock', { method: 'POST' });
      onChanged();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to lock Vault.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} label="Adult Vault">
      <div className="bp-row">
        <div>
          <p className="bp-kicker">Stealth vault</p>
          <h2 style={{ margin: '0.2rem 0 0' }}>{unlocked ? 'Vault Unlocked' : 'Unlock Vault'}</h2>
        </div>
        <button type="button" className="bp-pill bp-pill-ghost" onClick={onClose}>
          Close
        </button>
      </div>

      {error ? (
        <div className="bp-alert" role="alert" style={{ marginTop: '1rem' }}>
          {error}
        </div>
      ) : null}

      {unlocked ? (
        <div className="bp-list" style={{ marginTop: '1.25rem' }}>
          <p className="bp-muted" style={{ margin: 0 }}>
            Stealth mode is currently disengaged for this session.
          </p>
          <button type="button" className="bp-pill bp-pill-accent" disabled={busy} onClick={handleLock}>
            {busy ? 'Locking…' : 'Lock Vault Now'}
          </button>
        </div>
      ) : (
        <form className="bp-list" style={{ marginTop: '1.25rem' }} onSubmit={handlePinUnlock}>
          <label>
            <span className="bp-kicker">Vault PIN</span>
            <input
              type="password"
              inputMode="numeric"
              className="bp-input"
              style={{ marginTop: '0.4rem' }}
              placeholder="Enter Vault PIN"
              value={pin}
              onChange={(e) => setPin(e.target.value)}
              autoFocus
            />
          </label>
          <div className="bp-nav-actions">
            <button type="submit" className="bp-pill bp-pill-solid" disabled={busy || !pin.trim()}>
              {busy ? 'Unlocking…' : 'Unlock with PIN'}
            </button>
            {passkeyReady ? (
              <button
                type="button"
                className="bp-pill bp-pill-ghost"
                disabled={busy}
                onClick={handlePasskeyUnlock}
              >
                Unlock with Passkey
              </button>
            ) : null}
          </div>
        </form>
      )}
    </Modal>
  );
}

interface PasskeysModalProps {
  open: boolean;
  onClose: () => void;
}

export function PasskeysModal({ open, onClose }: PasskeysModalProps) {
  const [passkeys, setPasskeys] = useState<PasskeyItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [deviceName, setDeviceName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const passkeyReady = isPasskeySupported();

  async function loadPasskeys() {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch<{ passkeys: PasskeyItem[] }>('/api/auth/passkeys');
      setPasskeys(res.passkeys || []);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to load passkeys.');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (open) {
      void loadPasskeys();
    } else {
      setDeviceName('');
      setError(null);
    }
  }, [open]);

  async function handleRegister(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const optRes = await apiFetch<{ challenge_id: string; options: Record<string, unknown> }>(
        '/api/auth/passkeys/register/options',
        { method: 'POST' },
      );
      const cred = (await navigator.credentials.create({
        publicKey: prepareCreationOptions(optRes.options),
      })) as PublicKeyCredential | null;
      if (!cred) {
        throw new Error('Passkey registration was cancelled.');
      }
      await apiFetch<PasskeyItem>('/api/auth/passkeys/register/verify', {
        method: 'POST',
        body: JSON.stringify({
          challenge_id: optRes.challenge_id,
          credential: serializeRegistrationCredential(cred),
          device_name: deviceName.trim() || 'Passkey',
        }),
      });
      setDeviceName('');
      await loadPasskeys();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : normalizeWebAuthnError(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(id: number) {
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/api/auth/passkeys/${id}`, { method: 'DELETE' });
      setPasskeys((prev) => prev.filter((pk) => pk.id !== id));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to delete passkey.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} label="Passkeys">
      <div className="bp-row">
        <div>
          <p className="bp-kicker">WebAuthn credentials</p>
          <h2 style={{ margin: '0.2rem 0 0' }}>Passkeys</h2>
        </div>
        <button type="button" className="bp-pill bp-pill-ghost" onClick={onClose}>
          Close
        </button>
      </div>

      {error ? (
        <div className="bp-alert" role="alert" style={{ marginTop: '1rem' }}>
          {error}
        </div>
      ) : null}

      {passkeyReady ? (
        <form className="bp-row" style={{ marginTop: '1rem' }} onSubmit={handleRegister}>
          <input
            type="text"
            className="bp-input"
            style={{ flex: '1 1 200px' }}
            placeholder="Device name (e.g. MacBook Touch ID)"
            value={deviceName}
            onChange={(e) => setDeviceName(e.target.value)}
          />
          <button type="submit" className="bp-pill bp-pill-solid" disabled={busy}>
            {busy ? 'Registering…' : 'Register Passkey'}
          </button>
        </form>
      ) : (
        <p className="bp-muted" style={{ marginTop: '1rem' }}>
          Passkeys require HTTPS or localhost in a WebAuthn-capable browser.
        </p>
      )}

      <hr className="bp-hr" />

      {loading ? (
        <p className="bp-muted">Loading passkeys…</p>
      ) : passkeys.length === 0 ? (
        <p className="bp-muted">No passkeys registered yet.</p>
      ) : (
        <div className="bp-list">
          {passkeys.map((pk) => (
            <div key={pk.id} className="bp-row">
              <div>
                <p style={{ margin: 0, fontWeight: 600 }}>{pk.device_name}</p>
                <p className="bp-muted" style={{ margin: '0.2rem 0 0', fontVariantNumeric: 'tabular-nums', fontSize: 13 }}>
                  Added {pk.created_at ? new Date(pk.created_at).toLocaleDateString() : '—'}
                  {pk.last_used_at ? ` · Last used ${new Date(pk.last_used_at).toLocaleDateString()}` : ''}
                </p>
              </div>
              <button
                type="button"
                className="bp-pill bp-pill-ghost"
                disabled={busy}
                onClick={() => handleDelete(pk.id)}
              >
                Revoke
              </button>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

interface HistoryModalProps {
  open: boolean;
  onClose: () => void;
}

export function HistoryModal({ open, onClose }: HistoryModalProps) {
  const [items, setItems] = useState<GrabHistoryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiFetch<GrabHistoryItem[]>('/api/grab/history')
      .then((res) => {
        if (!cancelled) setItems(res || []);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Unable to load grab history.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  return (
    <Modal open={open} onClose={onClose} label="Grab History" panelClassName="bp-modal-wide">
      <div className="bp-row">
        <div>
          <p className="bp-kicker">Recent activity</p>
          <h2 style={{ margin: '0.2rem 0 0' }}>Grab history</h2>
        </div>
        <button type="button" className="bp-pill bp-pill-ghost" onClick={onClose}>
          Close
        </button>
      </div>

      {error ? (
        <div className="bp-alert" role="alert" style={{ marginTop: '1rem' }}>
          {error}
        </div>
      ) : null}

      {loading ? (
        <p className="bp-muted" style={{ marginTop: '1rem' }}>
          Loading grab history…
        </p>
      ) : items.length === 0 ? (
        <p className="bp-muted" style={{ marginTop: '1rem' }}>
          No grabs recorded yet.
        </p>
      ) : (
        <div className="bp-list" style={{ marginTop: '1rem' }}>
          {items.map((item, idx) => (
            <div
              key={item.id ?? `${item.title}-${idx}`}
              className="bp-row"
              style={{ paddingTop: '0.7rem', borderTop: '1px solid var(--bp-line)' }}
            >
              <div style={{ flex: '1 1 320px', minWidth: 0 }}>
                <p style={{ margin: 0, fontWeight: 600, wordBreak: 'break-word' }}>{item.title}</p>
                <p className="bp-muted" style={{ margin: '0.2rem 0 0', fontVariantNumeric: 'tabular-nums', fontSize: 13 }}>
                  {[item.category, item.indexer, item.target].filter(Boolean).join(' · ')}
                </p>
              </div>
              {item.created_at ? (
                <span className="bp-tag">{new Date(item.created_at).toLocaleString()}</span>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

interface SettingsResponse {
  ai_provider: string;
  gemini_api_key_configured: boolean;
  gemini_model: string;
  github_token_configured: boolean;
  github_model: string;
  seedbox_host: string;
  seedbox_user: string;
  qbittorrent_configured: boolean;
  prowlarr_configured: boolean;
  radarr_configured: boolean;
  sonarr_configured: boolean;
  whisparr_configured: boolean;
  cross_seed_configured: boolean;
  vault_pin_configured: boolean;
  sentry_configured?: boolean;
}

interface SettingsModalProps {
  open: boolean;
  isAdmin: boolean;
  onClose: () => void;
  onSaved?: () => void;
}

export function SettingsModal({ open, isAdmin, onClose, onSaved }: SettingsModalProps) {
  const [settingsData, setSettingsData] = useState<SettingsResponse | null>(null);
  const [modalTab, setModalTab] = useState<'credentials' | 'users'>('credentials');
  const [usersList, setUsersList] = useState<AdminUserItem[]>([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const [busyUserId, setBusyUserId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedMsg, setSavedMsg] = useState<string | null>(null);

  const [aiProvider, setAiProvider] = useState('');
  const [geminiApiKey, setGeminiApiKey] = useState('');
  const [githubToken, setGithubToken] = useState('');
  const [prowlarrApiKey, setProwlarrApiKey] = useState('');
  const [radarrApiKey, setRadarrApiKey] = useState('');
  const [sonarrApiKey, setSonarrApiKey] = useState('');
  const [whisparrApiKey, setWhisparrApiKey] = useState('');
  const [crossSeedApiKey, setCrossSeedApiKey] = useState('');
  const [qbittorrentPassword, setQbittorrentPassword] = useState('');
  const [vaultPin, setVaultPin] = useState('');
  const [sentryDsn, setSentryDsn] = useState('');

  useEffect(() => {
    if (!open || !isAdmin) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSavedMsg(null);
    apiFetch<SettingsResponse>('/api/settings')
      .then((res) => {
        if (!cancelled) {
          setSettingsData(res);
          setAiProvider(res.ai_provider || '');
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Unable to load settings.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, isAdmin]);

  const loadUsers = async () => {
    setUsersLoading(true);
    setError(null);
    try {
      const res = await apiFetch<AdminUsersResponse>('/api/admin/users');
      setUsersList(res.users || []);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to load users.');
    } finally {
      setUsersLoading(false);
    }
  };

  useEffect(() => {
    if (open && isAdmin && modalTab === 'users') {
      void loadUsers();
    }
  }, [open, isAdmin, modalTab]);

  async function updateAdminUser(
    userId: number,
    patch: { role?: 'admin' | 'user'; is_active?: boolean; revoke_sessions?: boolean },
  ) {
    setBusyUserId(userId);
    setError(null);
    setSavedMsg(null);
    try {
      const updated = await apiFetch<AdminUserItem>(`/api/admin/users/${userId}`, {
        method: 'PATCH',
        body: JSON.stringify(patch),
      });
      setUsersList((prev) => prev.map((u) => (u.id === userId ? updated : u)));
      setSavedMsg(`Updated ${updated.username}.`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to update user.');
    } finally {
      setBusyUserId(null);
    }
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    setSavedMsg(null);

    const payload: Record<string, string> = {};
    if (aiProvider.trim()) payload.ai_provider = aiProvider.trim();
    if (geminiApiKey.trim()) payload.gemini_api_key = geminiApiKey.trim();
    if (githubToken.trim()) payload.github_token = githubToken.trim();
    if (prowlarrApiKey.trim()) payload.prowlarr_api_key = prowlarrApiKey.trim();
    if (radarrApiKey.trim()) payload.radarr_api_key = radarrApiKey.trim();
    if (sonarrApiKey.trim()) payload.sonarr_api_key = sonarrApiKey.trim();
    if (whisparrApiKey.trim()) payload.whisparr_api_key = whisparrApiKey.trim();
    if (crossSeedApiKey.trim()) payload.cross_seed_api_key = crossSeedApiKey.trim();
    if (qbittorrentPassword.trim()) payload.qbittorrent_password = qbittorrentPassword.trim();
    if (vaultPin.trim()) payload.vault_pin = vaultPin.trim();
    if (sentryDsn.trim()) payload.sentry_dsn = sentryDsn.trim();

    try {
      await apiFetch('/api/settings', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      setGeminiApiKey('');
      setGithubToken('');
      setProwlarrApiKey('');
      setRadarrApiKey('');
      setSonarrApiKey('');
      setWhisparrApiKey('');
      setCrossSeedApiKey('');
      setQbittorrentPassword('');
      setVaultPin('');
      setSentryDsn('');
      setSavedMsg('Settings updated.');
      onSaved?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to save settings.');
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} label="Admin Settings" panelClassName="bp-modal-wide">
      <div className="bp-row">
        <div>
          <p className="bp-kicker">Administration</p>
          <h2 style={{ margin: '0.2rem 0 0' }}>Settings &amp; Credentials</h2>
        </div>
        <button type="button" className="bp-pill bp-pill-ghost" onClick={onClose}>
          Close
        </button>
      </div>

      {!isAdmin ? (
        <div className="bp-alert" style={{ marginTop: '1rem' }}>
          Administrator privileges are required to view or modify server settings.
        </div>
      ) : (
        <>
          <div style={{ display: 'flex', gap: '0.5rem', marginTop: '1rem' }}>
            <button
              type="button"
              className={`bp-pill ${modalTab === 'credentials' ? '' : 'bp-pill-ghost'}`}
              onClick={() => setModalTab('credentials')}
            >
              Credentials
            </button>
            <button
              type="button"
              className={`bp-pill ${modalTab === 'users' ? '' : 'bp-pill-ghost'}`}
              onClick={() => setModalTab('users')}
            >
              Users &amp; Roles
            </button>
          </div>

          {error ? (
            <div className="bp-alert" role="alert" style={{ marginTop: '1rem' }}>
              {error}
            </div>
          ) : null}

          {savedMsg ? (
            <div className="bp-card" style={{ marginTop: '1rem', borderColor: 'var(--bp-ok)' }}>
              <p style={{ margin: 0 }}>{savedMsg}</p>
            </div>
          ) : null}

          {modalTab === 'users' ? (
            usersLoading ? (
              <p className="bp-muted" style={{ marginTop: '1rem' }}>Loading users…</p>
            ) : (
              <div className="bp-list" style={{ marginTop: '1rem', gap: '0.65rem' }}>
                {usersList.map((u) => (
                  <div key={u.id} className="bp-card" style={{ padding: '0.85rem 1rem' }}>
                    <div className="bp-row" style={{ gap: '0.75rem', alignItems: 'center' }}>
                      <div>
                        <strong>{u.username}</strong>{' '}
                        <span className="bp-muted" style={{ fontSize: '13px' }}>{u.email}</span>
                        <div className="bp-muted" style={{ fontSize: '13px', marginTop: '0.2rem' }}>
                          Role: <strong>{u.role}</strong> · {u.is_active ? 'Active' : 'Disabled'} · Passkeys: {u.passkey_count} · Session v{u.token_version}
                        </div>
                      </div>
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem' }}>
                        <button
                          type="button"
                          className="bp-pill bp-pill-ghost"
                          disabled={busyUserId === u.id}
                          onClick={() => void updateAdminUser(u.id, { role: u.role === 'admin' ? 'user' : 'admin' })}
                        >
                          {u.role === 'admin' ? 'Make user' : 'Make admin'}
                        </button>
                        <button
                          type="button"
                          className="bp-pill bp-pill-ghost"
                          disabled={busyUserId === u.id}
                          onClick={() => void updateAdminUser(u.id, { is_active: !u.is_active })}
                        >
                          {u.is_active ? 'Disable' : 'Enable'}
                        </button>
                        <button
                          type="button"
                          className="bp-pill bp-pill-ghost"
                          disabled={busyUserId === u.id}
                          onClick={() => void updateAdminUser(u.id, { revoke_sessions: true })}
                        >
                          Revoke sessions
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )
          ) : loading ? (
            <p className="bp-muted" style={{ marginTop: '1rem' }}>
              Loading settings…
            </p>
          ) : (
            <form className="bp-list" style={{ marginTop: '1rem' }} onSubmit={handleSave}>
              {settingsData ? (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem' }}>
                  <span className="bp-tag">Host: {settingsData.seedbox_host || '—'}</span>
                  <span className="bp-tag">Prowlarr: {settingsData.prowlarr_configured ? 'OK' : 'Missing'}</span>
                  <span className="bp-tag">Radarr: {settingsData.radarr_configured ? 'OK' : 'Missing'}</span>
                  <span className="bp-tag">Sonarr: {settingsData.sonarr_configured ? 'OK' : 'Missing'}</span>
                  <span className="bp-tag">Whisparr: {settingsData.whisparr_configured ? 'OK' : 'Missing'}</span>
                  <span className="bp-tag">qBit: {settingsData.qbittorrent_configured ? 'OK' : 'Missing'}</span>
                  <span className="bp-tag">Gemini: {settingsData.gemini_api_key_configured ? 'OK' : 'Missing'}</span>
                  <span className="bp-tag">GitHub: {settingsData.github_token_configured ? 'OK' : 'Missing'}</span>
                  <span className="bp-tag">Sentry: {settingsData.sentry_configured ? 'OK' : 'Off'}</span>
                </div>
              ) : null}

              <div className="bp-grid-2">
                <label>
                  <span className="bp-kicker">AI provider</span>
                  <input
                    type="text"
                    className="bp-input"
                    style={{ marginTop: '0.35rem' }}
                    placeholder="gemini / github"
                    value={aiProvider}
                    onChange={(e) => setAiProvider(e.target.value)}
                  />
                </label>
                <label>
                  <span className="bp-kicker">Vault PIN (new)</span>
                  <input
                    type="password"
                    className="bp-input"
                    style={{ marginTop: '0.35rem' }}
                    placeholder="Leave blank to keep"
                    value={vaultPin}
                    onChange={(e) => setVaultPin(e.target.value)}
                  />
                </label>
                <label>
                  <span className="bp-kicker">Gemini API key</span>
                  <input
                    type="password"
                    className="bp-input"
                    style={{ marginTop: '0.35rem' }}
                    placeholder="Leave blank to keep"
                    value={geminiApiKey}
                    onChange={(e) => setGeminiApiKey(e.target.value)}
                  />
                </label>
                <label>
                  <span className="bp-kicker">GitHub token</span>
                  <input
                    type="password"
                    className="bp-input"
                    style={{ marginTop: '0.35rem' }}
                    placeholder="Leave blank to keep"
                    value={githubToken}
                    onChange={(e) => setGithubToken(e.target.value)}
                  />
                </label>
                <label>
                  <span className="bp-kicker">Prowlarr API key</span>
                  <input
                    type="password"
                    className="bp-input"
                    style={{ marginTop: '0.35rem' }}
                    placeholder="Leave blank to keep"
                    value={prowlarrApiKey}
                    onChange={(e) => setProwlarrApiKey(e.target.value)}
                  />
                </label>
                <label>
                  <span className="bp-kicker">Radarr API key</span>
                  <input
                    type="password"
                    className="bp-input"
                    style={{ marginTop: '0.35rem' }}
                    placeholder="Leave blank to keep"
                    value={radarrApiKey}
                    onChange={(e) => setRadarrApiKey(e.target.value)}
                  />
                </label>
                <label>
                  <span className="bp-kicker">Sonarr API key</span>
                  <input
                    type="password"
                    className="bp-input"
                    style={{ marginTop: '0.35rem' }}
                    placeholder="Leave blank to keep"
                    value={sonarrApiKey}
                    onChange={(e) => setSonarrApiKey(e.target.value)}
                  />
                </label>
                <label>
                  <span className="bp-kicker">Whisparr API key</span>
                  <input
                    type="password"
                    className="bp-input"
                    style={{ marginTop: '0.35rem' }}
                    placeholder="Leave blank to keep"
                    value={whisparrApiKey}
                    onChange={(e) => setWhisparrApiKey(e.target.value)}
                  />
                </label>
                <label>
                  <span className="bp-kicker">cross-seed API key</span>
                  <input
                    type="password"
                    className="bp-input"
                    style={{ marginTop: '0.35rem' }}
                    placeholder="Leave blank to keep"
                    value={crossSeedApiKey}
                    onChange={(e) => setCrossSeedApiKey(e.target.value)}
                  />
                </label>
                <label>
                  <span className="bp-kicker">qBittorrent password</span>
                  <input
                    type="password"
                    className="bp-input"
                    style={{ marginTop: '0.35rem' }}
                    placeholder="Leave blank to keep"
                    value={qbittorrentPassword}
                    onChange={(e) => setQbittorrentPassword(e.target.value)}
                  />
                </label>
                <label>
                  <span className="bp-kicker">Sentry DSN</span>
                  <input
                    type="text"
                    className="bp-input"
                    style={{ marginTop: '0.35rem' }}
                    placeholder="Leave blank to keep"
                    value={sentryDsn}
                    onChange={(e) => setSentryDsn(e.target.value)}
                  />
                </label>
              </div>

              <div className="bp-row">
                <span className="bp-muted" style={{ fontSize: 13 }}>
                  Only non-empty fields are updated.
                </span>
                <button type="submit" className="bp-pill bp-pill-solid" disabled={saving}>
                  {saving ? 'Saving…' : 'Save Settings'}
                </button>
              </div>
            </form>
          )}
        </>
      )}
    </Modal>
  );
}
