import type { RefObject } from 'react';
import { gsap, useGSAP, prefersReducedMotion } from './gsap';

/**
 * Fade-and-rise any `selector` matches inside `scope` that haven't been shown
 * yet, staggered. Re-runs when `deps` change, so new rows (a new results page,
 * a torrent added to the queue) animate in while rows already on screen stay
 * still. Total stagger is capped so long lists don't crawl in.
 */
export function useStaggerIn(scope: RefObject<HTMLElement | null>, selector: string, deps: unknown[]): void {
  useGSAP(
    () => {
      if (!scope.current) return;
      const fresh = gsap.utils
        .toArray<HTMLElement>(selector, scope.current)
        .filter((el) => el.dataset.motionIn !== '1');
      if (!fresh.length) return;
      fresh.forEach((el) => {
        el.dataset.motionIn = '1';
      });
      if (prefersReducedMotion()) return;
      gsap.fromTo(
        fresh,
        { autoAlpha: 0, y: 18 },
        {
          autoAlpha: 1,
          y: 0,
          duration: 0.6,
          ease: 'expo.out',
          stagger: { each: 0.045, amount: Math.min(0.5, fresh.length * 0.045) },
          clearProps: 'transform,opacity,visibility',
        },
      );
    },
    { scope, dependencies: deps },
  );
}
