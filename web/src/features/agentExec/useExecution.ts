// One execution, live: the detail (GET /agent-executions/{id}) plus its event stream (followExecution). The detail
// is refreshed when an event changes what it shows (status, outputs, verdict, end); usage in between comes from
// the events themselves (model_call / tool_call), so the budget moves without polling.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getAgentExecution, listExecutionEvents, streamExecutionEvents, type AgentExecution, type ExecutionEvent } from '../../api/agentExecutions';
import { useResource, type Resource } from '../../hooks/useResource';
import { abortableSleep, followExecution, type FollowState } from './follow';
import { buildTimeline, liveUsed, mergeEvents, type TimelineItem } from './model';

const REFRESH_TYPES = new Set(['status', 'deliver', 'verdict', 'blocked', 'question']);
const REFRESH_MS = 1500;

export interface LiveExecution {
  detail: Resource<AgentExecution>;
  events: ExecutionEvent[];
  items: TimelineItem[];
  /** Detail usage plus events newer than the detail. */
  used: AgentExecution['budget']['used'] | null;
  state: FollowState;
  streamError: unknown;
  /** Stream ended (end event or finished page). */
  finished: boolean;
}

export function useExecutionLive(id: string | undefined, opts: { follow?: boolean } = {}): LiveExecution {
  const follow = opts.follow ?? true;
  const detail = useResource((signal) => getAgentExecution(id!, { signal }), [id], { enabled: !!id });
  const [events, setEvents] = useState<ExecutionEvent[]>([]);
  const [state, setState] = useState<FollowState>('loading');
  const [streamError, setStreamError] = useState<unknown>(null);
  const reloadRef = useRef(detail.reload);
  reloadRef.current = detail.reload;
  const timer = useRef<number | null>(null);

  const scheduleRefresh = useCallback((now = false) => {
    if (timer.current !== null) {
      if (!now) return;
      window.clearTimeout(timer.current);
    }
    timer.current = window.setTimeout(
      () => {
        timer.current = null;
        reloadRef.current();
      },
      now ? 0 : REFRESH_MS,
    );
  }, []);

  useEffect(() => {
    setEvents([]);
    setState('loading');
    setStreamError(null);
    if (!id || !follow) return;
    const ctrl = new AbortController();
    void followExecution(
      {
        fetchPage: (after, limit, signal) => listExecutionEvents(id, { after, limit }, { signal }),
        openStream: (after, h, signal) => streamExecutionEvents(id, after, h, { signal }),
        sleep: abortableSleep,
        onEvents: (fresh) => {
          setEvents((prev) => mergeEvents(prev, fresh));
          if (fresh.some((e) => REFRESH_TYPES.has(e.type) || (e.type === 'tool_result' && Array.isArray(e.payload?.produced) && (e.payload.produced as unknown[]).length > 0)))
            scheduleRefresh();
        },
        onState: (s, err) => {
          setState(s);
          if (s === 'error') setStreamError(err ?? null);
          if (s === 'ended') scheduleRefresh(true);
        },
      },
      ctrl.signal,
    );
    return () => {
      ctrl.abort();
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = null;
    };
  }, [id, follow, scheduleRefresh]);

  const items = useMemo(() => buildTimeline(events), [events]);
  const d = detail.data && detail.data.id === id ? detail.data : undefined;
  const used = useMemo(() => (d ? liveUsed(d.budget.used, events, d.last_seq ?? 0) : null), [d, events]);
  return { detail, events, items, used, state, streamError, finished: state === 'ended' };
}

/** The current time, ticking every second while `live` (elapsed time, lease remaining). */
export function useNow(live: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!live) return;
    setNow(Date.now());
    const h = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(h);
  }, [live]);
  return now;
}

/** Milliseconds between two ISO times (`to` missing: until `now`); null when `from` is missing. */
export function spanMs(from: string | null | undefined, to: string | null | undefined, now: number): number | null {
  const start = from ? Date.parse(from) : NaN;
  if (Number.isNaN(start)) return null;
  const end = to ? Date.parse(to) : now;
  return Math.max(0, (Number.isNaN(end) ? now : end) - start);
}
