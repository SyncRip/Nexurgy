// Live run state: initial GET, then the SSE stream (/runs/{id}/events), with a
// polling fallback when the stream cannot be opened or drops. All state comes
// from the server, so a refresh simply starts over from GET /runs/{id}.
// The same stream carries the run's progress view (`event: progress`, the execution process).

import { useCallback, useEffect, useMemo, useState } from 'react';
import { isAbortError } from '../../../api/client';
import { getRun, subscribeRunEvents } from '../../../api/endpoints';
import { getRunProgress, type RunProgress } from '../../../api/progress';
import type { WorkflowRun } from '../../../api/types';
import { TERMINAL_RUN } from '../model';
import { wait, whenVisible } from '../../progress/polling';
import { createProgressReader } from '../../progress/progressReader';
import { useDocumentVisible } from '../../progress/useDocumentVisible';

export type LiveMode = 'connecting' | 'live' | 'polling' | 'idle';

const POLL_MS = 2500;

/** `live: false` = the initial state only, no stream (e.g. a card scrolled out of view); turning it on reconnects.
 * `progress: true` also follows the run's progress view (the run page). */
export function useLiveRun(runId: string, { live = true, progress: withProgress = false }: { live?: boolean; progress?: boolean } = {}) {
  const [run, setRunState] = useState<WorkflowRun | undefined>(undefined);
  const [progress, setProgress] = useState<RunProgress | null>(null);
  const [progressError, setProgressError] = useState<unknown>(null);
  const [error, setError] = useState<unknown>(null);
  const [mode, setMode] = useState<LiveMode>('connecting');
  const [nonce, setNonce] = useState(0);
  const [lastUpdate, setLastUpdate] = useState<number | null>(null);
  const visible = useDocumentVisible();
  const progressReader = useMemo(() => createProgressReader(
    (signal) => getRunProgress(runId, { signal }), setProgress, setProgressError,
  ), [runId]);

  const setRun = useCallback((r: WorkflowRun) => {
    setRunState(r);
    setLastUpdate(Date.now());
  }, []);

  // Initial load (and manual reload).
  useEffect(() => {
    const ctrl = new AbortController();
    setError(null);
    void (async () => {
      await whenVisible(ctrl.signal);
      if (ctrl.signal.aborted) return;
      if (withProgress) void progressReader.read(ctrl.signal);
      try {
        const r = await getRun(runId, { signal: ctrl.signal });
        if (!ctrl.signal.aborted) setRun(r);
      } catch (e) {
        if (!ctrl.signal.aborted && !isAbortError(e)) setError(e);
      }
    })();
    return () => ctrl.abort();
  }, [runId, nonce, setRun, withProgress, progressReader]);

  const loaded = run !== undefined && run.id === runId;
  const terminal = loaded && TERMINAL_RUN.has(run.status);

  // This request survives cleanup of the live stream/poll effect when terminal run state arrives.
  // It also repairs a missed final SSE progress event without delaying the primary run result.
  useEffect(() => {
    if (!withProgress || !terminal) return;
    const ctrl = new AbortController();
    void (async () => {
      await whenVisible(ctrl.signal);
      if (!ctrl.signal.aborted) await progressReader.read(ctrl.signal);
    })();
    return () => ctrl.abort();
  }, [terminal, progressReader, withProgress, nonce]);

  // Live updates while the run is active.
  useEffect(() => {
    if (!loaded) return;
    if (terminal || !live || !visible) {
      setMode('idle');
      return;
    }
    const ctrl = new AbortController();
    let stopped = false;
    const poll = async () => {
      setMode('polling');
      while (!stopped) {
        await wait(POLL_MS, ctrl.signal);
        if (stopped) return;
        try {
          const r = await getRun(runId, { signal: ctrl.signal });
          if (stopped) return;
          setRun(r);
          setError(null);
          if (TERMINAL_RUN.has(r.status)) return;
          if (withProgress) await progressReader.read(ctrl.signal);
        } catch (e) {
          if (isAbortError(e)) return;
          setError(e);
        }
      }
    };
    setMode('connecting');
    subscribeRunEvents(
      runId,
      (e) => {
        if (stopped || !e.data || typeof e.data !== 'object') return;
        if (e.event === 'progress') {
          if (withProgress) progressReader.receive(e.data as RunProgress);
          return;
        }
        if (e.event !== 'run') return;
        setMode('live');
        setRun(e.data as WorkflowRun);
        setError(null);
      },
      { signal: ctrl.signal, progress: withProgress },
    )
      .then(() => {
        // Stream closed: the run finished (the effect re-runs on `terminal`) or the server dropped it.
        if (!stopped) void poll();
      })
      .catch(() => {
        if (!stopped) void poll();
      });
    return () => {
      stopped = true;
      ctrl.abort();
    };
  }, [runId, loaded, terminal, live, setRun, withProgress, visible, progressReader]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { run: loaded ? run : undefined, progress: progress && progress.run_id === runId ? progress : null, progressError, error, mode, reload, setRun, lastUpdate };
}
