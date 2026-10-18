// Modal dialog built on Base UI: portal, focus trap, scroll lock, Esc / backdrop
// dismissal and focus return to the opener come from the library. Callers keep
// the "mounted = open" contract: the root is always open and every dismissal
// request is forwarded to `onClose` (ignored while `busy`). Nested dialogs and
// menus only affect the topmost layer.
// `placement="side"` renders the same modal as a full-height sheet on the right
// (full width on phones); it starts focus on the sheet itself (or a
// [data-autofocus] element) instead of the first form control. Its styles live
// with its first user (features/resources/detail.css).

import { Dialog as BaseDialog } from '@base-ui/react/dialog';
import { X } from 'lucide-react';
import { useRef, type KeyboardEvent, type ReactNode } from 'react';
import { useT } from '../../i18n/I18nProvider';

/** Element focused on open: [data-autofocus], else (centred only) the first field, the primary button, any footer button. */
function initialFocusTarget(root: HTMLElement, side: boolean): HTMLElement {
  const marked = root.querySelector<HTMLElement>('[data-autofocus]');
  if (marked || side) return marked ?? root;
  return (
    root.querySelector<HTMLElement>('.dlg-body input:not([disabled]), .dlg-body select:not([disabled]), .dlg-body textarea:not([disabled])') ??
    root.querySelector<HTMLElement>('.dlg-foot .btn.primary:not([disabled])') ??
    root.querySelector<HTMLElement>('.dlg-foot button:not([disabled])') ??
    root
  );
}

export interface DialogProps {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  headerActions?: ReactNode;
  onKeyDown?: (event: KeyboardEvent<HTMLDivElement>) => void;
  /** Short line under the title. */
  subtitle?: ReactNode;
  size?: 'sm' | 'md' | 'lg';
  /** While busy, Esc / backdrop do not close (a request is in flight). */
  busy?: boolean;
  /** Wrap the body + footer in a <form> and call this on submit. */
  onSubmit?: () => void;
  role?: 'dialog' | 'alertdialog';
  /** center (default): a centred dialog; side: a right-hand sheet (detail panels). */
  placement?: 'center' | 'side';
}

export function Dialog({ title, onClose, children, footer, headerActions, onKeyDown, subtitle, size = 'md', busy = false, onSubmit, role = 'dialog', placement = 'center' }: DialogProps) {
  const t = useT();
  const popupRef = useRef<HTMLDivElement>(null);
  const side = placement === 'side';

  const onOpenChange = (open: boolean, details: BaseDialog.Root.ChangeEventDetails) => {
    if (open) return;
    // Stay open while a request is in flight, or when an inner control
    // (e.g. a search box clearing itself) already consumed the Escape key.
    if (busy || (details.reason === 'escape-key' && details.event.defaultPrevented)) {
      details.cancel();
      return;
    }
    onClose();
  };

  const content = (
    <>
      <div className="dlg-body">{children}</div>
      {footer && <div className="dlg-foot">{footer}</div>}
    </>
  );

  return (
    <BaseDialog.Root open onOpenChange={onOpenChange}>
      <BaseDialog.Portal>
        {/* The viewport doubles as the scrim: pressing it is an outside press. */}
        <BaseDialog.Viewport className={side ? 'scrim scrim-side' : 'scrim'}>
          <BaseDialog.Popup
            ref={popupRef}
            // `nokey`: Delete / arrows typed here must not reach a React Flow canvas behind.
            className={side ? 'dlg dlg-side nokey' : `dlg dlg-${size} nokey`}
            // Overrides the root's role without AlertDialog's "no backdrop dismissal";
            // aria-modal is what global shortcuts check before acting.
            role={role}
            aria-modal="true"
            initialFocus={() => (popupRef.current ? initialFocusTarget(popupRef.current, side) : true)}
            onKeyDown={onKeyDown}
          >
            <div className="dlg-head">
              <div className="dlg-titles">
                <BaseDialog.Title>{title}</BaseDialog.Title>
                {subtitle && <BaseDialog.Description className="dlg-sub">{subtitle}</BaseDialog.Description>}
              </div>
              {headerActions && <div className="dlg-head-actions">{headerActions}</div>}
              <BaseDialog.Close className="btn ghost icon" aria-label={t('dialog.close')} disabled={busy}>
                <X />
              </BaseDialog.Close>
            </div>
            {onSubmit ? (
              <form
                className="dlg-form"
                noValidate
                onSubmit={(e) => {
                  e.preventDefault();
                  if (!busy) onSubmit();
                }}
              >
                {content}
              </form>
            ) : (
              content
            )}
          </BaseDialog.Popup>
        </BaseDialog.Viewport>
      </BaseDialog.Portal>
    </BaseDialog.Root>
  );
}

export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  danger,
  busy,
  error,
  onConfirm,
  onClose,
}: {
  title: ReactNode;
  body: ReactNode;
  confirmLabel: ReactNode;
  danger?: boolean;
  busy?: boolean;
  error?: ReactNode;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const t = useT();
  return (
    <Dialog
      title={title}
      onClose={onClose}
      size="sm"
      busy={busy}
      role="alertdialog"
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button type="button" className={`btn ${danger ? 'danger-solid' : 'primary'}`} onClick={onConfirm} disabled={busy} data-autofocus>
            {busy ? t('common.working') : confirmLabel}
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <p className="muted">{body}</p>
        {error}
      </div>
    </Dialog>
  );
}
