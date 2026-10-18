import type { RunProgress } from '../../api/progress';
import { isAbortError } from '../../api/client';

/** SSE snapshots invalidate older reads, and an aborted request can never publish into the next run. */
export function createProgressReader(load: (signal: AbortSignal) => Promise<RunProgress>, onProgress: (p: RunProgress) => void, onError: (error: unknown) => void) {
  let version = 0;
  return {
    receive(p: RunProgress) {
      version += 1;
      onProgress(p);
      onError(null);
    },
    async read(signal: AbortSignal): Promise<void> {
      const current = ++version;
      try {
        const p = await load(signal);
        if (signal.aborted || current !== version) return;
        onProgress(p);
        onError(null);
      } catch (error) {
        if (!signal.aborted && current === version && !isAbortError(error)) onError(error);
      }
    },
  };
}
