import { ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import { gsap, useGSAP, prefersReducedMotion } from '../motion/gsap';

/**
 * Backdrop dismissal that only fires when the press both started and ended on
 * the backdrop. A plain `onClick` + `target === currentTarget` check also fires
 * when you press inside the panel (e.g. selecting text in an input) and release
 * outside it, which closed modals mid-typing.
 */
export function useBackdropDismiss(onDismiss: () => void) {
  const pressedOnBackdrop = useRef(false);
  const onPointerDown = useCallback((e: React.PointerEvent) => {
    pressedOnBackdrop.current = e.target === e.currentTarget;
  }, []);
  const onClick = useCallback(
    (e: React.MouseEvent) => {
      const startedHere = pressedOnBackdrop.current;
      pressedOnBackdrop.current = false;
      if (startedHere && e.target === e.currentTarget) onDismiss();
    },
    [onDismiss],
  );
  return { onPointerDown, onClick };
}

/** Fade the backdrop and lift the panel in when `active` turns true. */
export function useDialogEnter(
  backdrop: React.RefObject<HTMLElement | null>,
  panel: React.RefObject<HTMLElement | null>,
  active: boolean,
) {
  useGSAP(
    () => {
      if (!active || !backdrop.current || prefersReducedMotion()) return;
      // Opacity, not autoAlpha: a visibility:hidden panel can't take focus,
      // and the modal focuses its first field as soon as it opens.
      gsap.fromTo(backdrop.current, { opacity: 0 }, { opacity: 1, duration: 0.25, ease: 'power2.out' });
      if (panel.current) {
        gsap.fromTo(
          panel.current,
          { opacity: 0, y: 28, scale: 0.98 },
          { opacity: 1, y: 0, scale: 1, duration: 0.55, ease: 'expo.out', clearProps: 'transform' },
        );
      }
    },
    { dependencies: [active] },
  );
}

interface ModalProps {
  open: boolean;
  onClose: () => void;
  label: string;
  /** Extra classes for the panel, e.g. `bp-modal-wide`. */
  panelClassName?: string;
  children: ReactNode;
}

/**
 * Shared dialog: animated in and out, closes on Escape or a real backdrop
 * click, locks page scroll, moves focus into the panel and gives it back on
 * close. Children stay mounted until the exit animation has finished.
 */
export function Modal({ open, onClose, label, panelClassName = '', children }: ModalProps) {
  const [mounted, setMounted] = useState(open);
  const backdrop = useRef<HTMLDivElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  // Parents pass inline closures that change on every render (the shell
  // re-renders on each poll); keep the latest one without re-running effects.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const close = useCallback(() => onCloseRef.current(), []);
  const dismiss = useBackdropDismiss(close);

  if (open && !mounted) setMounted(true);

  useDialogEnter(backdrop, panel, open && mounted);

  // Exit animation, then unmount.
  useGSAP(
    () => {
      if (open || !mounted || !backdrop.current) return;
      if (prefersReducedMotion()) {
        setMounted(false);
        return;
      }
      gsap.to(panel.current, { autoAlpha: 0, y: 14, duration: 0.18, ease: 'power2.in' });
      gsap.to(backdrop.current, {
        autoAlpha: 0,
        duration: 0.22,
        delay: 0.04,
        ease: 'power2.in',
        onComplete: () => setMounted(false),
      });
    },
    { dependencies: [open, mounted] },
  );

  // Escape, scroll lock and focus handling while open.
  useEffect(() => {
    if (!open) return;
    returnFocus.current = document.activeElement as HTMLElement | null;
    const focusTarget =
      panel.current?.querySelector<HTMLElement>('input:not([type="hidden"]), select, textarea') ?? panel.current;
    focusTarget?.focus({ preventScroll: true });

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        close();
      }
    };
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
      returnFocus.current?.focus?.({ preventScroll: true });
    };
  }, [open, close]);

  if (!mounted) return null;

  return (
    <div
      ref={backdrop}
      className="bp-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label={label}
      onPointerDown={dismiss.onPointerDown}
      onClick={dismiss.onClick}
    >
      <div ref={panel} className={`bp-modal ${panelClassName}`.trim()} tabIndex={-1}>
        {children}
      </div>
    </div>
  );
}
