import { useCallback, useEffect, useState } from 'react';
import { ApiError, apiFetch } from '../lib/api';
import type { AdminUserItem, AdminUsersResponse, AdminUserUpdateRequest } from '../types/api';

interface AdminViewProps {
  currentUsername?: string;
  onOpenSettings: () => void;
}

function formatDate(iso: string): string {
  if (!iso) return '—';
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return '—';
  return new Date(ts).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

export default function AdminView({ currentUsername, onOpenSettings }: AdminViewProps) {
  const [users, setUsers] = useState<AdminUserItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busyMap, setBusyMap] = useState<Record<number, boolean>>({});

  const loadUsers = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError('');
    try {
      const res = await apiFetch<AdminUsersResponse>('/api/admin/users', { signal });
      if (signal?.aborted) return;
      setUsers(res.users ?? []);
    } catch (err) {
      if (signal?.aborted) return;
      setError(err instanceof ApiError ? err.message : 'Failed to load users');
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void loadUsers(controller.signal);
    return () => controller.abort();
  }, [loadUsers]);

  const updateUser = async (user: AdminUserItem, patch: AdminUserUpdateRequest, label: string) => {
    setBusyMap((prev) => ({ ...prev, [user.id]: true }));
    setError('');
    setNotice('');
    try {
      const updated = await apiFetch<AdminUserItem>(`/api/admin/users/${user.id}`, {
        method: 'PATCH',
        body: JSON.stringify(patch),
      });
      setUsers((prev) => prev.map((u) => (u.id === updated.id ? updated : u)));
      setNotice(`${updated.username}: ${label}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : `Could not update ${user.username}`);
    } finally {
      setBusyMap((prev) => ({ ...prev, [user.id]: false }));
    }
  };

  const activeCount = users.filter((u) => u.is_active).length;
  const adminCount = users.filter((u) => u.role === 'admin' && u.is_active).length;

  return (
    <section className="bp-list" style={{ gap: '1.25rem' }} aria-label="Admin user management">
      <div className="bp-card">
        <div className="bp-row">
          <div>
            <h2 style={{ margin: 0, fontSize: '1.25rem', letterSpacing: '-0.02em' }}>Users and access</h2>
            <p className="bp-muted" style={{ margin: '0.25rem 0 0', fontSize: 14 }}>
              {users.length} {users.length === 1 ? 'account' : 'accounts'} · {activeCount} active · {adminCount}{' '}
              {adminCount === 1 ? 'admin' : 'admins'}
            </p>
          </div>
          <div className="bp-nav-actions">
            <button type="button" className="bp-pill bp-pill-ghost" onClick={onOpenSettings}>
              System settings
            </button>
            <button
              type="button"
              className="bp-pill bp-pill-solid"
              onClick={() => void loadUsers()}
              disabled={loading}
            >
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>
        </div>
      </div>

      {error && (
        <div className="bp-alert" role="alert">
          {error}
        </div>
      )}

      {notice && (
        <div className="bp-card" role="status" style={{ padding: '0.8rem 1rem', fontSize: 14 }}>
          {notice}
        </div>
      )}

      <div className="bp-list" role="list">
        {loading && users.length === 0 ? (
          <p className="bp-muted" style={{ padding: '1rem 0' }}>
            Loading accounts…
          </p>
        ) : users.length === 0 ? (
          <p className="bp-muted" style={{ padding: '1rem 0' }}>
            No user accounts found.
          </p>
        ) : (
          users.map((user) => {
            const isSelf = Boolean(currentUsername && user.username === currentUsername);
            const busy = Boolean(busyMap[user.id]);
            const isAdminRole = user.role === 'admin';

            return (
              <article key={user.id} className="bp-card bp-row" role="listitem">
                <div style={{ minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                    <strong style={{ fontSize: 15 }}>{user.username}</strong>
                    {isSelf && <span className="bp-kicker">(you)</span>}
                    <span
                      className="bp-kicker"
                      style={{ color: isAdminRole ? 'var(--bp-red-text)' : 'var(--bp-muted)' }}
                    >
                      {isAdminRole ? 'Admin' : 'User'}
                    </span>
                    <span
                      className="bp-kicker"
                      style={{ color: user.is_active ? 'var(--bp-ok)' : 'var(--bp-warn)' }}
                    >
                      {user.is_active ? 'Active' : 'Suspended'}
                    </span>
                  </div>
                  <p className="bp-muted" style={{ margin: '0.2rem 0 0', fontSize: 13, overflowWrap: 'anywhere' }}>
                    {user.email || 'No email'} · {user.passkey_count} {user.passkey_count === 1 ? 'passkey' : 'passkeys'} ·{' '}
                    {user.email_verified ? 'Verified' : 'Unverified'} · Joined {formatDate(user.created_at)}
                  </p>
                </div>

                <div className="bp-nav-actions">
                  <button
                    type="button"
                    className="bp-pill bp-pill-ghost"
                    disabled={busy || isSelf}
                    title={isSelf ? 'Cannot change your own role' : undefined}
                    onClick={() =>
                      void updateUser(
                        user,
                        { role: isAdminRole ? 'user' : 'admin' },
                        isAdminRole ? 'demoted to user' : 'promoted to admin',
                      )
                    }
                  >
                    {isAdminRole ? 'Make user' : 'Make admin'}
                  </button>

                  <button
                    type="button"
                    className="bp-pill bp-pill-ghost"
                    disabled={busy || isSelf}
                    title={isSelf ? 'Cannot suspend your own account' : undefined}
                    onClick={() =>
                      void updateUser(
                        user,
                        { is_active: !user.is_active },
                        user.is_active ? 'suspended' : 'reactivated',
                      )
                    }
                  >
                    {user.is_active ? 'Suspend' : 'Activate'}
                  </button>

                  <button
                    type="button"
                    className="bp-pill bp-pill-ghost"
                    disabled={busy}
                    onClick={() =>
                      void updateUser(user, { revoke_sessions: true }, 'sessions revoked')
                    }
                  >
                    Revoke sessions
                  </button>
                </div>
              </article>
            );
          })
        )}
      </div>
    </section>
  );
}

