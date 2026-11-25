import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, apiFetch } from '../lib/api';
import '../styles/tokens.css';
import { useBackdropDismiss, useDialogEnter } from './Modal';
import { LoadingBar } from './LoadingBar';
import { useStaggerIn } from '../motion/useStaggerIn';
import type {
  AiRankingInfo,
  PreviewImageItem,
  PreviewResponse,
  SearchResponse,
  SearchResultItem,
  SuggestionItem,
} from '../types/api';

export interface SearchViewProps {
  vaultUnlocked: boolean;
  initialQuery?: string;
  onOpenPlayer: (itemId: string, title: string) => void;
  onPrefetchManifest: (itemId: string) => void;
  onQueueChanged: () => void;
}

type CategoryKey = 'all' | 'movies' | 'tv' | 'adult';

const CATEGORIES: Array<{ key: CategoryKey; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'movies', label: 'Movies' },
  { key: 'tv', label: 'TV Shows' },
  { key: 'adult', label: 'Adult' },
];

const PAGE_SIZE = 20;

function isAdultCategory(cat?: string | null): boolean {
  // Prowlarr names include "XXX/HD", "XXX/Other" etc., not just "XXX".
  const c = (cat || '').trim().toLowerCase();
  return c.includes('adult') || c.includes('xxx');
}

function resolveArrTarget(cat?: string | null): 'sonarr' | 'whisparr' | 'radarr' {
  const c = (cat || '').trim().toLowerCase();
  if (c === 'tv' || c === 'shows' || c === 'series') return 'sonarr';
  if (c === 'adult' || c === 'xxx') return 'whisparr';
  return 'radarr';
}

function isEditableElement(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
}

export default function SearchView({
  vaultUnlocked,
  initialQuery = '',
  onOpenPlayer,
  onPrefetchManifest,
  onQueueChanged,
}: SearchViewProps) {
  const [query, setQuery] = useState(initialQuery);
  const [category, setCategory] = useState<CategoryKey>('all');
  const [deep, setDeep] = useState(false);

  const [suggestions, setSuggestions] = useState<SuggestionItem[]>([]);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [activeSuggestIdx, setActiveSuggestIdx] = useState(-1);

  const [results, setResults] = useState<SearchResultItem[]>([]);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(0);
  const [totalFound, setTotalFound] = useState(0);
  const [hasSearched, setHasSearched] = useState(false);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [aiRanking, setAiRanking] = useState<AiRankingInfo | null>(null);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);

  const [grabbingMap, setGrabbingMap] = useState<Record<string, boolean>>({});
  const [grabbedMap, setGrabbedMap] = useState<Record<string, boolean>>({});
  const [grabNotice, setGrabNotice] = useState<string | null>(null);

  // Inline Adult Preview state keyed by result guid
  const [previewsByGuid, setPreviewsByGuid] = useState<Record<string, PreviewImageItem[]>>({});
  const [previewLoading, setPreviewLoading] = useState<Record<string, boolean>>({});
  const [previewErrors, setPreviewErrors] = useState<Record<string, string>>({});
  const [expandedPreviewGuid, setExpandedPreviewGuid] = useState<string | null>(null);

  // Lightbox state
  const [lightbox, setLightbox] = useState<{
    guid: string;
    title: string;
    images: PreviewImageItem[];
    index: number;
  } | null>(null);

  const inputRef = useRef<HTMLInputElement | null>(null);
  const searchAbortRef = useRef<AbortController | null>(null);
  const suggestAbortRef = useRef<AbortController | null>(null);
  const prefetchAbortRef = useRef<AbortController | null>(null);
  const previewAbortMapRef = useRef<Map<string, AbortController>>(new Map());
  const touchStartRef = useRef<{ x: number; y: number } | null>(null);
  const latestResultsRef = useRef<SearchResultItem[]>([]);
  latestResultsRef.current = results;
  const lightboxBackdropRef = useRef<HTMLDivElement | null>(null);
  const lightboxPanelRef = useRef<HTMLDivElement | null>(null);
  const closeLightbox = useCallback(() => setLightbox(null), []);
  const lightboxDismiss = useBackdropDismiss(closeLightbox);
  useDialogEnter(lightboxBackdropRef, lightboxPanelRef, Boolean(lightbox && vaultUnlocked));

  // Result rows rise in as each page arrives; notices and the AI pick pop in.
  const searchRootRef = useRef<HTMLElement | null>(null);
  const resultsRef = useRef<HTMLDivElement | null>(null);
  useStaggerIn(resultsRef, 'article', [results]);
  useStaggerIn(searchRootRef, '[data-motion-pop]', [grabNotice, aiRanking?.recommended_guid]);
  const lastInitialQueryRef = useRef<string>('');

  const runSearch = useCallback(
    async (
      qText: string,
      cat: CategoryKey,
      deepFlag: boolean,
      targetPage = 1,
    ) => {
      const trimmed = qText.trim();
      if (!trimmed) return;

      searchAbortRef.current?.abort();
      const controller = new AbortController();
      searchAbortRef.current = controller;

      setSuggestOpen(false);
      setActiveSuggestIdx(-1);
      setSearching(true);
      setSearchError(null);
      setAiRanking(null);
      setAiError(null);
      setGrabNotice(null);

      try {
        const res = await apiFetch<SearchResponse>('/api/search', {
          method: 'POST',
          signal: controller.signal,
          body: JSON.stringify({
            query: trimmed,
            category: cat,
            deep: deepFlag,
            page: targetPage,
            page_size: PAGE_SIZE,
          }),
        });
        if (controller.signal.aborted) return;

        const filtered = vaultUnlocked
          ? res.results || []
          : (res.results || []).filter((r) => !isAdultCategory(r.category) && !r.preview_token);

        setResults(filtered);
        setPage(res.page || targetPage);
        setTotalPages(res.total_pages || 1);
        setTotalFound(vaultUnlocked ? res.total_found ?? filtered.length : filtered.length);
        setAiRanking(res.ai_ranking ?? null);
        setHasSearched(true);
      } catch (err) {
        if (controller.signal.aborted) return;
        setSearchError(err instanceof Error ? err.message : 'Search failed.');
      } finally {
        if (!controller.signal.aborted) {
          setSearching(false);
        }
      }
    },
    [vaultUnlocked],
  );

  // Run search automatically when initialQuery prop updates to a new non-empty value
  useEffect(() => {
    const trimmed = initialQuery.trim();
    if (trimmed && trimmed !== lastInitialQueryRef.current) {
      lastInitialQueryRef.current = trimmed;
      setQuery(trimmed);
      void runSearch(trimmed, category, deep, 1);
    }
  }, [initialQuery, category, deep, runSearch]);

  // Security: when vaultUnlocked transitions to false, immediately abort preview fetches,
  // clear all preview images/lightbox state, and clear/filter out adult search results.
  useEffect(() => {
    if (vaultUnlocked) return;

    previewAbortMapRef.current.forEach((ctrl) => ctrl.abort());
    previewAbortMapRef.current.clear();

    setPreviewsByGuid({});
    setPreviewLoading({});
    setPreviewErrors({});
    setExpandedPreviewGuid(null);
    setLightbox(null);

    // Library suggestions carry no category, so drop them all and let the next
    // keystroke fetch a locked list.
    setSuggestions([]);
    if (category === 'adult') {
      // The Adult tab only exists while the vault is open (stealth), so fall back to All.
      setCategory('all');
      setResults([]);
      setTotalFound(0);
      setTotalPages(0);
      setAiRanking(null);
    } else {
      const current = latestResultsRef.current;
      const kept = current.filter((r) => !r.is_adult && !isAdultCategory(r.category) && !r.preview_token);
      if (kept.length !== current.length) {
        setResults(kept);
        setTotalFound((t) => Math.max(0, t - (current.length - kept.length)));
      }
    }
  }, [vaultUnlocked, category]);

  // Global `/` shortcut to focus search bar when not in an input
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === '/' && !e.metaKey && !e.ctrlKey && !e.altKey && !isEditableElement(e.target)) {
        e.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  // Suggestions fetch with AbortController for q.length >= 2
  useEffect(() => {
    const trimmed = query.trim();
    if (trimmed.length < 2) {
      suggestAbortRef.current?.abort();
      setSuggestions([]);
      setActiveSuggestIdx(-1);
      return;
    }

    const timer = window.setTimeout(() => {
      suggestAbortRef.current?.abort();
      const controller = new AbortController();
      suggestAbortRef.current = controller;

      apiFetch<SuggestionItem[] | { suggestions?: SuggestionItem[] }>(
        `/api/suggest?q=${encodeURIComponent(trimmed)}`,
        { signal: controller.signal },
      )
        .then((data) => {
          if (controller.signal.aborted) return;
          const items = Array.isArray(data) ? data : data.suggestions || [];
          const safeItems = vaultUnlocked
            ? items
            : items.filter((s) => !isAdultCategory(s.category));
          setSuggestions(safeItems);
          setActiveSuggestIdx(-1);
        })
        .catch(() => {
          if (!controller.signal.aborted) {
            setSuggestions([]);
          }
        });
    }, 150);

    return () => {
      window.clearTimeout(timer);
      suggestAbortRef.current?.abort();
    };
  }, [query, vaultUnlocked]);

  // 180ms prefetch when navigating suggestions via arrow keys
  useEffect(() => {
    if (!suggestOpen || activeSuggestIdx < 0 || activeSuggestIdx >= suggestions.length) {
      return;
    }
    const target = suggestions[activeSuggestIdx];
    if (!target?.title) return;

    const timer = window.setTimeout(() => {
      prefetchAbortRef.current?.abort();
      const controller = new AbortController();
      prefetchAbortRef.current = controller;

      apiFetch<SearchResponse>('/api/search', {
        method: 'POST',
        signal: controller.signal,
        headers: { 'X-Prefetch': '1' },
        body: JSON.stringify({
          query: target.title,
          category,
          deep,
          limit: 60,
        }),
      }).catch(() => {});
    }, 180);

    return () => {
      window.clearTimeout(timer);
      prefetchAbortRef.current?.abort();
    };
  }, [activeSuggestIdx, suggestOpen, suggestions, category, deep]);

  // Cleanup all aborts on unmount
  useEffect(() => {
    return () => {
      searchAbortRef.current?.abort();
      suggestAbortRef.current?.abort();
      prefetchAbortRef.current?.abort();
      previewAbortMapRef.current.forEach((ctrl) => ctrl.abort());
      previewAbortMapRef.current.clear();
    };
  }, []);

  const handleLoadPreview = useCallback(
    async (item: SearchResultItem) => {
      if (!vaultUnlocked || !item.preview_token) return;

      if (expandedPreviewGuid === item.guid && previewsByGuid[item.guid]) {
        setExpandedPreviewGuid(null);
        return;
      }

      setExpandedPreviewGuid(item.guid);
      if (previewsByGuid[item.guid]?.length) return;

      previewAbortMapRef.current.get(item.guid)?.abort();
      const controller = new AbortController();
      previewAbortMapRef.current.set(item.guid, controller);

      setPreviewLoading((prev) => ({ ...prev, [item.guid]: true }));
      setPreviewErrors((prev) => {
        const next = { ...prev };
        delete next[item.guid];
        return next;
      });

      try {
        const res = await apiFetch<PreviewResponse>(
          `/api/vault/x/preview/${encodeURIComponent(item.preview_token)}`,
          { signal: controller.signal },
        );
        if (controller.signal.aborted) return;
        setPreviewsByGuid((prev) => ({ ...prev, [item.guid]: res.images || [] }));
      } catch (err) {
        if (controller.signal.aborted) return;
        const msg =
          err instanceof ApiError && err.status === 429
            ? 'Preview rate limit reached (429). Please wait a moment and try again.'
            : err instanceof Error
              ? err.message
              : 'Failed to load preview.';
        setPreviewErrors((prev) => ({ ...prev, [item.guid]: msg }));
      } finally {
        if (!controller.signal.aborted) {
          setPreviewLoading((prev) => ({ ...prev, [item.guid]: false }));
        }
      }
    },
    [vaultUnlocked, expandedPreviewGuid, previewsByGuid],
  );

  // Lightbox keyboard navigation
  useEffect(() => {
    if (!lightbox) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        setLightbox(null);
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault();
        setLightbox((prev) =>
          prev
            ? {
                ...prev,
                index: (prev.index - 1 + prev.images.length) % prev.images.length,
              }
            : null,
        );
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        setLightbox((prev) =>
          prev
            ? {
                ...prev,
                index: (prev.index + 1) % prev.images.length,
              }
            : null,
        );
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [lightbox]);

  const handleRecommendOne = async () => {
    if (category.toLowerCase() === 'adult' || results.length === 0 || aiLoading) return;
    setAiLoading(true);
    setAiError(null);
    try {
      const res = await apiFetch<AiRankingInfo>('/api/search/ai-pick', {
        method: 'POST',
        body: JSON.stringify({
          query: query.trim(),
          category,
          candidates: results.slice(0, 15),
        }),
      });
      setAiRanking(res);
    } catch (err) {
      setAiError(err instanceof Error ? err.message : 'AI recommendation failed.');
    } finally {
      setAiLoading(false);
    }
  };

  // `stream`: grab in sequential mode and open the player straight away.
  const handleQuickGrab = async (item: SearchResultItem, stream = false) => {
    setGrabbingMap((prev) => ({ ...prev, [item.guid]: true }));
    setGrabNotice(null);
    try {
      const res = await apiFetch<{ info_hash?: string | null }>('/api/grab/quick', {
        method: 'POST',
        body: JSON.stringify({
          guid: item.guid,
          title: item.title,
          download_url: item.download_url,
          magnet_url: item.magnet_url,
          info_hash: item.info_hash,
          category: item.category || category,
          indexer: item.indexer,
          stream,
        }),
      });
      setGrabbedMap((prev) => ({ ...prev, [item.guid]: true }));
      onQueueChanged();
      if (stream && res.info_hash) {
        setGrabNotice(`Streaming "${item.title}". It downloads in order, so playback can start in a moment.`);
        onOpenPlayer(`qb:${res.info_hash.toLowerCase()}`, item.title);
      } else if (stream) {
        setGrabNotice(`Queued "${item.title}" in streaming mode. Open it from Downloads once it starts.`);
      } else {
        setGrabNotice(`Queued "${item.title}" via Quick Grab.`);
      }
    } catch (err) {
      setGrabNotice(err instanceof Error ? err.message : 'Quick Grab failed.');
    } finally {
      setGrabbingMap((prev) => ({ ...prev, [item.guid]: false }));
    }
  };

  const handleArrGrab = async (item: SearchResultItem) => {
    const target = resolveArrTarget(item.category || category);
    setGrabbingMap((prev) => ({ ...prev, [item.guid]: true }));
    setGrabNotice(null);
    try {
      await apiFetch('/api/grab/arr', {
        method: 'POST',
        body: JSON.stringify({
          guid: item.guid,
          title: item.title,
          download_url: item.download_url,
          magnet_url: item.magnet_url,
          info_hash: item.info_hash,
          category: item.category || category,
          indexer: item.indexer,
          target,
        }),
      });
      setGrabbedMap((prev) => ({ ...prev, [item.guid]: true }));
      setGrabNotice(`Sent "${item.title}" to ${target}.`);
      onQueueChanged();
    } catch (err) {
      setGrabNotice(err instanceof Error ? err.message : 'Arr Grab failed.');
    } finally {
      setGrabbingMap((prev) => ({ ...prev, [item.guid]: false }));
    }
  };

  const handleInputKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (!suggestOpen || suggestions.length === 0) {
      if (e.key === 'ArrowDown' && suggestions.length > 0) {
        setSuggestOpen(true);
        setActiveSuggestIdx(0);
        e.preventDefault();
      }
      return;
    }

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveSuggestIdx((prev) => (prev + 1) % suggestions.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveSuggestIdx((prev) => (prev <= 0 ? suggestions.length - 1 : prev - 1));
    } else if (e.key === 'Escape') {
      setSuggestOpen(false);
      setActiveSuggestIdx(-1);
    } else if (e.key === 'Enter' && activeSuggestIdx >= 0) {
      e.preventDefault();
      const chosen = suggestions[activeSuggestIdx];
      if (chosen) {
        setQuery(chosen.title);
        setSuggestOpen(false);
        void runSearch(chosen.title, category, deep, 1);
      }
    }
  };

  const recommendDisabled = useMemo(
    () => category.toLowerCase() === 'adult' || results.length === 0 || aiLoading,
    [category, results.length, aiLoading],
  );

  return (
    <section ref={searchRootRef} className="bp-container" aria-label="Search">
      <LoadingBar active={searching} />
      <div className="bp-card" style={{ position: 'relative' }}>
        <form
          className="bp-search-form"
          style={{ marginTop: 0, maxWidth: '100%' }}
          onSubmit={(e) => {
            e.preventDefault();
            void runSearch(query, category, deep, 1);
          }}
        >
          <div style={{ position: 'relative', flex: 1 }}>
            <label htmlFor="muuvies-search-input" className="sr-only">
              Search media
            </label>
            <input
              id="muuvies-search-input"
              ref={inputRef}
              type="search"
              className="bp-search-input"
              style={{ width: '100%' }}
              placeholder="Search movies, shows, releases… (press / to focus)"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setSuggestOpen(true);
              }}
              onFocus={() => {
                if (suggestions.length > 0) setSuggestOpen(true);
              }}
              onBlur={() => {
                window.setTimeout(() => setSuggestOpen(false), 140);
              }}
              onKeyDown={handleInputKeyDown}
              aria-autocomplete="list"
              aria-expanded={suggestOpen && suggestions.length > 0}
              aria-controls="muuvies-suggest-list"
            />

            {suggestOpen && suggestions.length > 0 && (
              <ul
                id="muuvies-suggest-list"
                role="listbox"
                style={{
                  position: 'absolute',
                  top: 'calc(100% + 6px)',
                  left: 0,
                  right: 0,
                  zIndex: 40,
                  margin: 0,
                  padding: '0.4rem',
                  listStyle: 'none',
                  background: 'var(--bp-raised)',
                  border: '1px solid var(--bp-line-strong)',
                  borderRadius: 16,
                  maxHeight: 320,
                  overflowY: 'auto',
                }}
              >
                {suggestions.map((s, idx) => {
                  const active = idx === activeSuggestIdx;
                  return (
                    <li
                      key={`${s.title}-${idx}`}
                      role="option"
                      aria-selected={active}
                      onMouseDown={(e) => {
                        e.preventDefault();
                        setQuery(s.title);
                        setSuggestOpen(false);
                        void runSearch(s.title, category, deep, 1);
                      }}
                      onMouseEnter={() => setActiveSuggestIdx(idx)}
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        gap: '0.5rem',
                        padding: '0.55rem 0.85rem',
                        borderRadius: 10,
                        cursor: 'pointer',
                        background: active ? 'var(--bp-surface)' : 'transparent',
                      }}
                    >
                      <span style={{ fontWeight: active ? 600 : 400 }}>{s.title}</span>
                      <span style={{ display: 'inline-flex', gap: '0.35rem', alignItems: 'center' }}>
                        {s.year && <span className="bp-tag">{s.year}</span>}
                        {s.res && <span className="bp-tag">{s.res}</span>}
                        {s.category && <span className="bp-tag">{s.category}</span>}
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          <button
            type="submit"
            className="bp-pill bp-pill-solid bp-search-btn"
            disabled={searching || !query.trim()}
          >
            {searching ? 'Searching…' : 'Search'}
          </button>
        </form>

        <div className="bp-row" style={{ marginTop: '1rem' }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.45rem' }} role="tablist" aria-label="Search categories">
            {CATEGORIES.filter((cat) => cat.key !== 'adult' || vaultUnlocked).map((cat) => {
              const active = category === cat.key;
              return (
                <button
                  key={cat.key}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  className={`bp-pill ${active ? 'bp-pill-active' : 'bp-pill-ghost'}`}
                  onClick={() => {
                    setCategory(cat.key);
                    if (query.trim()) {
                      void runSearch(query, cat.key, deep, 1);
                    }
                  }}
                >
                  {cat.label}
                </button>
              );
            })}
          </div>

          <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '0.6rem' }}>
            <label
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '0.4rem',
                fontVariantNumeric: 'tabular-nums',
                fontSize: 13,
                cursor: 'pointer',
              }}
            >
              <input
                type="checkbox"
                checked={deep}
                onChange={(e) => setDeep(e.target.checked)}
              />
              Deep search
            </label>

            <button
              type="button"
              className="bp-pill bp-pill-ghost"
              disabled={recommendDisabled}
              onClick={() => void handleRecommendOne()}
              title={
                category.toLowerCase() === 'adult'
                  ? 'AI pick is disabled for Adult category'
                  : 'Pick the best release from top 15 candidates'
              }
            >
              {aiLoading ? 'Picking…' : 'Recommend one'}
            </button>
          </div>
        </div>

        {searchError && (
          <div className="bp-alert" style={{ marginTop: '1rem' }}>
            {searchError}
          </div>
        )}

        {aiError && (
          <div className="bp-alert" style={{ marginTop: '1rem' }}>
            {aiError}
          </div>
        )}

        {grabNotice && (
          <div
            key={grabNotice}
            data-motion-pop
            role="status"
            style={{
              marginTop: '1rem',
              padding: '0.75rem 1rem',
              borderRadius: 14,
              border: '1px solid var(--bp-line-strong)',
              background: 'var(--bp-raised)',
              fontSize: 13,
            }}
          >
            {grabNotice}
          </div>
        )}

        {aiRanking?.recommended_guid && (
          <div
            key={aiRanking.recommended_guid}
            data-motion-pop
            style={{
              marginTop: '1rem',
              padding: '0.9rem 1.1rem',
              borderRadius: 16,
              border: '1px solid var(--bp-red)',
              background: 'var(--bp-raised)',
            }}
          >
            <p className="bp-kicker">AI recommendation</p>
            <p style={{ margin: '0.35rem 0 0', fontSize: 14 }}>
              {aiRanking.recommendation_reason || 'Highlighted best release based on quality and seeders.'}
            </p>
          </div>
        )}
      </div>

      {hasSearched && (
        <div style={{ marginTop: '1.5rem' }}>
          <div className="bp-row" style={{ marginBottom: '0.85rem' }}>
            <p className="bp-kicker">
              {totalFound} RESULT{totalFound === 1 ? '' : 'S'} · PAGE {page} OF {Math.max(1, totalPages)}
            </p>
          </div>

          {results.length === 0 && !searching ? (
            <div className="bp-card">
              <p className="bp-muted" style={{ margin: 0 }}>
                No results found. Try enabling Deep Search or adjusting your query.
              </p>
            </div>
          ) : (
            <div ref={resultsRef} className="bp-list">
              {results.map((item) => {
                const isRecommended = aiRanking?.recommended_guid === item.guid;
                const isGrabbing = Boolean(grabbingMap[item.guid]);
                const isGrabbed = Boolean(grabbedMap[item.guid] || item.already_grabbed);
                const arrTarget = resolveArrTarget(item.category || category);
                const arrLabel =
                  arrTarget === 'sonarr'
                    ? 'Sonarr'
                    : arrTarget === 'whisparr'
                      ? 'Whisparr'
                      : 'Radarr';
                const canPlay = Boolean(item.in_library && item.library_item_id !== null && item.library_item_id !== undefined);
                const canPreview = Boolean(vaultUnlocked && item.preview_token);
                const previewImgs = previewsByGuid[item.guid] || [];
                const previewErr = previewErrors[item.guid];
                const isPreviewLoading = Boolean(previewLoading[item.guid]);
                const isPreviewExpanded = expandedPreviewGuid === item.guid;

                return (
                  <article
                    key={item.guid}
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (
                        (e.key === 'p' || e.key === 'P') &&
                        !isEditableElement(e.target) &&
                        canPreview
                      ) {
                        e.preventDefault();
                        void handleLoadPreview(item);
                      }
                    }}
                    className="bp-card"
                    style={{
                      padding: '1.1rem 1.25rem',
                      borderColor: isRecommended ? 'var(--bp-red)' : undefined,
                      background: isRecommended ? 'var(--bp-raised)' : undefined,
                    }}
                  >
                    <div className="bp-row" style={{ alignItems: 'flex-start' }}>
                      <div style={{ flex: '1 1 360px', minWidth: 0 }}>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem', alignItems: 'center' }}>
                          {isRecommended && <span className="bp-tag" style={{ borderColor: 'var(--bp-red)', color: 'var(--bp-red-text)' }}>AI pick</span>}
                          {item.res && <span className="bp-tag">{item.res}</span>}
                          {item.category && <span className="bp-tag">{item.category}</span>}
                          {item.indexer && <span className="bp-tag">{item.indexer}</span>}
                          {item.size_formatted && <span className="bp-tag">{item.size_formatted}</span>}
                          {typeof item.seeders === 'number' && (
                            <span className="bp-tag">Seeds {item.seeders}</span>
                          )}
                          {typeof item.muuvies_score === 'number' && (
                            <span className="bp-tag">Score {item.muuvies_score}</span>
                          )}
                          {(item.badges || []).map((badge, idx) => {
                            // The API sends {label, color}; rendering the object itself
                            // crashed the whole results list (React error #31).
                            const label = typeof badge === 'string' ? badge : badge?.label;
                            return label ? (
                              <span key={`${label}-${idx}`} className="bp-tag">
                                {label}
                              </span>
                            ) : null;
                          })}
                        </div>

                        <h3 style={{ margin: '0.5rem 0 0', fontSize: 16, lineHeight: 1.35, wordBreak: 'break-word' }}>
                          {item.title}
                        </h3>
                      </div>

                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.45rem', alignItems: 'center' }}>
                        {canPlay && (
                          <button
                            type="button"
                            className="bp-pill bp-pill-solid"
                            onMouseEnter={() => onPrefetchManifest(String(item.library_item_id))}
                            onFocus={() => onPrefetchManifest(String(item.library_item_id))}
                            onClick={() => onOpenPlayer(String(item.library_item_id), item.title)}
                          >
                            Play
                          </button>
                        )}

                        {canPreview && (
                          <button
                            type="button"
                            className="bp-pill bp-pill-ghost"
                            onClick={() => void handleLoadPreview(item)}
                            title="Toggle inline preview (shortcut: P)"
                          >
                            {isPreviewLoading ? 'Loading…' : isPreviewExpanded ? 'Hide preview' : 'Preview (P)'}
                          </button>
                        )}

                        <button
                          type="button"
                          className="bp-pill bp-pill-solid"
                          disabled={isGrabbing || isGrabbed || item.can_grab === false}
                          onClick={() => void handleQuickGrab(item)}
                        >
                          {isGrabbed ? 'Grabbed' : isGrabbing ? 'Grabbing…' : 'Quick grab'}
                        </button>

                        {!isGrabbed && (
                          <button
                            type="button"
                            className="bp-pill bp-pill-ghost"
                            disabled={isGrabbing || item.can_grab === false}
                            onClick={() => void handleQuickGrab(item, true)}
                            title="Grab it downloading in order and start watching as it arrives"
                          >
                            Stream
                          </button>
                        )}

                        <button
                          type="button"
                          className="bp-pill bp-pill-ghost"
                          disabled={isGrabbing || isGrabbed || item.can_grab === false}
                          onClick={() => void handleArrGrab(item)}
                        >
                          Send to {arrLabel}
                        </button>
                      </div>
                    </div>

                    {canPreview && isPreviewExpanded && (
                      <div style={{ marginTop: '0.9rem', borderTop: '1px solid var(--bp-line)', paddingTop: '0.9rem' }}>
                        {isPreviewLoading && (
                          <p className="bp-muted" style={{ margin: 0, fontSize: 13 }}>
                            Loading preview frames…
                          </p>
                        )}
                        {previewErr && (
                          <div className="bp-alert" style={{ fontSize: 13 }}>
                            {previewErr}
                          </div>
                        )}
                        {!isPreviewLoading && !previewErr && previewImgs.length > 0 && (
                          <div
                            style={{
                              display: 'grid',
                              gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))',
                              gap: '0.6rem',
                            }}
                          >
                            {previewImgs.map((img, idx) => (
                              <button
                                key={img.id}
                                type="button"
                                onClick={() =>
                                  setLightbox({
                                    guid: item.guid,
                                    title: item.title,
                                    images: previewImgs,
                                    index: idx,
                                  })
                                }
                                style={{
                                  padding: 0,
                                  border: '1px solid var(--bp-line)',
                                  borderRadius: 10,
                                  overflow: 'hidden',
                                  background: 'var(--bp-canvas)',
                                  cursor: 'zoom-in',
                                  aspectRatio: '16 / 9',
                                }}
                              >
                                <img
                                  src={`/api/vault/x/preview/img/${encodeURIComponent(img.id)}`}
                                  alt={`${item.title} preview ${idx + 1}`}
                                  referrerPolicy="no-referrer"
                                  loading="lazy"
                                  style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                                />
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </article>
                );
              })}
            </div>
          )}

          {totalPages > 1 && (
            <div className="bp-row" style={{ marginTop: '1.25rem', justifyContent: 'center' }}>
              <button
                type="button"
                className="bp-pill bp-pill-ghost"
                disabled={page <= 1 || searching}
                onClick={() => void runSearch(query, category, deep, page - 1)}
              >
                Previous
              </button>
              <span style={{ fontVariantNumeric: 'tabular-nums', fontSize: 13 }}>
                PAGE {page} / {totalPages}
              </span>
              <button
                type="button"
                className="bp-pill bp-pill-ghost"
                disabled={page >= totalPages || searching}
                onClick={() => void runSearch(query, category, deep, page + 1)}
              >
                Next
              </button>
            </div>
          )}
        </div>
      )}

      {lightbox && vaultUnlocked && (
        <div
          ref={lightboxBackdropRef}
          className="bp-modal-backdrop"
          role="dialog"
          aria-modal="true"
          aria-label={`Preview lightbox for ${lightbox.title}`}
          onPointerDown={lightboxDismiss.onPointerDown}
          onClick={lightboxDismiss.onClick}
          onTouchStart={(e) => {
            const t = e.touches[0];
            if (t) touchStartRef.current = { x: t.clientX, y: t.clientY };
          }}
          onTouchEnd={(e) => {
            const start = touchStartRef.current;
            const t = e.changedTouches[0];
            touchStartRef.current = null;
            if (!start || !t) return;
            const dx = t.clientX - start.x;
            const dy = t.clientY - start.y;
            if (dy > 60 && Math.abs(dy) > Math.abs(dx)) {
              setLightbox(null);
            } else if (dx > 40) {
              setLightbox((prev) =>
                prev
                  ? { ...prev, index: (prev.index - 1 + prev.images.length) % prev.images.length }
                  : null,
              );
            } else if (dx < -40) {
              setLightbox((prev) =>
                prev ? { ...prev, index: (prev.index + 1) % prev.images.length } : null,
              );
            }
          }}
        >
          <div
            ref={lightboxPanelRef}
            className="bp-modal bp-modal-wide"
            onClick={(e) => e.stopPropagation()}
            style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}
          >
            <div className="bp-row">
              <span className="bp-kicker">
                PREVIEW {lightbox.index + 1} / {lightbox.images.length}
              </span>
              <button
                type="button"
                className="bp-pill bp-pill-ghost"
                onClick={() => setLightbox(null)}
                aria-label="Close preview lightbox"
              >
                Close (Esc)
              </button>
            </div>

            <div style={{ display: 'flex', justifyContent: 'center', background: 'var(--bp-canvas)', borderRadius: 14, overflow: 'hidden' }}>
              <img
                src={`/api/vault/x/preview/img/${encodeURIComponent(lightbox.images[lightbox.index].id)}`}
                alt={`${lightbox.title} frame ${lightbox.index + 1}`}
                referrerPolicy="no-referrer"
                style={{ maxWidth: '100%', maxHeight: '70vh', objectFit: 'contain' }}
              />
            </div>

            <div className="bp-row">
              <button
                type="button"
                className="bp-pill bp-pill-ghost"
                onClick={() =>
                  setLightbox((prev) =>
                    prev
                      ? { ...prev, index: (prev.index - 1 + prev.images.length) % prev.images.length }
                      : null,
                  )
                }
              >
                PREV (←)
              </button>
              <button
                type="button"
                className="bp-pill bp-pill-ghost"
                onClick={() =>
                  setLightbox((prev) =>
                    prev ? { ...prev, index: (prev.index + 1) % prev.images.length } : null,
                  )
                }
              >
                Next (→)
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
