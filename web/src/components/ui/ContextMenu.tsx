// Context menu for an area that hosts many targets (canvas nodes, edges, the
// pane). P0 spike, not wired into features yet; see
// .trellis/tasks/10-17-ui-p0-tokens-primitives/research/canvas-context-menu.md.
//
// One Base UI ContextMenu covers the whole area. On each contextmenu event
// `resolveTarget` maps the DOM element under the pointer (or the focused
// element, for Shift+F10 / the Menu key) to a target; `items` builds the list
// for it. A null target or an empty list lets the browser show its own menu,
// so text fields keep copy / paste. The popup carries `nokey` so React Flow's
// document-level shortcuts (Delete, arrows) ignore keys pressed inside it.
//
// Touch long-press is ours, on pointer events: React Flow's d3 handlers stop
// native touchstart before it reaches React, so the library's own long-press
// never fires inside a canvas. While open, the library's modal backdrop takes
// wheel / drag, so the canvas can't pan away from the pinned menu.
// Skin: the shared `.menu` rules in app.css.

import { ContextMenu as BaseContextMenu } from '@base-ui/react/context-menu';
import type { BaseUIEvent } from '@base-ui/react/types';
import { Fragment, useEffect, useRef, useState, type MouseEvent, type PointerEvent, type ReactNode } from 'react';
import type { MenuItem } from './Menu';

type Anchor = Element | { getBoundingClientRect: () => DOMRect };

export interface ContextMenuAreaProps<T> {
  /** Accessible name of the menu. */
  label: string;
  /** The element under the pointer -> what the menu is about; null = native menu. */
  resolveTarget: (element: Element) => T | null;
  items: (target: T) => MenuItem[];
  /** Called with the target before the menu opens (e.g. select the node). */
  onOpenTarget?: (target: T) => void;
  className?: string;
  children: ReactNode;
}

interface OpenState {
  items: MenuItem[];
  anchor: Anchor;
  /** Focusable element the menu was opened on; null = library default. */
  returnTo: HTMLElement | null;
  /** Opened by long-press: keep the menu clear of the finger. */
  touch: boolean;
}

const TEXT_FIELD = 'input, textarea, select, [contenteditable=""], [contenteditable="true"]';
const LONG_PRESS_MS = 500;
const LONG_PRESS_SLOP = 10;
/** Gap below the touch point, so lifting the finger doesn't land on an item. */
const TOUCH_OFFSET = 12;

/** Keyboard-initiated events carry no pointer position (pointerType '' or 0,0). */
function isKeyboardEvent(e: MouseEvent): boolean {
  const pointerType = (e.nativeEvent as globalThis.PointerEvent).pointerType;
  return pointerType === '' || (e.clientX === 0 && e.clientY === 0);
}

function pointAnchor(x: number, y: number, size = 0): Anchor {
  return { getBoundingClientRect: () => DOMRect.fromRect({ x, y, width: size, height: size }) };
}

export function ContextMenuArea<T>({ label, resolveTarget, items, onOpenTarget, className, children }: ContextMenuAreaProps<T>) {
  const [open, setOpen] = useState<OpenState | null>(null);
  const press = useRef<{ timer: number; x: number; y: number } | null>(null);
  const cancelPress = () => {
    if (press.current) window.clearTimeout(press.current.timer);
    press.current = null;
  };
  useEffect(() => cancelPress, []);

  /** Resolve and open; false when the browser menu should show instead. */
  const openAt = (element: Element, anchor: Anchor, touch = false): boolean => {
    const target = element.closest(TEXT_FIELD) ? null : resolveTarget(element);
    const list = target === null ? [] : items(target);
    if (target === null || list.length === 0) return false;
    onOpenTarget?.(target);
    setOpen({ items: list, anchor, returnTo: element.closest<HTMLElement>('[tabindex]'), touch });
    return true;
  };

  // Runs before Base UI's own handler (rightmost props first).
  const onContextMenu = (e: BaseUIEvent<MouseEvent<HTMLDivElement>>) => {
    cancelPress();
    // Android also fires contextmenu for a long-press we already handled.
    if (open?.touch && (e.nativeEvent as globalThis.PointerEvent).pointerType === 'touch') {
      e.preventDefault();
      e.preventBaseUIHandler();
      return;
    }
    const element = e.target as Element;
    if (openAt(element, isKeyboardEvent(e) ? element : pointAnchor(e.clientX, e.clientY))) return;
    // Skip Base UI and keep the event away from its document listener,
    // which would otherwise suppress the native menu.
    e.preventBaseUIHandler();
    e.stopPropagation();
  };

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.pointerType !== 'touch' || !e.isPrimary) return;
    cancelPress();
    const element = e.target as Element;
    const { clientX: x, clientY: y } = e;
    const timer = window.setTimeout(() => {
      press.current = null;
      openAt(element, pointAnchor(x, y, 10), true);
    }, LONG_PRESS_MS);
    press.current = { timer, x, y };
  };

  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    const p = press.current;
    if (p && (Math.abs(e.clientX - p.x) > LONG_PRESS_SLOP || Math.abs(e.clientY - p.y) > LONG_PRESS_SLOP)) cancelPress();
  };

  return (
    <BaseContextMenu.Root open={open !== null} onOpenChange={(next) => !next && setOpen(null)}>
      <BaseContextMenu.Trigger
        className={className}
        onContextMenu={onContextMenu}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={cancelPress}
        onPointerCancel={cancelPress}
      >
        {children}
      </BaseContextMenu.Trigger>
      <BaseContextMenu.Portal>
        <BaseContextMenu.Positioner className="menu-positioner" anchor={open?.anchor} sideOffset={open?.touch ? TOUCH_OFFSET : undefined} collisionPadding={8}>
          {/* Focus goes back to the node the menu was opened on, else the library default (previous focus). */}
          <BaseContextMenu.Popup className="menu nokey" aria-label={label} finalFocus={open?.returnTo ? { current: open.returnTo } : undefined}>
            {open?.items.map((it) => (
              <Fragment key={it.id}>
                {it.separator && <BaseContextMenu.Separator className="menu-sep" />}
                <BaseContextMenu.Item className={it.danger ? 'menu-item danger' : 'menu-item'} disabled={it.disabled} onClick={it.onSelect}>
                  {it.icon}
                  <span>{it.label}</span>
                </BaseContextMenu.Item>
              </Fragment>
            ))}
          </BaseContextMenu.Popup>
        </BaseContextMenu.Positioner>
      </BaseContextMenu.Portal>
    </BaseContextMenu.Root>
  );
}
