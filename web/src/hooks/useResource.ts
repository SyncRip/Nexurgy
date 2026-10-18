// Minimal server-state hook: run an async loader when its deps change, abort
// stale requests, keep the previous data while reloading (no list flicker on
// search), and expose reload / local mutation.

import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react';
import { isAbortError } from '../api/client';

export interface Resource<T> {
  data: T | undefined;
  error: unknown;
  /** True while a request is in flight (also during reloads with stale data). */
  loading: boolean;
  /** True before the first successful load. */
  initial: boolean;
  reload: () => void;
  /** Replace the cached data locally (after a mutation that returned the new value). */
  setData: (updater: T | ((prev: T | undefined) => T)) => void;
}

export function useResource<T>(loader: (signal: AbortSignal) => Promise<T>, deps: DependencyList, opts: { enabled?: boolean } = {}): Resource<T> {
  const enabled = opts.enabled ?? true;
  const [data, setDataState] = useState<T | undefined>(undefined);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(enabled);
  const [nonce, setNonce] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const ctrl = new AbortController();
    setLoading(true);
    loaderRef
      .current(ctrl.signal)
      .then((d) => {
        if (ctrl.signal.aborted) return;
        setDataState(d);
        setError(null);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (ctrl.signal.aborted || isAbortError(e)) return;
        setError(e);
        setLoading(false);
      });
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, enabled]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const setData = useCallback((updater: T | ((prev: T | undefined) => T)) => {
    setDataState((prev) => (typeof updater === 'function' ? (updater as (p: T | undefined) => T)(prev) : updater));
  }, []);

  return { data, error, loading, initial: data === undefined, reload, setData };
}
