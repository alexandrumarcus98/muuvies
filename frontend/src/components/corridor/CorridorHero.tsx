import { FormEvent, useEffect, useRef } from 'react';
import { gsap, useGSAP, prefersReducedMotion } from '../../motion/gsap';

interface CorridorHeroProps {
  query: string;
  onQueryChange: (next: string) => void;
  onSubmit: (e: FormEvent) => void;
  onEnterCorridor: () => void;
  /** True once preloader handoff finishes so the hero entrance reveal plays */
  revealed: boolean;
  /** 0 at top of page, 1 when hero has fully exited into the corridor */
  exitProgress: number;
}

export function CorridorHero({
  query,
  onQueryChange,
  onSubmit,
  onEnterCorridor,
  revealed,
  exitProgress,
}: CorridorHeroProps) {
  const rootRef = useRef<HTMLElement>(null);
  const glowRef = useRef<HTMLDivElement>(null);
  const svgStageRef = useRef<SVGSVGElement>(null);
  const ringOuterRef = useRef<SVGCircleElement>(null);
  const ringInnerRef = useRef<SVGCircleElement>(null);
  const frameRectRef = useRef<SVGRectElement>(null);
  const badgeRef = useRef<SVGSVGElement>(null);

  const line1Ref = useRef<HTMLSpanElement>(null);
  const line2Ref = useRef<HTMLSpanElement>(null);
  const subRef = useRef<HTMLParagraphElement>(null);
  const searchRef = useRef<HTMLFormElement>(null);
  const cueRef = useRef<HTMLDivElement>(null);
  const hasPlayedReveal = useRef(false);

  // Subtle pointer-reactive darkroom light.
  useEffect(() => {
    if (prefersReducedMotion()) return;
    const glow = glowRef.current;
    if (!glow) return;

    const xTo = gsap.quickTo(glow, 'x', { duration: 0.9, ease: 'power3.out' });
    const yTo = gsap.quickTo(glow, 'y', { duration: 0.9, ease: 'power3.out' });

    const onMove = (e: PointerEvent) => {
      const dx = (e.clientX / window.innerWidth - 0.5) * 140;
      const dy = (e.clientY / window.innerHeight - 0.5) * 110;
      xTo(dx);
      yTo(dy);
    };
    window.addEventListener('pointermove', onMove, { passive: true });
    return () => window.removeEventListener('pointermove', onMove);
  }, []);

  // Load-time SVG draw-in + kinetic text reveal once preloader completes.
  useGSAP(
    () => {
      if (!revealed || hasPlayedReveal.current) return;
      hasPlayedReveal.current = true;

      if (prefersReducedMotion()) {
        gsap.set(
          [line1Ref.current, line2Ref.current, subRef.current, searchRef.current, cueRef.current],
          { clearProps: 'all', opacity: 1 },
        );
        return;
      }

      const tl = gsap.timeline({ defaults: { ease: 'expo.out' } });

      // Draw SVG lens rings & anamorphic gate frame.
      const svgShapes = [ringOuterRef.current, ringInnerRef.current, frameRectRef.current].filter(
        (el): el is SVGCircleElement | SVGRectElement => el !== null,
      );
      for (const shape of svgShapes) {
        const len = shape.getTotalLength();
        gsap.set(shape, { strokeDasharray: len, strokeDashoffset: len });
      }
      tl.to(
        svgShapes,
        { strokeDashoffset: 0, duration: 1.45, stagger: 0.12, ease: 'power3.inOut' },
        0,
      );

      // Slow continuous rotation on the circular scroll badge.
      if (badgeRef.current) {
        gsap.to(badgeRef.current, {
          rotation: 360,
          duration: 24,
          repeat: -1,
          ease: 'none',
        });
      }

      tl.fromTo(
        [line1Ref.current, line2Ref.current],
        { yPercent: 120, skewY: 2.5, opacity: 0 },
        { yPercent: 0, skewY: 0, opacity: 1, duration: 1.1, stagger: 0.1 },
        0.08,
      )
        .fromTo(
          subRef.current,
          { y: 22, opacity: 0 },
          { y: 0, opacity: 1, duration: 0.9 },
          0.26,
        )
        .fromTo(
          searchRef.current,
          { clipPath: 'inset(0 100% 0 0)', opacity: 0 },
          { clipPath: 'inset(0 0% 0 0)', opacity: 1, duration: 0.95, ease: 'power4.out' },
          0.34,
        )
        .fromTo(
          cueRef.current,
          { y: 16, opacity: 0 },
          { y: 0, opacity: 1, duration: 0.8 },
          0.48,
        );
    },
    { scope: rootRef, dependencies: [revealed] },
  );

  const clamped = Math.min(Math.max(exitProgress, 0), 1);
  const isHidden = clamped >= 0.99;

  // Scroll-scrubbed aperture expansion + kinetic split into the 3D corridor.
  const svgScale = 1 + clamped * 1.45;
  const svgRotate = clamped * 32;
  const svgOpacity = 1 - Math.pow(clamped, 1.2);
  const line1Offset = -clamped * 68;
  const line2Offset = clamped * 56;
  const containerOpacity = 1 - Math.pow(clamped, 1.35);
  const containerY = -clamped * 52;

  return (
    <section
      ref={rootRef}
      className="cr-hero"
      aria-label="Muuvies introduction"
      style={{
        pointerEvents: isHidden ? 'none' : 'auto',
        visibility: isHidden ? 'hidden' : 'visible',
      }}
    >
      {/* Pointer-reactive darkroom ambient light */}
      <div
        ref={glowRef}
        className="cr-hero-glow"
        aria-hidden="true"
        style={{ opacity: 1 - clamped }}
      />

      {/* Animated SVG Anamorphic Viewfinder & Lens Aperture */}
      <svg
        ref={svgStageRef}
        className="cr-hero-svg-stage"
        viewBox="0 0 1200 800"
        preserveAspectRatio="xMidYMid slice"
        aria-hidden="true"
        style={
          clamped > 0
            ? {
                transform: `scale(${svgScale}) rotate(${svgRotate}deg)`,
                opacity: svgOpacity,
              }
            : undefined
        }
      >
        <rect
          ref={frameRectRef}
          x="120"
          y="140"
          width="960"
          height="520"
          fill="none"
          stroke="rgba(255,255,255,0.09)"
          strokeWidth="1"
        />
        <circle
          ref={ringOuterRef}
          cx="600"
          cy="400"
          r="310"
          fill="none"
          stroke="rgba(255,255,255,0.12)"
          strokeWidth="1"
        />
        <circle
          ref={ringInnerRef}
          cx="600"
          cy="400"
          r="195"
          fill="none"
          stroke="rgba(229,72,46,0.28)"
          strokeWidth="1"
        />
        {/* Corner registration ticks */}
        <path
          d="M 105 140 L 135 140 M 120 125 L 120 155 M 1065 140 L 1095 140 M 1080 125 L 1080 155 M 105 660 L 135 660 M 120 645 L 120 675 M 1065 660 L 1095 660 M 1080 645 L 1080 675"
          stroke="rgba(242,240,235,0.22)"
          strokeWidth="1"
          fill="none"
        />
      </svg>

      <div
        className="cr-hero-inner"
        style={
          clamped > 0
            ? {
                opacity: containerOpacity,
                transform: `translate3d(0, ${containerY}px, 0)`,
              }
            : undefined
        }
      >
        <h1 className="cr-hero-headline">
          <span className="cr-mask cr-hero-line-wrap">
            <span
              ref={line1Ref}
              className="cr-hero-line"
              style={{
                opacity: revealed ? undefined : 0,
                transform: clamped > 0 ? `translate3d(${line1Offset}px, 0, 0)` : undefined,
              }}
            >
              Find the cleanest release,
            </span>
          </span>
          <span className="cr-mask cr-hero-line-wrap">
            <span
              ref={line2Ref}
              className="cr-hero-line cr-serif-accent"
              style={{
                opacity: revealed ? undefined : 0,
                transform: clamped > 0 ? `translate3d(${line2Offset}px, 0, 0)` : undefined,
              }}
            >
              stream in 4K.
            </span>
          </span>
        </h1>

        <p
          ref={subRef}
          className="cr-hero-sub"
          style={{ opacity: revealed ? undefined : 0 }}
        >
          Search releases across your indexers, send grabs to your seedbox, and screen directly in the browser.
        </p>

        <form
          ref={searchRef}
          className="cr-hero-search"
          onSubmit={onSubmit}
          role="search"
          style={{ opacity: revealed ? undefined : 0 }}
        >
          <label htmlFor="cr-hero-search-input" className="sr-only">
            Search films or series
          </label>
          <input
            id="cr-hero-search-input"
            type="search"
            className="cr-hero-input"
            placeholder="Search a film, director, or year…"
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            autoComplete="off"
          />
          <button type="submit" className="cr-btn cr-btn-solid cr-hero-submit">
            Search
          </button>
        </form>

        <div
          ref={cueRef}
          className="cr-hero-cue"
          style={{ opacity: revealed ? undefined : 0 }}
        >
          <button type="button" className="cr-btn cr-btn-ghost" onClick={onEnterCorridor}>
            Enter 3D corridor
          </button>
          <span className="cr-label cr-muted cr-hide-mobile">or scroll to descend</span>
        </div>
      </div>

      {/* Rotating SVG Scroll Seal */}
      <button
        type="button"
        className="cr-scroll-seal cr-hide-mobile"
        onClick={onEnterCorridor}
        aria-label="Scroll into 3D corridor"
        style={{ opacity: (revealed ? 1 : 0) * (1 - clamped) }}
      >
        <svg ref={badgeRef} viewBox="0 0 120 120" className="cr-seal-svg" aria-hidden="true">
          <defs>
            <path
              id="cr-seal-path"
              d="M 60, 60 m -42, 0 a 42,42 0 1,1 84,0 a 42,42 0 1,1 -84,0"
            />
          </defs>
          <text className="cr-seal-text">
            <textPath href="#cr-seal-path" startOffset="0%">
              Scroll to descend · 3D cinema archive ·
            </textPath>
          </text>
        </svg>
        <svg viewBox="0 0 24 24" className="cr-seal-arrow" aria-hidden="true">
          <path d="M12 5v14M6 13l6 6 6-6" fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      </button>
    </section>
  );
}
