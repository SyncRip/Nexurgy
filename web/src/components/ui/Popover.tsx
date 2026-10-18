// Disclosure popover (non-modal) built on Base UI: Esc / outside press closes
// and returns focus to the trigger; the panel flips / shifts to stay on screen.
// It is portaled into the wrapper right after the trigger, so DOM order, Tab
// order and feature CSS scoped under the caller (`.as-head .popover`,
// `.nt-wrap .pop-positioner`, …) behave as before. Focus is not moved on open;
// content that wants it (the session switcher) focuses itself.

import { Popover as BasePopover } from '@base-ui/react/popover';
import { useCallback, useRef, useState, type ReactNode } from 'react';

export function Popover({
  trigger,
  label,
  children,
  triggerClassName = 'btn ghost',
  align = 'end',
}: {
  trigger: ReactNode;
  label: string;
  children: ReactNode | ((close: () => void) => ReactNode);
  triggerClassName?: string;
  align?: 'start' | 'end';
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);

  return (
    <div className="pop-wrap" ref={wrap}>
      <BasePopover.Root open={open} onOpenChange={setOpen}>
        {/* A disclosure, not a dialog: drop the library's aria-haspopup="dialog". */}
        <BasePopover.Trigger className={triggerClassName} aria-label={label} aria-haspopup={undefined}>
          {trigger}
        </BasePopover.Trigger>
        <BasePopover.Portal container={wrap}>
          <BasePopover.Positioner className="pop-positioner" align={align} sideOffset={6} collisionPadding={8}>
            <BasePopover.Popup className="popover nokey" role="region" aria-label={label} initialFocus={false}>
              {typeof children === 'function' ? children(close) : children}
            </BasePopover.Popup>
          </BasePopover.Positioner>
        </BasePopover.Portal>
      </BasePopover.Root>
    </div>
  );
}
