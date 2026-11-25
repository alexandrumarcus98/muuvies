import { useEffect, useRef, useState } from 'react';
import { gsap, useGSAP, prefersReducedMotion } from '../../motion/gsap';

interface PreloaderOvertureProps {
  /** Real asset readiness [0, 1] from LoadingManager + fonts + compileAsync */
  progress: number;
  /** True once textures, fonts, and shader compilation have all resolved */
  ready: boolean;
  /** Called once the title has docked into the navbar so Hero reveal & scroll can begin */
  onComplete: () => void;
}

export function PreloaderOverture({
  progress,
  ready,
  onComplete,
}: PreloaderOvertureProps) {
  const root = useRef<HTMLDivElement>(null);
  const backdropRef = useRef<HTMLDivElement>(null);
  const titleWrapRef = useRef<HTMLDivElement>(null);
  const charsRef = useRef<HTMLSpanElement[]>([]);
  const ruleRef = useRef<HTMLDivElement>(null);
  const counterWrapRef = useRef<HTMLDivElement>(null);
  const counterElRef = useRef<HTMLSpanElement>(null);

  const counterState = useRef({ val: 0 });
  const handoffStarted = useRef(false);
  const [done, setDone] = useState(false);

  const onCompleteRef = useRef(onComplete);
  onCompleteRef.current = onComplete;

  const word = 'Muuvies';

  // Entrance: unclipped per-character kinetic reveal.
  useGSAP(
    () => {
      if (prefersReducedMotion()) {
        setDone(true);
        onCompleteRef.current();
        return;
      }

      const chars = charsRef.current.filter(Boolean);
      if (chars.length > 0) {
        gsap.fromTo(
          chars,
          { yPercent: 120, opacity: 0 },
          {
            yPercent: 0,
            opacity: 1,
            duration: 0.95,
            stagger: 0.045,
            ease: 'power4.out',
          },
        );
      }
    },
    { scope: root },
  );

  // Tick monospace counter & hairline rule to real asset readiness.
  useEffect(() => {
    if (prefersReducedMotion() || done) return;
    const targetPct = Math.round(Math.min(Math.max(progress, 0), 1) * 100);

    gsap.to(counterState.current, {
      val: targetPct,
      duration: 0.4,
      ease: 'power2.out',
      snap: { val: 1 },
      overwrite: true,
      onUpdate: () => {
        const n = Math.round(counterState.current.val);
        if (counterElRef.current) {
          counterElRef.current.textContent = String(n).padStart(3, '0');
        }
        if (ruleRef.current) {
          ruleRef.current.style.transform = `scaleX(${n / 100})`;
        }
      },
    });
  }, [progress, done]);

  // Dock title smoothly into the navbar (outside any overflow:hidden mask) + curtain wipe.
  useEffect(() => {
    if (!ready || handoffStarted.current || prefersReducedMotion()) return;
    handoffStarted.current = true;

    const tl = gsap.timeline({
      delay: 0.15,
      onComplete: () => {
        setDone(true);
        onCompleteRef.current();
      },
    });

    tl.to(counterState.current, {
      val: 100,
      duration: 0.22,
      snap: { val: 1 },
      ease: 'power2.out',
      onUpdate: () => {
        const n = Math.round(counterState.current.val);
        if (counterElRef.current) {
          counterElRef.current.textContent = String(n).padStart(3, '0');
        }
        if (ruleRef.current) {
          ruleRef.current.style.transform = `scaleX(${n / 100})`;
        }
      },
    });

    const titleEl = titleWrapRef.current;
    const navTarget = document.querySelector<HTMLElement>('[data-nav-brand]');

    if (titleEl && navTarget) {
      // Remove mask clipping before moving across the viewport.
      const maskParent = titleEl.parentElement;
      if (maskParent) maskParent.style.overflow = 'visible';

      const fromRect = titleEl.getBoundingClientRect();
      const toRect = navTarget.getBoundingClientRect();
      const dx = toRect.left - fromRect.left;
      const dy = toRect.top - fromRect.top;
      const targetScale =
        toRect.height > 0 && fromRect.height > 0 ? toRect.height / fromRect.height : 0.2;

      tl.to(
        [counterWrapRef.current, ruleRef.current?.parentElement].filter(Boolean),
        { autoAlpha: 0, y: -12, duration: 0.25, ease: 'power2.in' },
        'dock',
      );

      // Fade the dark curtain early so the Hero reveals while the title glides to the nav.
      tl.to(
        backdropRef.current,
        {
          autoAlpha: 0,
          duration: 0.55,
          ease: 'power2.out',
        },
        'dock+=0.08',
      );

      tl.to(
        titleEl,
        {
          x: dx,
          y: dy,
          scale: targetScale,
          transformOrigin: 'top left',
          duration: 0.85,
          ease: 'expo.inOut',
        },
        'dock',
      );
    } else {
      tl.to(root.current, { autoAlpha: 0, duration: 0.45, ease: 'power2.out' });
    }
  }, [ready]);

  if (done) return null;

  return (
    <div ref={root} className="cr-preloader" aria-live="polite" aria-label="Loading Muuvies">
      <div ref={backdropRef} className="cr-preloader-backdrop" aria-hidden="true" />
      <div ref={counterWrapRef} className="cr-preloader-top">
        <span className="cr-mono-counter">
          <span ref={counterElRef}>000</span>
          <span className="cr-muted">%</span>
        </span>
      </div>

      <div className="cr-preloader-center">
        <div className="cr-mask cr-preloader-mask">
          <div ref={titleWrapRef} className="cr-preloader-title">
            {word.split('').map((char, idx) => (
              <span
                key={idx}
                ref={(el) => {
                  if (el) charsRef.current[idx] = el;
                }}
                className="cr-preloader-char"
              >
                {char}
              </span>
            ))}
          </div>
        </div>
        <div className="cr-hairline-track">
          <div ref={ruleRef} className="cr-hairline-fill" />
        </div>
      </div>

      <div className="cr-preloader-foot" />
    </div>
  );
}
