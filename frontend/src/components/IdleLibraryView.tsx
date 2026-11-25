import { useEffect, useRef, useState } from 'react';
import { useStaggerIn } from '../motion/useStaggerIn';
import { apiFetch } from '../lib/api';
import '../styles/tokens.css';
import type {
  GrabHistoryItem,
  LibraryItem,
  LibraryResponse,
  PlaybackProgressItem,
} from '../types/api';

export interface IdleLibraryViewProps {
  vaultUnlocked: boolean;
  playbackItems: PlaybackProgressItem[];
  onOpenPlayer: (itemId: string, title: string) => void;
  onPrefetchManifest: (itemId: string) => void;
  onRunSearch: (q: string) => void;
}

function calcProgressPct(item: PlaybackProgressItem): number {
  if (typeof item.progress_pct === 'number' && !Number.isNaN(item.progress_pct)) {
    return Math.max(0, Math.min(100, Math.round(item.progress_pct)));
  }
  if (item.duration_seconds > 0) {
    return Math.max(0, Math.min(100, Math.round((item.position_seconds / item.duration_seconds) * 100)));
  }
  return 0;
}

export default function IdleLibraryView({
  vaultUnlocked,
  playbackItems,
  onOpenPlayer,
  onPrefetchManifest,
  onRunSearch,
}: IdleLibraryViewProps) {
  const [library, setLibrary] = useState<LibraryResponse | null>(null);
  const [history, setHistory] = useState<GrabHistoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    // Don't keep rows from the other vault state on screen while refetching.
    setHistory([]);

    Promise.all([
      apiFetch<LibraryResponse>('/api/library', { signal: controller.signal }),
      apiFetch<GrabHistoryItem[] | { items?: GrabHistoryItem[]; history?: GrabHistoryItem[] }>(
        '/api/grab/history?limit=5',
        { signal: controller.signal },
      ).catch(() => [] as GrabHistoryItem[]),
    ])
      .then(([libRes, histRes]) => {
        if (controller.signal.aborted) return;
        setLibrary(libRes);
        const histList = Array.isArray(histRes)
          ? histRes
          : histRes.items || histRes.history || [];
        setHistory(histList.slice(0, 5));
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : 'Failed to load library.');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => controller.abort();
  }, [vaultUnlocked]);

  const continueWatching = playbackItems.filter((p) => !p.completed);

  // Shelves and cards rise in once their data arrives.
  const rootRef = useRef<HTMLElement | null>(null);
  useStaggerIn(rootRef, 'article', [library, playbackItems]);

  const renderShelf = (title: string, items: LibraryItem[], total?: number) => {
    if (!items || items.length === 0) return null;
    return (
      <div style={{ marginTop: '2rem' }}>
        <div className="bp-row" style={{ marginBottom: '0.85rem' }}>
          <h2 style={{ margin: 0, fontSize: '1.25rem', letterSpacing: '-0.02em' }}>{title}</h2>
          {typeof total === 'number' && <span className="bp-kicker">{total} items</span>}
        </div>
        <div className="bp-grid-3">
          {items.map((item) => {
            const idStr = String(item.id);
            return (
              <article
                key={idStr}
                className="bp-card"
                style={{
                  padding: '1rem',
                  display: 'flex',
                  flexDirection: 'column',
                  justifyContent: 'space-between',
                  gap: '0.75rem',
                }}
              >
                <div>
                  {item.poster_url && (
                    <div
                      style={{
                        borderRadius: 12,
                        overflow: 'hidden',
                        marginBottom: '0.75rem',
                        aspectRatio: '16 / 9',
                        background: 'var(--bp-canvas)',
                      }}
                    >
                      <img
                        src={item.poster_url}
                        alt={item.title}
                        referrerPolicy="no-referrer"
                        loading="lazy"
                        style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                      />
                    </div>
                  )}
                  <h3 style={{ margin: 0, fontSize: 15, lineHeight: 1.3 }}>{item.title}</h3>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.35rem', marginTop: '0.45rem' }}>
                    {item.year && <span className="bp-tag">{item.year}</span>}
                    {item.resolution && <span className="bp-tag">{item.resolution}</span>}
                    {item.type && <span className="bp-tag">{item.type}</span>}
                  </div>
                </div>

                <button
                  type="button"
                  className="bp-pill bp-pill-solid"
                  onMouseEnter={() => onPrefetchManifest(idStr)}
                  onFocus={() => onPrefetchManifest(idStr)}
                  onClick={() => onOpenPlayer(idStr, item.title)}
                >
                  Play
                </button>
              </article>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <section ref={rootRef} className="bp-container" aria-label="Library overview" style={{ paddingTop: '0.5rem' }}>
      {continueWatching.length > 0 && (
        <div style={{ marginTop: '1rem' }}>
          <div className="bp-row" style={{ marginBottom: '0.85rem' }}>
            <h2 style={{ margin: 0, fontSize: '1.25rem', letterSpacing: '-0.02em' }}>Continue watching</h2>
            <span className="bp-kicker">{continueWatching.length} IN PROGRESS</span>
          </div>
          <div className="bp-grid-3">
            {continueWatching.map((item) => {
              const pct = calcProgressPct(item);
              return (
                <article key={item.item_id} className="bp-card" style={{ padding: '1rem' }}>
                  <div className="bp-row" style={{ marginBottom: '0.4rem' }}>
                    <span className="bp-kicker">Resume</span>
                    <span className="bp-tag">{pct}%</span>
                  </div>
                  <h3 style={{ margin: '0 0 0.75rem', fontSize: 15, lineHeight: 1.35 }}>{item.title}</h3>
                  <div
                    role="progressbar"
                    aria-valuenow={pct}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    style={{
                      height: 6,
                      borderRadius: 999,
                      background: 'var(--bp-line)',
                      overflow: 'hidden',
                      marginBottom: '0.85rem',
                    }}
                  >
                    <div
                      style={{
                        width: `${pct}%`,
                        height: '100%',
                        background: 'var(--bp-red)',
                      }}
                    />
                  </div>
                  <button
                    type="button"
                    className="bp-pill bp-pill-solid"
                    onMouseEnter={() => onPrefetchManifest(item.item_id)}
                    onFocus={() => onPrefetchManifest(item.item_id)}
                    onClick={() => onOpenPlayer(item.item_id, item.title)}
                  >
                    RESUME ({pct}%)
                  </button>
                </article>
              );
            })}
          </div>
        </div>
      )}

      {error && (
        <div className="bp-alert" style={{ marginTop: '1.25rem' }}>
          {error}
        </div>
      )}

      {loading && !library && (
        <div className="bp-card" style={{ marginTop: '1.25rem' }}>
          <p className="bp-muted" style={{ margin: 0 }}>
            Loading library shelves…
          </p>
        </div>
      )}

      {library && (
        <>
          {renderShelf('Movies', library.movies, library.total_movies)}
          {renderShelf('TV Shows', library.shows, library.total_shows)}
          {vaultUnlocked && renderShelf('Adult Vault', library.adult, library.total_adult)}
        </>
      )}

      {history.length > 0 && (
        <div style={{ marginTop: '2rem' }}>
          <div className="bp-row" style={{ marginBottom: '0.85rem' }}>
            <h2 style={{ margin: 0, fontSize: '1.25rem', letterSpacing: '-0.02em' }}>Recent grabs</h2>
            <span className="bp-kicker">Click to search</span>
          </div>
          <div className="bp-card" style={{ padding: '0.75rem' }}>
            <div className="bp-list" style={{ gap: '0.45rem' }}>
              {history.map((grab, idx) => (
                <button
                  key={`${grab.id ?? idx}-${grab.title}`}
                  type="button"
                  onClick={() => onRunSearch(grab.title)}
                  className="bp-row"
                  style={{
                    width: '100%',
                    textAlign: 'left',
                    padding: '0.65rem 0.9rem',
                    borderRadius: 12,
                    border: '1px solid var(--bp-line)',
                    background: 'var(--bp-canvas)',
                    color: 'var(--bp-ink)',
                    cursor: 'pointer',
                  }}
                >
                  <span style={{ fontWeight: 500, fontSize: 14 }}>{grab.title}</span>
                  <span style={{ display: 'inline-flex', gap: '0.35rem', alignItems: 'center' }}>
                    {grab.target && <span className="bp-tag">{grab.target}</span>}
                    {grab.category && <span className="bp-tag">{grab.category}</span>}
                    {grab.indexer && <span className="bp-tag">{grab.indexer}</span>}
                  </span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
