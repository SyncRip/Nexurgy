// Toast stack built on Base UI: a polite live region (errors are announced
// assertively), hovering / focusing the stack pauses the timers, F6 jumps into
// it. `useToast()` keeps its signature. At most four toasts show at once
// (older ones are hidden until they time out); the newest sits at the bottom.

import { Toast as BaseToast } from '@base-ui/react/toast';
import { AlertTriangle, CheckCircle2, Info, X } from 'lucide-react';
import { createContext, useCallback, useContext, useState, type ReactNode } from 'react';
import { useT } from '../../i18n/I18nProvider';

export type ToastTone = 'ok' | 'err' | 'info';

interface ToastAction {
  label: ReactNode;
  onClick: () => void;
}

interface ToastData {
  tone: ToastTone;
  action?: ToastAction;
}

type Show = (message: ReactNode, opts?: { tone?: ToastTone; action?: ToastAction; durationMs?: number }) => void;

const MAX_VISIBLE = 4;

const TONE_ICON: Record<ToastTone, ReactNode> = {
  ok: <CheckCircle2 aria-hidden />,
  err: <AlertTriangle aria-hidden />,
  info: <Info aria-hidden />,
};

const ToastContext = createContext<Show | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [manager] = useState(() => BaseToast.createToastManager<ToastData>());

  const show = useCallback<Show>(
    (message, opts = {}) => {
      const tone = opts.tone ?? 'ok';
      manager.add({
        description: message,
        type: tone,
        priority: tone === 'err' ? 'high' : 'low',
        timeout: opts.durationMs ?? (tone === 'err' ? 7000 : 4200),
        data: { tone, action: opts.action },
      });
    },
    [manager],
  );

  return (
    <BaseToast.Provider toastManager={manager} limit={MAX_VISIBLE}>
      <ToastContext.Provider value={show}>{children}</ToastContext.Provider>
      <BaseToast.Portal>
        <ToastStack />
      </BaseToast.Portal>
    </BaseToast.Provider>
  );
}

function ToastStack() {
  const t = useT();
  const { toasts, close } = BaseToast.useToastManager<ToastData>();
  // The manager lists newest first; render oldest first so new toasts land at the bottom.
  const ordered = [...toasts].reverse();
  return (
    <BaseToast.Viewport className="toasts" aria-label={t('toast.region')}>
      {ordered.map((toast) => {
        const tone = toast.data?.tone ?? 'ok';
        const action = toast.data?.action;
        return (
          <BaseToast.Root key={toast.id} toast={toast} className={`toast ${tone}`}>
            {TONE_ICON[tone]}
            {/* A div, not the default <p>: messages may contain block content. */}
            <BaseToast.Description className="toast-msg" render={<div />}>
              {toast.description}
            </BaseToast.Description>
            {action && (
              <BaseToast.Action
                className="btn sm"
                onClick={() => {
                  action.onClick();
                  close(toast.id);
                }}
              >
                {action.label}
              </BaseToast.Action>
            )}
            {/* The stack never collapses, so keep the close button exposed (Base UI hides it while collapsed). */}
            <BaseToast.Close className="btn ghost icon sm" aria-label={t('toast.dismiss')} aria-hidden={false}>
              <X />
            </BaseToast.Close>
          </BaseToast.Root>
        );
      })}
    </BaseToast.Viewport>
  );
}

export function useToast(): Show {
  const v = useContext(ToastContext);
  if (!v) throw new Error('useToast must be used inside <ToastProvider>');
  return v;
}
