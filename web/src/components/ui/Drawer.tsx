// Side drawer on Base UI's Drawer (a Dialog with swipe-to-dismiss). Same
// contract as Dialog: mounted = open, every dismissal (Esc, backdrop press,
// close button, swipe) goes to `onClose` and is ignored while `busy`. Focus is
// trapped, starts on a [data-autofocus] element or the panel itself, and
// returns to the opener on close.
// From md (1024px) up it slides in from the right (`size` md 480 / lg 740);
// below md it becomes a bottom sheet dismissed by swiping down. Text inside the
// body stays selectable with a mouse (Drawer.Content).
// Skin: Drawer.css (`pr-drawer`).

import { Drawer as BaseDrawer } from '@base-ui/react/drawer';
import { X } from 'lucide-react';
import { useRef, type ReactNode } from 'react';
import { useT } from '../../i18n/I18nProvider';
import { useMediaQuery } from '../../prefs/ThemeProvider';
import { minWidthQuery } from './breakpoints';
import { IconButton } from './Button';
import './Drawer.css';

export interface DrawerProps {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  subtitle?: ReactNode;
  footer?: ReactNode;
  headerActions?: ReactNode;
  size?: 'md' | 'lg';
  /** While busy, Esc / backdrop / swipe do not close (a request is in flight). */
  busy?: boolean;
}

export function Drawer({ title, onClose, children, subtitle, footer, headerActions, size = 'md', busy = false }: DrawerProps) {
  const t = useT();
  const popupRef = useRef<HTMLDivElement>(null);
  const wide = useMediaQuery(minWidthQuery('md'));
  const placement = wide ? 'side' : 'sheet';

  const onOpenChange = (open: boolean, details: BaseDrawer.Root.ChangeEventDetails) => {
    if (open) return;
    // Same rule as Dialog: a request in flight, or an inner control that already
    // consumed Escape, keeps the drawer open.
    if (busy || (details.reason === 'escape-key' && details.event.defaultPrevented)) {
      details.cancel();
      return;
    }
    onClose();
  };

  return (
    <BaseDrawer.Root open onOpenChange={onOpenChange} swipeDirection={wide ? 'right' : 'down'}>
      <BaseDrawer.Portal>
        <BaseDrawer.Backdrop className="pr-drawer-backdrop" />
        <BaseDrawer.Viewport className="pr-drawer-viewport" data-placement={placement}>
          <BaseDrawer.Popup
            ref={popupRef}
            // `nokey`: keeps React Flow's Delete / arrow handlers off (same as Dialog).
            className={`pr-drawer pr-drawer--${size} nokey`}
            data-placement={placement}
            // Global shortcuts check aria-modal before acting (same as Dialog).
            aria-modal="true"
            initialFocus={() => popupRef.current?.querySelector<HTMLElement>('[data-autofocus]') ?? popupRef.current ?? true}
          >
            {!wide && <div className="pr-drawer__handle" aria-hidden="true" />}
            <div className="pr-drawer__head">
              <div className="pr-drawer__titles">
                <BaseDrawer.Title className="pr-drawer__title">{title}</BaseDrawer.Title>
                {subtitle && <BaseDrawer.Description className="pr-drawer__sub">{subtitle}</BaseDrawer.Description>}
              </div>
              {headerActions && <div className="pr-drawer__actions">{headerActions}</div>}
              <BaseDrawer.Close render={<IconButton label={t('drawer.close')} icon={<X />} disabled={busy} />} />
            </div>
            <BaseDrawer.Content className="pr-drawer__body">{children}</BaseDrawer.Content>
            {footer && <div className="pr-drawer__foot">{footer}</div>}
          </BaseDrawer.Popup>
        </BaseDrawer.Viewport>
      </BaseDrawer.Portal>
    </BaseDrawer.Root>
  );
}
