import { useEffect } from 'react';
import Lenis from 'lenis';
import { gsap, ScrollTrigger, prefersReducedMotion } from './gsap';
import './lenis.css';

let current: Lenis | null = null;

/** Scroll to a target, through Lenis when it is running. */
export function smoothScrollTo(target: number | HTMLElement): void {
  if (current) {
    current.scrollTo(target, { duration: 1.4 });
    return;
  }
  const top = typeof target === 'number' ? target : target.getBoundingClientRect().top + window.scrollY;
  window.scrollTo({ top, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
}

/** Pause Lenis scrolling (e.g., during the preloader overture or modal inspection). */
export function stopSmoothScroll(): void {
  current?.stop();
}

/** Resume Lenis scrolling after a lock. */
export function startSmoothScroll(): void {
  current?.start();
}

/** Scroll smoothly to a normalized [0, 1] document progress. */
export function scrollToProgress(progress: number): void {
  const maxScroll = Math.max(document.documentElement.scrollHeight - window.innerHeight, 0);
  const clamped = Math.min(Math.max(progress, 0), 1);
  smoothScrollTo(clamped * maxScroll);
}

/**
 * Lenis smooth scrolling, driven by GSAP's ticker so ScrollTrigger and Lenis
 * share one frame loop. Landing page only: the workspace keeps native scroll.
 * Inner scrollers opt out with `data-lenis-prevent`.
 */
export function useSmoothScroll(enabled = true): void {
  useEffect(() => {
    if (!enabled || prefersReducedMotion()) return;

    const lenis = new Lenis({ lerp: 0.1, wheelMultiplier: 1 });
    current = lenis;
    lenis.on('scroll', ScrollTrigger.update);

    const tick = (time: number) => lenis.raf(time * 1000);
    gsap.ticker.add(tick);
    gsap.ticker.lagSmoothing(0);

    return () => {
      gsap.ticker.remove(tick);
      gsap.ticker.lagSmoothing(500, 33);
      lenis.destroy();
      if (current === lenis) current = null;
    };
  }, [enabled]);
}
