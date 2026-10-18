// Following one execution's events (design §6.5): back-fill with GET …/events?after=, then read the SSE stream
// from the last seq; after a dropped connection reconnect from the last seq (backoff), never re-delivering a seq;
// stop after `event: end`. I/O is injected so the loop is testable without a browser.

import { isApiError } from '../../api/client';
import type { ExecutionEvent, ExecutionEventsPage, StreamHandlers } from '../../api/agentExecutions';

export type FollowState = 'loading' | 'live' | 'reconnecting' | 'ended' | 'error';

export interface FollowDeps {
  fetchPage: (after: number, limit: number, signal: AbortSignal) => Promise<ExecutionEventsPage>;
  openStream: (after: number, h: StreamHandlers, signal: AbortSignal) => Promise<void>;
  sleep: (ms: number, signal: AbortSignal) => Promise<void>;
  /** Only events not delivered before (seq greater than the last one). */
  onEvents: (events: ExecutionEvent[]) => void;
  onState: (s: FollowState, error?: unknown) => void;
}

export const PAGE_LIMIT = 500;
const BACKOFF_START = 1000;
const BACKOFF_MAX = 15000;

/** 4xx other than timeout / rate limit will not get better by retrying. */
function fatal(e: unknown): boolean {
  return isApiError(e) && e.status >= 400 && e.status < 500 && e.status !== 408 && e.status !== 429;
}

export function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const h = setTimeout(done, ms);
    function done() {
      clearTimeout(h);
      signal.removeEventListener('abort', done);
      resolve();
    }
    signal.addEventListener('abort', done);
  });
}

/** Runs until the execution ended, a fatal error, or `signal` aborts. Returns the last seq delivered. */
export async function followExecution(deps: FollowDeps, signal: AbortSignal, startAfter = 0): Promise<number> {
  let last = startAfter;
  let delay = BACKOFF_START;
  const deliver = (events: ExecutionEvent[]) => {
    const fresh: ExecutionEvent[] = [];
    for (const e of [...events].sort((a, b) => a.seq - b.seq)) {
      if (e.seq > last) {
        fresh.push(e);
        last = e.seq;
      }
    }
    if (fresh.length) deps.onEvents(fresh);
  };
  const backoff = async () => {
    deps.onState('reconnecting');
    await deps.sleep(delay, signal);
    delay = Math.min(delay * 2, BACKOFF_MAX);
  };

  // 1. Back-fill what already happened.
  deps.onState('loading');
  for (;;) {
    if (signal.aborted) return last;
    let page: ExecutionEventsPage;
    try {
      page = await deps.fetchPage(last, PAGE_LIMIT, signal);
    } catch (e) {
      if (signal.aborted) return last;
      if (fatal(e)) {
        deps.onState('error', e);
        return last;
      }
      await backoff();
      continue;
    }
    delay = BACKOFF_START;
    const before = last;
    deliver(page.events ?? []);
    const more = (page.events?.length ?? 0) >= PAGE_LIMIT && last > before;
    if (more) continue;
    if (page.finished && last >= (page.last_seq ?? 0)) {
      deps.onState('ended');
      return last;
    }
    break;
  }

  // 2. Live: stream from the last seq, reconnecting from wherever it stopped.
  for (;;) {
    if (signal.aborted) return last;
    let ended = false;
    try {
      deps.onState('live');
      await deps.openStream(
        last,
        {
          onEvent: (e) => {
            delay = BACKOFF_START;
            deliver([e]);
          },
          onEnd: () => {
            ended = true;
          },
        },
        signal,
      );
    } catch (e) {
      if (signal.aborted) return last;
      if (fatal(e)) {
        deps.onState('error', e);
        return last;
      }
    }
    if (signal.aborted) return last;
    if (ended) {
      deps.onState('ended');
      return last;
    }
    await backoff();
  }
}
