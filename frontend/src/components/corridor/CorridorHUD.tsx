import { useRef } from 'react';
import { gsap, useGSAP, prefersReducedMotion } from '../../motion/gsap';
import type { CorridorCategory, CorridorMediaItem } from './mediaCatalog';

interface CorridorHUDProps {
  items: readonly CorridorMediaItem[];
  category: CorridorCategory;
  onChangeCategory: (category: CorridorCategory) => void;
  activeIndex: number;
  zDepth: number;
  inCorridor: boolean;
  navReady: boolean;
  allowRegistration: boolean;
  onJumpToItem: (item: CorridorMediaItem) => void;
  onInspectItem: (item: CorridorMediaItem) => void;
}

export function CorridorHUD({
  items,
  category,
  onChangeCategory,
  activeIndex,
  zDepth,
  inCorridor,
  navReady,
  allowRegistration,
  onJumpToItem,
  onInspectItem,
}: CorridorHUDProps) {
  const active = items[activeIndex] ?? items[0];
  const metaRef = useRef<HTMLDivElement>(null);
  const firstRender = useRef(true);

  useGSAP(
    () => {
      if (firstRender.current) {
        firstRender.current = false;
        return;
      }
      if (prefersReducedMotion() || !metaRef.current || !inCorridor) return;
      gsap.fromTo(
        metaRef.current,
        { opacity: 0, y: 10 },
        { opacity: 1, y: 0, duration: 0.45, ease: 'power3.out', overwrite: true },
      );
    },
    { dependencies: [activeIndex, inCorridor] },
  );

  const formattedIndex = String(activeIndex + 1).padStart(2, '0');
  const formattedTotal = String(items.length).padStart(2, '0');
  const formattedZ = `${zDepth >= 0 ? '+' : ''}${zDepth.toFixed(1)}m`;

  return (
    <div className="cr-hud" aria-label="Archive navigation HUD">
      {/* Top Architectural Frame */}
      <header className="cr-hud-top">
        <div className="cr-brand-block" style={{ opacity: navReady ? 1 : 0 }}>
          <span className="cr-indicator-dot" aria-hidden="true" />
          <span className="cr-brand-name" data-nav-brand>
            Muuvies
          </span>
        </div>

        <div
          className="cr-hud-center"
          style={{
            opacity: inCorridor ? 1 : 0,
            pointerEvents: inCorridor ? 'auto' : 'none',
            visibility: inCorridor ? 'visible' : 'hidden',
          }}
        >
          <div className="cr-cat-switch" role="tablist" aria-label="Corridor collection">
            <button
              type="button"
              role="tab"
              aria-selected={category === 'trending'}
              className={`cr-cat-btn ${category === 'trending' ? 'is-active' : ''}`}
              onClick={() => onChangeCategory('trending')}
            >
              Trending
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={category === 'archival'}
              className={`cr-cat-btn ${category === 'archival' ? 'is-active' : ''}`}
              onClick={() => onChangeCategory('archival')}
            >
              Archival
            </button>
          </div>

          <span className="cr-divider-v cr-hide-mobile" aria-hidden="true" />

          <div className="cr-telemetry-center cr-hide-mobile" aria-live="polite">
            <span className="cr-telemetry-num">
              <span className="cr-accent-text">{formattedIndex}</span> / {formattedTotal}
            </span>
            <span className="cr-divider-v" aria-hidden="true" />
            <span className="cr-telemetry-num cr-muted">z {formattedZ}</span>
          </div>
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

      {/* Right-Edge Depth Rail (only in corridor) */}
      <aside
        className="cr-depth-rail"
        aria-label="Depth rail navigation"
        style={{
          opacity: inCorridor ? 1 : 0,
          pointerEvents: inCorridor ? 'auto' : 'none',
          visibility: inCorridor ? 'visible' : 'hidden',
        }}
      >
        {items.map((item, idx) => {
          const isCurrent = idx === activeIndex;
          return (
            <button
              key={item.id}
              type="button"
              className={`cr-rail-tick ${isCurrent ? 'is-active' : ''}`}
              onClick={() => onJumpToItem(item)}
              aria-label={`Jump to ${item.index}: ${item.title}`}
              aria-current={isCurrent ? 'true' : undefined}
              title={`${String(item.index).padStart(2, '0')} · ${item.title}`}
            >
              <span className="cr-rail-num">{String(item.index).padStart(2, '0')}</span>
              <span className="cr-rail-bar" aria-hidden="true" />
            </button>
          );
        })}
      </aside>

      {/* Bottom Framing: Proximity-Synced Metadata + Hints (only in corridor) */}
      <footer
        className="cr-hud-bottom"
        style={{
          opacity: inCorridor ? 1 : 0,
          pointerEvents: inCorridor ? 'auto' : 'none',
          visibility: inCorridor ? 'visible' : 'hidden',
        }}
      >
        <div ref={metaRef} className="cr-meta-block">
          <div className="cr-meta-kicker">
            <span className="cr-label cr-accent-text">{active.year}</span>
            <span className="cr-label cr-muted">{active.specs}</span>
          </div>
          <h2 className="cr-meta-title">{active.title}</h2>
          <p className="cr-meta-director">{active.director}</p>
          <div className="cr-meta-actions">
            <button
              type="button"
              className="cr-btn cr-btn-ghost"
              onClick={() => onInspectItem(active)}
            >
              Inspect print
            </button>
          </div>
        </div>

        <div className="cr-hints">
          <span className="cr-label cr-muted cr-hide-mobile">Scroll or use arrow keys · Click a frame to inspect</span>
          <span className="cr-label cr-muted cr-tmdb-credit">
            This product uses the TMDB API but is not endorsed or certified by TMDB.
          </span>
        </div>
      </footer>
    </div>
  );
}
