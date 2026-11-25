import { useRef } from 'react';
import { gsap, useGSAP, prefersReducedMotion } from '../motion/gsap';

/**
 * 2px accent bar pinned to the top of the viewport while something the user
 * asked for is loading (a search, the maintenance report). It eases towards
 * 85% while busy, then completes and fades. Not used for background polling.
 */
export function LoadingBar({ active }: { active: boolean }) {
  const bar = useRef<HTMLDivElement>(null);

  useGSAP(
    () => {
      const el = bar.current;
      if (!el) return;
      gsap.killTweensOf(el);
      if (prefersReducedMotion()) {
        gsap.set(el, { autoAlpha: active ? 1 : 0, scaleX: active ? 1 : 0 });
        return;
      }
      if (active) {
        gsap.set(el, { autoAlpha: 1, scaleX: 0 });
        gsap.to(el, { scaleX: 0.85, duration: 6, ease: 'power3.out' });
      } else if (gsap.getProperty(el, 'opacity') !== 0) {
        gsap
          .timeline()
          .to(el, { scaleX: 1, duration: 0.25, ease: 'power2.out' })
          .to(el, { autoAlpha: 0, duration: 0.3 }, '+=0.05');
      }
    },
    { dependencies: [active] },
  );

  return <div ref={bar} className="bp-loading-bar" aria-hidden="true" />;
}
