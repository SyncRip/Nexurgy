// One subscription follows visible result galleries or expanded diagnostics in a visible tab; terminal cards refresh once.
import { useCallback, useEffect, useState } from 'react';
import { getRunProgress, type RunProgress } from '../../api/progress';
import { followProgress } from './polling';
import { useProgressDisclosure } from './useProgressDisclosure';

export function useRunProgress(runId: string | undefined, { active, refreshKey, followResults = false }: { active: boolean; refreshKey?: string; followResults?: boolean }) {
  const [progress, setProgress] = useState<RunProgress | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [nonce, setNonce] = useState(0);
  const current = progress?.run_id === runId ? progress : null;
  const { expanded, setExpanded } = useProgressDisclosure(runId ?? '', current, active);
  const following = followResults || expanded;

  useEffect(() => {
    if (!runId) return;
    const ctrl = new AbortController();
    setError(null);
    void followProgress({
      signal: ctrl.signal,
      active,
      expanded: following,
      load: () => getRunProgress(runId, { signal: ctrl.signal }),
      onProgress: (p) => {
        setProgress(p);
        setError(null);
      },
      onError: setError,
    });
    return () => ctrl.abort();
  }, [runId, active, following, refreshKey, nonce]);

  const reload = useCallback(() => {
    setExpanded(true);
    setNonce((n) => n + 1);
  }, [setExpanded]);
  return { progress: current, error, reload, expanded, setExpanded };
}
