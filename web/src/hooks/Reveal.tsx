import type { ReactNode } from 'react';
import { useReveal } from './useReveal';

/**
 * Wraps content in a scroll-in reveal (useReveal). Renders a <div class="reveal">
 * that fades/rises in when it enters the viewport; honours reduced motion. Pass
 * `delay` (ms) for a stagger across siblings, and `className` for layout.
 */
export function Reveal({ children, delay = 0, className = '' }: { children: ReactNode; delay?: number; className?: string }) {
  const ref = useReveal<HTMLDivElement>(delay);
  return (
    <div ref={ref} className={`reveal ${className}`.trim()}>
      {children}
    </div>
  );
}
