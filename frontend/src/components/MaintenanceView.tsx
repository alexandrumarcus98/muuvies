import { useRef, useState } from 'react';
import { useStaggerIn } from '../motion/useStaggerIn';
import { ApiError, apiFetch } from '../lib/api';
import type { MaintenanceReport } from '../types/api';

interface MaintenanceViewProps {
  report: MaintenanceReport | null;
  loading: boolean;
  onRefresh: (force?: boolean) => void;
  onQueueChanged: () => void;
}

interface AiTriageResult {
  summary?: string;
  diagnosis?: string;
  severity?: string;
  recommendations?: string[];
  actions?: string[];
  [key: string]: unknown;
}

export default function MaintenanceView({
  report,
  loading,
  onRefresh,
  onQueueChanged,
}: MaintenanceViewProps) {
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [triageLoading, setTriageLoading] = useState(false);
  const [triageResult, setTriageResult] = useState<AiTriageResult | string | null>(null);
  const rootRef = useRef<HTMLElement | null>(null);
  useStaggerIn(rootRef, '.bp-card', [report, triageResult]);

  async function removeStalledTorrent(hash: string) {
    setBusyKey(`torrent:${hash}`);
    setError(null);
    setStatusMessage(null);
    try {
      await apiFetch('/api/queue/action', {
        method: 'POST',
        body: JSON.stringify({
          hashes: [hash],
          action: 'delete',
          delete_files: false,
        }),
      });
      setStatusMessage('Removed stalled torrent from queue.');
      onQueueChanged();
      onRefresh(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to remove stalled torrent.');
    } finally {
      setBusyKey(null);
    }
  }

  async function blacklistAndResearch(warning: NonNullable<MaintenanceReport['arr_warnings']>[number]) {
    if (!warning.queue_id) return;
    const key = `arr:${warning.service}:${warning.queue_id}`;
    setBusyKey(key);
    setError(null);
    setStatusMessage(null);
    try {
      const res = await apiFetch<{ message?: string }>('/api/maintenance/blacklist-research', {
        method: 'POST',
        body: JSON.stringify({
          service: warning.service,
          queue_id: warning.queue_id,
          remove_from_client: true,
          blocklist: true,
          search_again: true,
          media_id: warning.media_id ?? null,
          episode_ids: warning.episode_ids ?? null,
        }),
      });
      setStatusMessage(res.message || `Blocklisted and triggered replacement search on ${warning.service}.`);
      onQueueChanged();
      onRefresh(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Blacklist & re-search failed.');
    } finally {
      setBusyKey(null);
    }
  }

  async function runAiTriage() {
    setTriageLoading(true);
    setError(null);
    try {
      const res = await apiFetch<AiTriageResult | string>('/api/maintenance/ai-triage', {
        method: 'POST',
        body: JSON.stringify({ report }),
      });
      setTriageResult(res);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'AI Triage request failed.');
    } finally {
      setTriageLoading(false);
    }
  }

  const stalled = report?.stalled_torrents ?? [];
  const arrWarnings = report?.arr_warnings ?? [];
  const indexerIssues = report?.indexer_issues ?? [];
  const duplicates = report?.duplicates ?? [];
  const heavyFiles = report?.heavy_files ?? [];

  return (
    <section ref={rootRef} className="bp-list" aria-label="Maintenance Watchdog">
      <div className="bp-card">
        <div className="bp-row">
          <div>
            <p className="bp-kicker">Watchdog and diagnostics</p>
            <h2 className="bp-slider-title" style={{ fontSize: 'clamp(1.5rem, 3vw, 2.25rem)', marginTop: '0.25rem' }}>
              Maintenance {report ? `(${report.issue_count} ${report.issue_count === 1 ? 'issue' : 'issues'})` : ''}
            </h2>
          </div>
          <div className="bp-nav-actions">
            <button
              type="button"
              className="bp-pill bp-pill-ghost"
              onClick={runAiTriage}
              disabled={triageLoading || loading}
            >
              {triageLoading ? 'Analyzing…' : 'AI triage'}
            </button>
            <button
              type="button"
              className="bp-pill bp-pill-ghost"
              onClick={() => onRefresh(false)}
              disabled={loading}
            >
              Refresh
            </button>
            <button
              type="button"
              className="bp-pill bp-pill-solid"
              onClick={() => onRefresh(true)}
              disabled={loading}
            >
              {loading ? 'Scanning…' : 'Force rescan'}
            </button>
          </div>
        </div>

        <div className="bp-grid-3" style={{ marginTop: '1.25rem' }}>
          <div className="bp-card" style={{ background: 'var(--bp-canvas)', padding: '1rem 1.25rem' }}>
            <p className="bp-kicker">Disk space</p>
            <p style={{ margin: '0.35rem 0 0', fontVariantNumeric: 'tabular-nums', fontSize: '1rem', fontWeight: 600 }}>
              {report?.disk_free_formatted || '—'}
            </p>
            {report?.low_disk_space ? (
              <span className="bp-tag" style={{ marginTop: '0.45rem', borderColor: 'var(--bp-red)', color: 'var(--bp-red-text)' }}>
                Low Disk Space
              </span>
            ) : null}
          </div>
          <div className="bp-card" style={{ background: 'var(--bp-canvas)', padding: '1rem 1.25rem' }}>
            <p className="bp-kicker">Reclaimable</p>
            <p style={{ margin: '0.35rem 0 0', fontVariantNumeric: 'tabular-nums', fontSize: '1rem', fontWeight: 600 }}>
              {typeof report?.reclaimable_gb === 'number' ? `${report.reclaimable_gb.toFixed(1)} GB` : '—'}
            </p>
          </div>
          <div className="bp-card" style={{ background: 'var(--bp-canvas)', padding: '1rem 1.25rem' }}>
            <p className="bp-kicker">Seeding locked</p>
            <p style={{ margin: '0.35rem 0 0', fontVariantNumeric: 'tabular-nums', fontSize: '1rem', fontWeight: 600 }}>
              {typeof report?.seeding_locked_gb === 'number' ? `${report.seeding_locked_gb.toFixed(1)} GB` : '—'}
            </p>
          </div>
        </div>
      </div>

      {error ? (
        <div className="bp-alert" role="alert">
          {error}
        </div>
      ) : null}

      {statusMessage ? (
        <div className="bp-card" style={{ borderColor: 'var(--bp-ok)' }}>
          <p style={{ margin: 0 }}>{statusMessage}</p>
        </div>
      ) : null}

      {triageResult ? (
        <div className="bp-card" style={{ borderColor: 'var(--bp-red)' }}>
          <div className="bp-row">
            <p className="bp-kicker">AI triage plan</p>
            <button type="button" className="bp-pill bp-pill-ghost" onClick={() => setTriageResult(null)}>
              Dismiss
            </button>
          </div>
          {typeof triageResult === 'string' ? (
            <p style={{ margin: '0.75rem 0 0', whiteSpace: 'pre-wrap' }}>{triageResult}</p>
          ) : (
            <div className="bp-list" style={{ marginTop: '0.75rem' }}>
              {triageResult.severity ? <span className="bp-tag">{String(triageResult.severity)}</span> : null}
              {triageResult.summary || triageResult.diagnosis ? (
                <p style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
                  {String(triageResult.summary || triageResult.diagnosis)}
                </p>
              ) : null}
              {Array.isArray(triageResult.recommendations) && triageResult.recommendations.length > 0 ? (
                <ul style={{ margin: 0, paddingLeft: '1.25rem' }}>
                  {triageResult.recommendations.map((rec, idx) => (
                    <li key={idx}>{rec}</li>
                  ))}
                </ul>
              ) : null}
              {Array.isArray(triageResult.actions) && triageResult.actions.length > 0 ? (
                <ul style={{ margin: 0, paddingLeft: '1.25rem' }}>
                  {triageResult.actions.map((act, idx) => (
                    <li key={idx}>{typeof act === 'string' ? act : JSON.stringify(act)}</li>
                  ))}
                </ul>
              ) : null}
              {!triageResult.summary &&
              !triageResult.diagnosis &&
              !triageResult.recommendations &&
              !triageResult.actions ? (
                <pre style={{ margin: 0, fontVariantNumeric: 'tabular-nums', fontSize: 13, overflowX: 'auto' }}>
                  {JSON.stringify(triageResult, null, 2)}
                </pre>
              ) : null}
            </div>
          )}
        </div>
      ) : null}

      {loading && !report ? (
        <div className="bp-card">
          <p className="bp-muted" style={{ margin: 0 }}>
            Running maintenance diagnostics…
          </p>
        </div>
      ) : null}

      {report ? (
        <div className="bp-grid-2">
          <div className="bp-card">
            <p className="bp-kicker">Stalled torrents ({stalled.length})</p>
            {stalled.length === 0 ? (
              <p className="bp-muted" style={{ margin: '0.75rem 0 0' }}>
                No stalled torrents detected.
              </p>
            ) : (
              <div className="bp-list" style={{ marginTop: '0.85rem' }}>
                {stalled.map((t) => {
                  const isBusy = busyKey === `torrent:${t.hash}`;
                  return (
                    <div
                      key={t.hash}
                      className="bp-row"
                      style={{
                        paddingTop: '0.65rem',
                        borderTop: '1px solid var(--bp-line)',
                        alignItems: 'flex-start',
                      }}
                    >
                      <div style={{ flex: '1 1 200px', minWidth: 0 }}>
                        <p style={{ margin: 0, fontWeight: 600, wordBreak: 'break-word' }}>{t.name}</p>
                        <p className="bp-muted" style={{ margin: '0.25rem 0 0', fontVariantNumeric: 'tabular-nums', fontSize: 13 }}>
                          {t.state || 'stalled'}
                          {typeof t.age_hours === 'number' ? ` · ${t.age_hours.toFixed(1)}h` : ''}
                        </p>
                      </div>
                      <button
                        type="button"
                        className="bp-pill bp-pill-ghost"
                        disabled={isBusy}
                        onClick={() => removeStalledTorrent(t.hash)}
                      >
                        {isBusy ? 'Removing…' : 'Remove'}
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <div className="bp-card">
            <p className="bp-kicker">*arr warnings ({arrWarnings.length})</p>
            {arrWarnings.length === 0 ? (
              <p className="bp-muted" style={{ margin: '0.75rem 0 0' }}>
                No Radarr/Sonarr/Whisparr queue warnings.
              </p>
            ) : (
              <div className="bp-list" style={{ marginTop: '0.85rem' }}>
                {arrWarnings.map((w, idx) => {
                  const key = `arr:${w.service}:${w.queue_id ?? idx}`;
                  const isBusy = busyKey === key;
                  return (
                    <div
                      key={key}
                      className="bp-row"
                      style={{
                        paddingTop: '0.65rem',
                        borderTop: '1px solid var(--bp-line)',
                        alignItems: 'flex-start',
                      }}
                    >
                      <div style={{ flex: '1 1 200px', minWidth: 0 }}>
                        <span className="bp-tag">{w.service}</span>
                        <p style={{ margin: '0.35rem 0 0', fontWeight: 600, wordBreak: 'break-word' }}>{w.title}</p>
                        {w.message ? (
                          <p className="bp-muted" style={{ margin: '0.25rem 0 0', fontSize: 13 }}>
                            {w.message}
                          </p>
                        ) : null}
                      </div>
                      {w.queue_id ? (
                        <button
                          type="button"
                          className="bp-pill bp-pill-solid"
                          disabled={isBusy}
                          onClick={() => blacklistAndResearch(w)}
                        >
                          {isBusy ? 'Processing…' : 'Blacklist & Re-search'}
                        </button>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <div className="bp-card">
            <p className="bp-kicker">Indexer issues ({indexerIssues.length})</p>
            {indexerIssues.length === 0 ? (
              <p className="bp-muted" style={{ margin: '0.75rem 0 0' }}>
                All indexers healthy.
              </p>
            ) : (
              <div className="bp-list" style={{ marginTop: '0.85rem' }}>
                {indexerIssues.map((iss, idx) => (
                  <div
                    key={`${iss.name}-${idx}`}
                    style={{ paddingTop: '0.65rem', borderTop: '1px solid var(--bp-line)' }}
                  >
                    <p style={{ margin: 0, fontWeight: 600 }}>{iss.name}</p>
                    {iss.message ? (
                      <p className="bp-muted" style={{ margin: '0.25rem 0 0', fontSize: 13 }}>
                        {iss.message}
                      </p>
                    ) : null}
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="bp-card">
            <p className="bp-kicker">Duplicates ({duplicates.length})</p>
            {duplicates.length === 0 ? (
              <p className="bp-muted" style={{ margin: '0.75rem 0 0' }}>
                No duplicate releases found.
              </p>
            ) : (
              <div className="bp-list" style={{ marginTop: '0.85rem' }}>
                {duplicates.map((dup, idx) => (
                  <div
                    key={`${dup.title}-${idx}`}
                    className="bp-row"
                    style={{ paddingTop: '0.65rem', borderTop: '1px solid var(--bp-line)' }}
                  >
                    <span style={{ fontWeight: 600 }}>{dup.title}</span>
                    <span className="bp-tag">{dup.count}x</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ) : null}

      {heavyFiles.length > 0 ? (
        <div className="bp-card">
          <p className="bp-kicker">Heavy files ({heavyFiles.length})</p>
          <div className="bp-list" style={{ marginTop: '0.85rem' }}>
            {heavyFiles.map((hf, idx) => (
              <div
                key={`${hf.name}-${idx}`}
                className="bp-row"
                style={{ paddingTop: '0.65rem', borderTop: '1px solid var(--bp-line)' }}
              >
                <span style={{ wordBreak: 'break-word' }}>{hf.name}</span>
                {hf.size_formatted ? <span className="bp-tag">{hf.size_formatted}</span> : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}
