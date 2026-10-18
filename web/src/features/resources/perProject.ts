// Loading one thing per project for a view scope (handoff §4, §10): nothing lists across projects, so the materials
// catalogue and the experience view ask each project of the scope, a bounded number at a time, and keep each answer
// (or failure) apart with its own retry. A new round (key, ids or reload) aborts the previous one, and an answer of
// an older round or an older retry of the same project never updates state.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { isAbortError } from '../../api/client';
import type { Project, UUID } from '../../api/types';
import type { ViewScope } from './types';

/** Projects loading at the same time when a scope spans several (the catalogue's are six request groups each). */
export const PROJECT_CONCURRENCY = 4;

/** One project's answer; `data` is its last one, kept while it reloads as `PerProjectOptions.keep` says. */
export interface PerProjectEntry<T> {
  status: 'loading' | 'ok' | 'error';
  data?: T;
  error?: unknown;
}

export interface PerProject<T> {
  /** One entry per id of the current round, in id order; empty while the ids are not known. */
  entries: ReadonlyMap<UUID, PerProjectEntry<T>>;
  /** Ask one project of the current round again (a failed load, or its data changed). */
  retry: (projectId: UUID) => void;
  /** Ask every project again, keeping their data on screen meanwhile. */
  reload: () => void;
}

export interface PerProjectOptions {
  /**
   * What a project's previous data is while it reloads:
   * - 'state' (default): its last known state under this key — kept through a failure, never shown for another key
   *   (a new scope never shows the old one's rows).
   * - 'placeholder': it only fills the gap while loading — kept across keys (a new search keeps the old rows on
   *   screen until it answers), dropped once the project fails.
   */
  keep?: 'state' | 'placeholder';
}

/** The projects a view scope loads: its one project, every accessible project (null while they load). */
export function scopeProjectIds(scope: ViewScope, projects: readonly Project[] | null): UUID[] | null {
  if (scope.kind === 'project') return [scope.projectId];
  return projects ? projects.map((p) => p.id) : null;
}

/** Run `fn` over `xs`, at most `limit` at a time. Nothing new starts once `signal` aborts; a rejection does not stop the others. */
export async function forEachBounded<T>(xs: readonly T[], limit: number, signal: AbortSignal, fn: (x: T) => Promise<void>): Promise<void> {
  let next = 0;
  const lane = async () => {
    while (next < xs.length && !signal.aborted) {
      try {
        await fn(xs[next++]);
      } catch {
        // the caller records its own failures
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(Math.max(1, limit), xs.length) }, lane));
}

interface Loaded<T> {
  /** Round the entries belong to (0 = none yet). */
  gen: number;
  key: string;
  /** `key` + ids: entries of another signature are stale for this render. */
  sig: string;
  entries: ReadonlyMap<UUID, PerProjectEntry<T>>;
}

/** Every id loading, with its previous data when `keep`; ids outside the list are dropped. */
export function reloading<T>(ids: readonly UUID[], prev: ReadonlyMap<UUID, PerProjectEntry<T>>, keep: boolean): Map<UUID, PerProjectEntry<T>> {
  return new Map(ids.map((id) => [id, { status: 'loading', data: keep ? prev.get(id)?.data : undefined }]));
}

interface Round {
  gen: number;
  /** A failure keeps the previous data ('state' mode). */
  keepOnError: boolean;
  ids: ReadonlySet<UUID>;
  ctrl: AbortController;
  /** The live request of each project (a retry aborts the one before it). */
  requests: Map<UUID, AbortController>;
}

const NONE: ReadonlyMap<UUID, never> = new Map<UUID, never>();

/**
 * Loads `load(projectId, signal)` for every id of `projectIds` (null = not known yet: nothing is asked). `key` names
 * what is loaded (scope, query); a change of `key` or of the ids starts a new round. `load` may be an inline callback:
 * it is read when a request starts, so its identity never restarts a round.
 */
export function usePerProject<T>(key: string, projectIds: readonly UUID[] | null, load: (projectId: UUID, signal: AbortSignal) => Promise<T>, opts: PerProjectOptions = {}): PerProject<T> {
  const placeholder = opts.keep === 'placeholder';
  const idsKey = projectIds ? projectIds.join(',') : null;
  const sig = `${key}\n${idsKey}`;

  const [loaded, setLoaded] = useState<Loaded<T>>({ gen: 0, key: '', sig: '', entries: NONE });
  const [nonce, setNonce] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  const genRef = useRef(0);
  const roundRef = useRef<Round | null>(null);

  /** Ask one project within `round`; resolves (never rejects) once its answer is recorded or dropped. */
  const fetchOne = useCallback((round: Round, id: UUID): Promise<void> => {
    round.requests.get(id)?.abort();
    const ctrl = new AbortController();
    round.requests.set(id, ctrl);
    const stop = () => ctrl.abort();
    if (round.ctrl.signal.aborted) stop();
    else round.ctrl.signal.addEventListener('abort', stop, { once: true });
    const put = (next: (prev: PerProjectEntry<T> | undefined) => PerProjectEntry<T>) =>
      setLoaded((s) => (s.gen !== round.gen || ctrl.signal.aborted ? s : { ...s, entries: new Map(s.entries).set(id, next(s.entries.get(id))) }));
    return new Promise<T>((resolve) => resolve(loadRef.current(id, ctrl.signal)))
      .then(
        (data) => put(() => ({ status: 'ok', data })),
        (error: unknown) => {
          if (!isAbortError(error)) put((prev) => ({ status: 'error', error, data: round.keepOnError ? prev?.data : undefined }));
        },
      )
      .finally(() => {
        round.ctrl.signal.removeEventListener('abort', stop);
        if (round.requests.get(id) === ctrl) round.requests.delete(id);
      });
  }, []);

  useEffect(() => {
    if (idsKey === null) return;
    const ids = idsKey ? idsKey.split(',') : [];
    const round: Round = { gen: ++genRef.current, keepOnError: !placeholder, ids: new Set(ids), ctrl: new AbortController(), requests: new Map() };
    roundRef.current = round;
    setLoaded((prev) => ({ gen: round.gen, key, sig, entries: reloading(ids, prev.entries, placeholder || prev.key === key) }));
    void forEachBounded(ids, PROJECT_CONCURRENCY, round.ctrl.signal, (id) => fetchOne(round, id));
    return () => round.ctrl.abort();
  }, [key, idsKey, sig, nonce, placeholder, fetchOne]);

  const retry = useCallback(
    (id: UUID) => {
      const round = roundRef.current;
      if (!round || round.ctrl.signal.aborted || !round.ids.has(id)) return;
      setLoaded((s) => (s.gen !== round.gen ? s : { ...s, entries: new Map(s.entries).set(id, { status: 'loading', data: s.entries.get(id)?.data }) }));
      void fetchOne(round, id);
    },
    [fetchOne],
  );
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  // Until the new round's effect runs, the previous answers read as loading (and as nothing for another key).
  const entries = useMemo((): ReadonlyMap<UUID, PerProjectEntry<T>> => {
    if (idsKey === null) return NONE;
    if (loaded.sig === sig) return loaded.entries;
    return reloading(idsKey ? idsKey.split(',') : [], loaded.entries, placeholder || loaded.key === key);
  }, [loaded, sig, idsKey, key, placeholder]);

  return useMemo(() => ({ entries, retry, reload }), [entries, retry, reload]);
}
