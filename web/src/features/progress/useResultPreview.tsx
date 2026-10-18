import { ImageOff, Loader2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useI18n } from '../../i18n/I18nProvider';
import { observeResultPreview, resultPreviewIdentity, type ResultPreviewState, type ResultPreviewTarget } from './resultPreview';

export function useResultPreview(target: ResultPreviewTarget | null) {
  const identity = resultPreviewIdentity(target);
  const [loaded, setLoaded] = useState<{ identity: string; state: ResultPreviewState } | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (!target) return;
    return observeResultPreview(target, (state) => setLoaded({ identity, state }));
    // The immutable identity includes every target field; polling objects must not restart image requests.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity, nonce]);
  const state: ResultPreviewState = !target ? { status: 'missing' }
    : loaded?.identity === identity ? loaded.state : { status: 'loading' };
  return {
    state,
    retry: () => setNonce((n) => n + 1),
    imageFailed: () => setLoaded({ identity, state: { status: 'error' } }),
  };
}

export function ResultPreview({ state, label, onError }: { state: ResultPreviewState; label: string; onError: () => void }) {
  const { t } = useI18n();
  if (state.status === 'ready') return <img src={state.url} alt={label} onError={onError} />;
  return <span className="prog-result-placeholder" aria-busy={state.status === 'loading'}>
    {state.status === 'loading' ? <Loader2 className="spin-icon" aria-hidden /> : <ImageOff aria-hidden />}
    <span>{t(state.status === 'loading' ? 'prog.result.loading' : state.status === 'error' ? 'prog.result.error' : 'prog.result.missing')}</span>
  </span>;
}
