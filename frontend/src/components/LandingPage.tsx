import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import type { ThemeMode } from '../types/api';
import { ScrollTrigger, useGSAP, prefersReducedMotion } from '../motion/gsap';
import {
  scrollToProgress,
  startSmoothScroll,
  stopSmoothScroll,
  useSmoothScroll,
} from '../motion/useSmoothScroll';
import { CorridorScene } from './corridor/CorridorScene';
import {
  fetchCorridorCatalog,
  getStaticCatalog,
  type CorridorCategory,
  type CorridorMediaItem,
} from './corridor/mediaCatalog';
import { PreloaderOverture } from './corridor/PreloaderOverture';
import { CorridorHero } from './corridor/CorridorHero';
import { CorridorHUD } from './corridor/CorridorHUD';
import { InspectionModal } from './corridor/InspectionModal';
import './corridor/corridor.css';

interface LandingPageProps {
  allowRegistration: boolean;
  theme: ThemeMode;
  onToggleTheme: () => void;
}

/** First 18% of scroll exits the full-width hero while dollying camera z = +26 -> +10. */
const HERO_SPLIT = 0.18;

function buildLoginUrl(query: string): string {
  const trimmed = query.trim();
  return trimmed ? `/login?next=${encodeURIComponent(`/?q=${encodeURIComponent(trimmed)}`)}` : '/login';
}

export default function LandingPage({ allowRegistration }: LandingPageProps) {
  const reducedMotion = prefersReducedMotion();
  useSmoothScroll(!reducedMotion);

  const rootRef = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const canvasHostRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<CorridorScene | null>(null);

  const [category, setCategory] = useState<CorridorCategory>('trending');
  const [items, setItems] = useState<readonly CorridorMediaItem[]>(() => getStaticCatalog('trending'));
  const [loadProgress, setLoadProgress] = useState(0);
  const [sceneReady, setSceneReady] = useState(reducedMotion);
  const [unlocked, setUnlocked] = useState(reducedMotion);

  const [heroExit, setHeroExit] = useState(0);
  const [inCorridor, setInCorridor] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [zDepth, setZDepth] = useState(26);
  const [inspectedItem, setInspectedItem] = useState<CorridorMediaItem | null>(null);

  const [query, setQuery] = useState(() => {
    try {
      return sessionStorage.getItem('muuvies_pending_q') ?? '';
    } catch {
      return '';
    }
  });

  const updateQuery = (next: string) => {
    setQuery(next);
    try {
      const trimmed = next.trim();
      if (trimmed) sessionStorage.setItem('muuvies_pending_q', trimmed);
      else sessionStorage.removeItem('muuvies_pending_q');
    } catch {
      // ignore storage errors
    }
  };

  const mountedScene = useRef(false);

  // Swap corridor slabs when category changes (and fetch live feed in reduced-motion mode).
  useEffect(() => {
    if (!reducedMotion && !mountedScene.current) return;
    const controller = new AbortController();
    void fetchCorridorCatalog(category, controller.signal).then((nextItems) => {
      if (controller.signal.aborted) return;
      setItems(nextItems);
      setActiveIndex(0);
      if (sceneRef.current) {
        void sceneRef.current.swapItems(nextItems);
      }
    });
    return () => controller.abort();
  }, [category, reducedMotion]);

  // Mount WebGL corridor after fetching live trending feed so prepare() compiles the real slabs once.
  useEffect(() => {
    if (reducedMotion || !canvasHostRef.current) return;

    stopSmoothScroll();
    let active = true;
    const controller = new AbortController();

    void (async () => {
      const initialItems = await fetchCorridorCatalog('trending', controller.signal);
      if (!active || !canvasHostRef.current) return;
      setItems(initialItems);
      mountedScene.current = true;

      const scene = new CorridorScene(canvasHostRef.current, initialItems, {
        onProgress: (p) => {
          if (active) setLoadProgress(p);
        },
        onActiveChange: (idx, z) => {
          if (!active) return;
          setActiveIndex(idx);
          setZDepth(z);
        },
        onSelectItem: (item) => {
          if (active) setInspectedItem(item);
        },
      });
      sceneRef.current = scene;

      await scene.prepare();
      if (active) {
        setLoadProgress(1);
        setSceneReady(true);
      }
    })();

    return () => {
      active = false;
      mountedScene.current = false;
      controller.abort();
      sceneRef.current?.dispose();
      sceneRef.current = null;
    };
  }, [reducedMotion]);

  // Deterministic ScrollTrigger: 0..HERO_SPLIT exits hero & dollies z=+26->+10; HERO_SPLIT..1 flies corridor.
  useGSAP(
    () => {
      if (reducedMotion || !trackRef.current) return;
      ScrollTrigger.create({
        trigger: trackRef.current,
        start: 'top top',
        end: 'bottom bottom',
        scrub: true,
        onUpdate: (self) => {
          const p = self.progress;
          const scene = sceneRef.current;
          if (!scene) return;

          if (p < HERO_SPLIT) {
            const heroT = p / HERO_SPLIT;
            setHeroExit(heroT);
            setInCorridor(false);
            scene.setDollyZ(14 - heroT * 4);
            scene.setTargetProgress(0, 0);
          } else {
            const corridorT = (p - HERO_SPLIT) / (1 - HERO_SPLIT);
            setHeroExit(1);
            setInCorridor(true);
            scene.completeHandoff();
            scene.setTargetProgress(corridorT, self.getVelocity() / 1000);
          }
        },
      });
    },
    { scope: rootRef, dependencies: [reducedMotion] },
  );

  // Keyboard stepping between slabs once inside the corridor.
  useEffect(() => {
    if (reducedMotion || !unlocked) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (inspectedItem || !inCorridor) return;
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA')) return;

      e.preventDefault();
      const delta = e.key === 'ArrowDown' ? 1 : -1;
      const nextIdx = Math.min(Math.max(activeIndex + delta, 0), items.length - 1);
      const nextItem = items[nextIdx];
      if (nextItem) {
        scrollToProgress(HERO_SPLIT + nextItem.progress * (1 - HERO_SPLIT));
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [reducedMotion, unlocked, inCorridor, activeIndex, items, inspectedItem]);

  const handleOvertureComplete = useCallback(() => {
    setUnlocked(true);
    startSmoothScroll();
    ScrollTrigger.refresh();
  }, []);

  const handleEnterCorridor = useCallback(() => {
    const first = items[0];
    const targetP = first ? HERO_SPLIT + first.progress * (1 - HERO_SPLIT) : HERO_SPLIT + 0.02;
    scrollToProgress(targetP);
  }, [items]);

  const handleJumpToItem = useCallback((item: CorridorMediaItem) => {
    scrollToProgress(HERO_SPLIT + item.progress * (1 - HERO_SPLIT));
  }, []);

  const handleLaunchSearch = useCallback((title: string) => {
    updateQuery(title);
    window.location.assign(buildLoginUrl(title));
  }, []);

  const handleHeroSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!query.trim()) return;
    window.location.assign(buildLoginUrl(query));
  };

  if (reducedMotion) {
    return (
      <div className="cr-reduced-shell">
        <header className="cr-hud-top">
          <div className="cr-brand-block">
            <span className="cr-indicator-dot" aria-hidden="true" />
            <span className="cr-brand-name">Muuvies</span>
          </div>
          <div className="cr-cat-switch" role="tablist" aria-label="Collection">
            <button
              type="button"
              role="tab"
              aria-selected={category === 'trending'}
              className={`cr-cat-btn ${category === 'trending' ? 'is-active' : ''}`}
              onClick={() => setCategory('trending')}
            >
              Trending
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={category === 'archival'}
              className={`cr-cat-btn ${category === 'archival' ? 'is-active' : ''}`}
              onClick={() => setCategory('archival')}
            >
              Archival
            </button>
          </div>
          <nav className="cr-hud-actions" aria-label="Account">
            {allowRegistration && (
              <a href="/register" className="cr-btn cr-btn-ghost">
                Register
              </a>
            )}
            <a href="/login" className="cr-btn cr-btn-solid">
              Sign in
            </a>
          </nav>
        </header>

        <section style={{ marginTop: '2.5rem', maxWidth: '640px' }}>
          <h1 className="cr-hero-headline">
            Find the cleanest release, <span className="cr-serif-accent">stream in 4K.</span>
          </h1>
          <form className="cr-hero-search" onSubmit={handleHeroSubmit} role="search" style={{ marginTop: '1.25rem' }}>
            <input
              type="search"
              className="cr-hero-input"
              placeholder="Search a film, director, or year…"
              value={query}
              onChange={(e) => updateQuery(e.target.value)}
            />
            <button type="submit" className="cr-btn cr-btn-solid cr-hero-submit">
              Search
            </button>
          </form>
        </section>

        <main className="cr-reduced-grid" aria-label="Cinema catalog">
          {items.map((item) => (
            <article key={item.id} className="cr-reduced-card">
              <img src={item.imageSrc} alt={`${item.title} (${item.year})`} loading="lazy" />
              <span className="cr-label cr-accent-text">
                {String(item.index).padStart(2, '0')} · {item.year}
              </span>
              <h2 className="cr-meta-title" style={{ margin: 0, fontSize: '1.25rem' }}>
                {item.title}
              </h2>
              <span className="cr-label cr-muted">{item.specs}</span>
              <button
                type="button"
                className="cr-btn cr-btn-ghost"
                onClick={() => setInspectedItem(item)}
              >
                Inspect print
              </button>
            </article>
          ))}
        </main>

        <InspectionModal
          item={inspectedItem}
          onClose={() => setInspectedItem(null)}
          onLaunchSearch={handleLaunchSearch}
        />

        <p className="cr-label cr-muted cr-tmdb-note">
          This product uses the TMDB API but is not endorsed or certified by TMDB.
        </p>
      </div>
    );
  }

  return (
    <div ref={rootRef} className="cr-root">
      <div ref={canvasHostRef} className="cr-canvas-stage" aria-hidden="true" />

      {!unlocked && (
        <PreloaderOverture
          progress={loadProgress}
          ready={sceneReady}
          onComplete={handleOvertureComplete}
        />
      )}

      <CorridorHero
        query={query}
        onQueryChange={updateQuery}
        onSubmit={handleHeroSubmit}
        onEnterCorridor={handleEnterCorridor}
        revealed={unlocked}
        exitProgress={heroExit}
      />

      <CorridorHUD
        items={items}
        category={category}
        onChangeCategory={setCategory}
        activeIndex={activeIndex}
        zDepth={zDepth}
        inCorridor={inCorridor}
        navReady={unlocked}
        allowRegistration={allowRegistration}
        onJumpToItem={handleJumpToItem}
        onInspectItem={setInspectedItem}
      />

      <div ref={trackRef} className="cr-scroll-track" aria-hidden="true" />

      <InspectionModal
        item={inspectedItem}
        onClose={() => setInspectedItem(null)}
        onLaunchSearch={handleLaunchSearch}
      />
    </div>
  );
}
