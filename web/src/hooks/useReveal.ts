import { useEffect, useRef } from 'react';

/** Reduced motion when the OS asks for it or <html data-motion="reduced"> is set. */
export function prefersReduced(): boolean {
  if (document.documentElement.dataset.motion === 'reduced') return true;
  if (document.documentElement.dataset.motion === 'full') return false;
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * Scroll-in reveal: attach the returned ref to an element that starts hidden
 * (`.reveal` in CSS); it gains `.reveal-in` once it scrolls into view, then the
 * observer disconnects (reveal is one-way). Honours reduced motion by revealing
 * immediately with no transition. For a stagger, pass an index-based `delay`.
 */
export function useReveal<T extends HTMLElement = HTMLDivElement>(delay = 0) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (prefersReduced()) {
      el.classList.add('reveal-in');
      return;
    }
    const io = new IntersectionObserver(
      (entries, obs) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          if (delay) el.style.transitionDelay = `${delay}ms`;
          el.classList.add('reveal-in');
          obs.disconnect();
        }
      },
      { threshold: 0.12 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [delay]);
  return ref;
}
