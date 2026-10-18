// Tooltip on Base UI: opens on hover (after a delay) and on keyboard focus,
// closes on Esc / blur / pointer leave, and flips near viewport edges. A
// tooltip is a visual hint only: the trigger must already have an accessible
// name (visible text or aria-label, e.g. IconButton's `label`), because
// screen readers are not guaranteed to announce the popup.
// Wrap a region in <TooltipProvider> so neighbouring tooltips open instantly
// once one is showing.
// Skin: Tooltip.css (`pr-tooltip`).

import { Tooltip as BaseTooltip } from '@base-ui/react/tooltip';
import type { ReactElement, ReactNode } from 'react';
import './Tooltip.css';

export function TooltipProvider({ children, delay = 500 }: { children: ReactNode; delay?: number }) {
  return <BaseTooltip.Provider delay={delay}>{children}</BaseTooltip.Provider>;
}

export interface TooltipProps {
  content: ReactNode;
  /** A single focusable element (Button, IconButton, link…) that accepts ref and props. */
  children: ReactElement;
  side?: 'top' | 'bottom' | 'left' | 'right';
  align?: 'start' | 'center' | 'end';
  /** Opening delay in ms; falls back to the provider, then 600. */
  delay?: number;
  disabled?: boolean;
}

export function Tooltip({ content, children, side = 'top', align = 'center', delay, disabled }: TooltipProps) {
  return (
    <BaseTooltip.Root disabled={disabled}>
      <BaseTooltip.Trigger render={children} delay={delay} />
      <BaseTooltip.Portal>
        <BaseTooltip.Positioner className="pr-tooltip-positioner" side={side} align={align} sideOffset={6} collisionPadding={8}>
          <BaseTooltip.Popup className="pr-tooltip">{content}</BaseTooltip.Popup>
        </BaseTooltip.Positioner>
      </BaseTooltip.Portal>
    </BaseTooltip.Root>
  );
}
