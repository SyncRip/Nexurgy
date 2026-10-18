import { isAbortError } from '../../api/client';
import type { RunProgress } from '../../api/progress';
import { isTerminalProgress } from './model';

export type VisibilitySource = Pick<Document, 'visibilityState' | 'addEventListener' | 'removeEventListener'>;

export function wait(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', finish);
      resolve();
    };
    const timer = setTimeout(finish, ms);
    signal.addEventListener('abort', finish, { once: true });
  });
}

export function whenVisible(signal: AbortSignal, source: VisibilitySource | undefined = typeof document === 'undefined' ? undefined : document): Promise<void> {
  if (signal.aborted || !source || source.visibilityState !== 'hidden') return Promise.resolve();
  return new Promise((resolve) => {
    const finish = () => {
      source.removeEventListener('visibilitychange', onVisibility);
      signal.removeEventListener('abort', finish);
      resolve();
    };
    const onVisibility = () => {
      if (source.visibilityState !== 'hidden') finish();
    };
    source.addEventListener('visibilitychange', onVisibility);
    signal.addEventListener('abort', finish, { once: true });
  });
}

/** One final refresh for a finished card, or visible-tab polling for an expanded active card. */
export async function followProgress({ signal, active, expanded, load, onProgress, onError, visibility }: {
  signal: AbortSignal;
  active: boolean;
  expanded: boolean;
  load: () => Promise<RunProgress>;
  onProgress: (p: RunProgress) => void;
  onError: (e: unknown) => void;
  visibility?: VisibilitySource;
}): Promise<void> {
  if (active && !expanded) return;
  while (!signal.aborted) {
    await whenVisible(signal, visibility);
    if (signal.aborted) return;
    let terminal = false;
    try {
      const p = await load();
      if (signal.aborted) return;
      onProgress(p);
      terminal = isTerminalProgress(p);
    } catch (e) {
      if (signal.aborted || isAbortError(e)) return;
      onError(e);
    }
    if (!active || terminal) return;
    await wait(2000, signal);
  }
}
