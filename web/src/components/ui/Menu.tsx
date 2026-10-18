// Dropdown menu built on Base UI: arrow-key / Home / End navigation, typeahead,
// Esc / outside press closes and returns focus to the trigger. The list is
// portaled to <body> with fixed positioning so scrolling tables never clip it,
// and it follows the trigger (flipping near the viewport edge) instead of
// closing on scroll. Non-modal: the page stays interactive while it is open.
// Clicks and Enter / Space stop at the trigger and the list so clickable table
// rows hosting a menu don't react.

import { Menu as BaseMenu } from '@base-ui/react/menu';
import { Fragment, type KeyboardEvent, type ReactNode, type SyntheticEvent } from 'react';

export interface MenuItem {
  id: string;
  label: ReactNode;
  icon?: ReactNode;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  /** Render a separator above this item. */
  separator?: boolean;
}

const stopPropagation = (e: SyntheticEvent) => e.stopPropagation();

const stopActivationKeys = (e: KeyboardEvent) => {
  if (e.key === 'Enter' || e.key === ' ') e.stopPropagation();
};

export function Menu({
  label,
  trigger,
  items,
  align = 'end',
  triggerClassName = 'btn ghost icon',
  header,
}: {
  /** Accessible name of the trigger. */
  label: string;
  trigger: ReactNode;
  items: MenuItem[];
  align?: 'start' | 'end';
  triggerClassName?: string;
  header?: ReactNode;
}) {
  return (
    <BaseMenu.Root modal={false}>
      <BaseMenu.Trigger className={triggerClassName} aria-label={label} onClick={stopPropagation} onKeyDown={stopActivationKeys}>
        {trigger}
      </BaseMenu.Trigger>
      <BaseMenu.Portal>
        <BaseMenu.Positioner className="menu-positioner" align={align} sideOffset={4} collisionPadding={8} positionMethod="fixed">
          {/* `nokey`: React Flow's document-level Delete / arrow handlers skip it. */}
          <BaseMenu.Popup className="menu nokey" aria-label={label} onClick={stopPropagation}>
            {header && <div className="menu-header">{header}</div>}
            {items.map((it) => (
              <Fragment key={it.id}>
                {it.separator && <BaseMenu.Separator className="menu-sep" />}
                {/* The item closes the menu after onSelect; focus goes back to the trigger unless onSelect moved it (e.g. into a dialog). */}
                <BaseMenu.Item className={it.danger ? 'menu-item danger' : 'menu-item'} disabled={it.disabled} onClick={it.onSelect}>
                  {it.icon}
                  <span>{it.label}</span>
                </BaseMenu.Item>
              </Fragment>
            ))}
          </BaseMenu.Popup>
        </BaseMenu.Positioner>
      </BaseMenu.Portal>
    </BaseMenu.Root>
  );
}
