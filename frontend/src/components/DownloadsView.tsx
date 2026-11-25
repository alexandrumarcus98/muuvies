import { useRef, useState } from 'react';
import { useStaggerIn } from '../motion/useStaggerIn';
import { ApiError, apiFetch } from '../lib/api';
import type { QueueTorrentItem, TelemetryResponse } from '../types/api';

interface DownloadsViewProps {
  queue: QueueTorrentItem[];
  loading: boolean;
  telemetry: TelemetryResponse | null;
  onRefresh: () => void;
  onOpenPlayer: (itemId: string, title: string) => void;
}

type QueueTorrentDisplayItem = QueueTorrentItem & {
  download_speed_formatted?: string;
  upload_speed_formatted?: string;
};

type QueueAction = 'pause' | 'resume' | 'delete';

export default function DownloadsView({ queue, loading, telemetry, onRefresh, onOpenPlayer }: DownloadsViewProps) {
  const [busyHash, setBusyHash] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmDeleteHash, setConfirmDeleteHash] = useState<string | null>(null);
  const [deleteFiles, setDeleteFiles] = useState(false);
  // New torrents rise into the queue; rows already listed stay put.
  const rootRef = useRef<HTMLElement | null>(null);
  useStaggerIn(rootRef, 'article', [queue]);

  // Finished: just play. Still downloading: switch that torrent to sequential
  // download first, so the start of the file arrives next.
  async function watch(item: QueueTorrentItem) {
    const itemId = `qb:${item.hash}`;
    if (Number(item.progress || 0) >= 100 || item.streaming) {
      onOpenPlayer(itemId, item.name);
      return;
    }
    setBusyHash(item.hash);
    setError(null);
    try {
      await apiFetch(`/api/stream/qb/${encodeURIComponent(item.hash)}/stream-mode`, { method: 'POST' });
      onOpenPlayer(itemId, item.name);
      onRefresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not start streaming this torrent.');
    } finally {
      setBusyHash(null);
    }
  }

  async function runAction(hash: string, action: QueueAction, shouldDeleteFiles = false) {
    setBusyHash(hash);
    setError(null);
    try {
      await apiFetch('/api/queue/action', {
        method: 'POST',
        body: JSON.stringify({
          hashes: [hash],
          action,
          delete_files: action === 'delete' ? shouldDeleteFiles : false,
        }),
      });
      if (action === 'delete') {
        setConfirmDeleteHash(null);
        setDeleteFiles(false);
      }
      onRefresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Queue action failed.');
    } finally {
      setBusyHash(null);
    }
  }

  return (
    <section ref={rootRef} className="bp-list" aria-label="Downloads Queue">
      <div className="bp-card">
        <div className="bp-row">
          <div>
            <p className="bp-kicker">Seedbox telemetry</p>
            <h2 className="bp-slider-title" style={{ fontSize: 'clamp(1.5rem, 3vw, 2.25rem)', marginTop: '0.25rem' }}>
              Active downloads
            </h2>
          </div>
          <div className="bp-nav-actions">
            {telemetry?.jellyfin_web_url ? (
              <a
                href={telemetry.jellyfin_web_url}
                target="_blank"
                rel="noreferrer"
                className="bp-pill bp-pill-ghost"
              >
                Open Jellyfin
              </a>
            ) : null}
            <button type="button" className="bp-pill bp-pill-solid" onClick={onRefresh} disabled={loading}>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>
        </div>

        <div className="bp-grid-3" style={{ marginTop: '1.25rem' }}>
          <div className="bp-card" style={{ background: 'var(--bp-canvas)', padding: '1rem 1.25rem' }}>
            <p className="bp-kicker">Download / upload</p>
            <p style={{ margin: '0.35rem 0 0', fontVariantNumeric: 'tabular-nums', fontSize: '1rem', fontWeight: 600 }}>
              ↓ {telemetry?.download_speed_formatted || '0 B/s'} · ↑ {telemetry?.upload_speed_formatted || '0 B/s'}
            </p>
          </div>
          <div className="bp-card" style={{ background: 'var(--bp-canvas)', padding: '1rem 1.25rem' }}>
            <p className="bp-kicker">Free disk space</p>
            <p style={{ margin: '0.35rem 0 0', fontVariantNumeric: 'tabular-nums', fontSize: '1rem', fontWeight: 600 }}>
              {telemetry?.free_disk_space_formatted || '—'}
            </p>
          </div>
          <div className="bp-card" style={{ background: 'var(--bp-canvas)', padding: '1rem 1.25rem' }}>
            <p className="bp-kicker">Active torrents</p>
            <p style={{ margin: '0.35rem 0 0', fontVariantNumeric: 'tabular-nums', fontSize: '1rem', fontWeight: 600 }}>
              {telemetry?.active_torrents_count ?? queue.length}
            </p>
          </div>
        </div>
      </div>

      {error ? (
        <div className="bp-alert" role="alert">
          {error}
        </div>
      ) : null}

      {loading && queue.length === 0 ? (
        <div className="bp-card">
          <p className="bp-muted" style={{ margin: 0 }}>
            Loading download queue…
          </p>
        </div>
      ) : null}

      {!loading && queue.length === 0 ? (
        <div className="bp-card">
          <p className="bp-muted" style={{ margin: 0 }}>
            The download queue is empty.
          </p>
        </div>
      ) : null}

      <div className="bp-list">
        {queue.map((rawItem) => {
          const item = rawItem as QueueTorrentDisplayItem;
          const isBusy = busyHash === item.hash;
          const dlSpeed = item.dlspeed_formatted ?? item.download_speed_formatted ?? '0 B/s';
          const ulSpeed = item.upspeed_formatted ?? item.upload_speed_formatted ?? '0 B/s';
          const progressPct = Math.max(0, Math.min(100, Number(item.progress || 0)));
          const isPaused = item.state.toLowerCase().includes('pause') || item.state.toLowerCase().includes('stop');
          const confirmingDelete = confirmDeleteHash === item.hash;

          return (
            <article key={item.hash} className="bp-card">
              <div className="bp-row" style={{ alignItems: 'flex-start' }}>
                <div style={{ flex: '1 1 360px', minWidth: 0 }}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem', marginBottom: '0.45rem' }}>
                    <span className="bp-tag">{item.state}</span>
                    {item.category ? <span className="bp-tag">{item.category}</span> : null}
                    {item.size_formatted ? <span className="bp-tag">{item.size_formatted}</span> : null}
                    {item.streaming && progressPct < 100 ? <span className="bp-tag">Streaming</span> : null}
                  </div>
                  <h3 style={{ margin: 0, fontSize: '1rem', wordBreak: 'break-word' }}>{item.name}</h3>
                  <p className="bp-muted" style={{ margin: '0.4rem 0 0', fontVariantNumeric: 'tabular-nums', fontSize: 13 }}>
                    {progressPct.toFixed(1)}% · ↓ {dlSpeed} · ↑ {ulSpeed}
                    {item.eta_formatted ? ` · ETA ${item.eta_formatted}` : ''}
                    {typeof item.ratio === 'number' ? ` · Ratio ${item.ratio.toFixed(2)}` : ''}
                  </p>
                </div>

                <div className="bp-nav-actions">
                  <button
                    type="button"
                    className="bp-pill bp-pill-solid"
                    disabled={isBusy}
                    onClick={() => void watch(item)}
                    title={progressPct >= 100 ? 'Play' : 'Play the downloaded part; it downloads in order from now on'}
                  >
                    {progressPct >= 100 ? 'Watch' : 'Watch now'}
                  </button>
                  {isPaused ? (
                    <button
                      type="button"
                      className="bp-pill bp-pill-ghost"
                      disabled={isBusy}
                      onClick={() => runAction(item.hash, 'resume')}
                    >
                      Resume
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="bp-pill bp-pill-ghost"
                      disabled={isBusy}
                      onClick={() => runAction(item.hash, 'pause')}
                    >
                      Pause
                    </button>
                  )}
                  <button
                    type="button"
                    className="bp-pill bp-pill-ghost"
                    disabled={isBusy}
                    onClick={() => {
                      setConfirmDeleteHash(confirmingDelete ? null : item.hash);
                      setDeleteFiles(false);
                    }}
                  >
                    Delete
                  </button>
                </div>
              </div>

              <div
                style={{
                  marginTop: '0.85rem',
                  height: 6,
                  borderRadius: 999,
                  background: 'var(--bp-line)',
                  overflow: 'hidden',
                }}
                aria-label={`Progress ${progressPct.toFixed(1)}%`}
              >
                <div
                  className="bp-progress-fill"
                  style={{
                    width: `${progressPct}%`,
                    height: '100%',
                    background: progressPct >= 100 ? 'var(--bp-ok)' : 'var(--bp-red)',
                  }}
                />
              </div>

              {confirmingDelete ? (
                <div
                  className="bp-row"
                  style={{
                    marginTop: '0.9rem',
                    paddingTop: '0.9rem',
                    borderTop: '1px solid var(--bp-line)',
                  }}
                >
                  <label style={{ display: 'inline-flex', alignItems: 'center', gap: '0.5rem', fontSize: 13 }}>
                    <input
                      type="checkbox"
                      checked={deleteFiles}
                      onChange={(e) => setDeleteFiles(e.target.checked)}
                    />
                    Also delete downloaded files from disk
                  </label>
                  <div className="bp-nav-actions">
                    <button
                      type="button"
                      className="bp-pill bp-pill-ghost"
                      disabled={isBusy}
                      onClick={() => {
                        setConfirmDeleteHash(null);
                        setDeleteFiles(false);
                      }}
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      className="bp-pill bp-pill-accent"
                      disabled={isBusy}
                      onClick={() => runAction(item.hash, 'delete', deleteFiles)}
                    >
                      {isBusy ? 'Deleting…' : 'Confirm Delete'}
                    </button>
                  </div>
                </div>
              ) : null}
            </article>
          );
        })}
      </div>
    </section>
  );
}
