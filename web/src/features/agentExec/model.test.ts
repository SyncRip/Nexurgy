import { describe, expect, it } from 'vitest';
import type { ExecutionEvent, ExecutionEventsPage, StreamHandlers } from '../../api/agentExecutions';
import { followExecution } from './follow';
import { budgetRows, budgetText, buildTimeline, matchesFilter, mergeEvents, pendingConfirmations, tailOnAppend, tailOnScroll, TAIL_START } from './model';

const ev = (seq: number, type: string, payload: Record<string, unknown> = {}): ExecutionEvent => ({ seq, type, at: null, payload });

describe('event stream', () => {
  it('back-fills, resumes from the last seq after a drop, never delivers a seq twice, stops at end', async () => {
    const pages: ExecutionEventsPage[] = [{ events: [ev(1, 'system', { code: 'started', message: '' }), ev(2, 'message', { text: 'plan' })], last_seq: 2, finished: false }];
    const afters: number[] = [];
    const streams: ((h: StreamHandlers) => void)[] = [
      // first connection: two new events, then the connection drops (no end)
      (h) => {
        h.onEvent(ev(3, 'tool_call', { call_index: 0, name: 'document', args: {} }));
        h.onEvent(ev(4, 'tool_result', { call_index: 0, name: 'document', ok: true, message: 'ok', produced: [] }));
      },
      // reconnect: the server resends 4, then 5 and end
      (h) => {
        h.onEvent(ev(4, 'tool_result', { call_index: 0, name: 'document', ok: true, message: 'ok', produced: [] }));
        h.onEvent(ev(5, 'status', { status: 'delivered' }));
        h.onEnd();
      },
    ];
    const delivered: number[] = [];
    let list: ExecutionEvent[] = [];
    const states: string[] = [];
    const last = await followExecution(
      {
        fetchPage: async () => pages.shift() ?? { events: [], last_seq: 2, finished: false },
        openStream: async (after, h) => {
          afters.push(after);
          streams.shift()?.(h);
        },
        sleep: async () => {},
        onEvents: (es) => {
          delivered.push(...es.map((e) => e.seq));
          list = mergeEvents(list, es);
        },
        onState: (s) => states.push(s),
      },
      new AbortController().signal,
    );
    expect(afters).toEqual([2, 4]);
    expect(delivered).toEqual([1, 2, 3, 4, 5]);
    expect(list.map((e) => e.seq)).toEqual([1, 2, 3, 4, 5]);
    expect(last).toBe(5);
    expect(states).toContain('reconnecting');
    expect(states[states.length - 1]).toBe('ended');
    // merging an out-of-order resend keeps one entry per seq, ascending
    expect(mergeEvents(list, [ev(3, 'tool_call'), ev(6, 'message', { text: 'x' })]).map((e) => e.seq)).toEqual([1, 2, 3, 4, 5, 6]);
  });
});

describe('timeline', () => {
  it('pairs tool_call with tool_result by call_index; model_call only badges its message', () => {
    const items = buildTimeline([
      ev(1, 'model_call', { call_id: 'c1', model_id: 'm', tokens_in: 100, tokens_out: 20, latency_ms: 900 }),
      ev(2, 'message', { text: 'I will read the drawing', call_id: 'c1' }),
      ev(3, 'tool_call', { call_index: 0, name: 'document', args: { q: 1 } }),
      ev(4, 'tool_call', { call_index: 1, name: 'process_plan', args: {} }),
      ev(5, 'tool_result', { call_index: 1, name: 'process_plan', ok: true, message: 'plan v3', produced: [{ asset_version_id: 'v', asset_type: 'process_plan', label: 'P3' }] }),
      ev(6, 'tool_result', { call_index: 0, name: 'document', ok: false, message: 'no such page', produced: [] }),
      ev(7, 'status', { status: 'running' }),
    ]);
    expect(items.map((i) => i.kind)).toEqual(['message', 'tool', 'tool']);
    const [msg, doc, plan] = items;
    expect(msg.kind === 'message' && msg.call?.tokens_in).toBe(100);
    expect(doc.kind === 'tool' && [doc.name, doc.result?.ok, doc.seqs]).toEqual(['document', false, [3, 6]]);
    expect(plan.kind === 'tool' && [plan.name, plan.result?.message, plan.seqs]).toEqual(['process_plan', 'plan v3', [4, 5]]);
  });

  it('workspace events (P3): one confirmation item follows its decision; failed commands need attention', () => {
    const items = buildTimeline([
      ev(1, 'command', { command: 'python make_bom.py', exit_code: 1, output_head: 'Traceback', duration_ms: 300 }),
      ev(2, 'file_change', { path: 'bom.csv', added: 3, removed: 0, diff_head: '+a,b' }),
      ev(3, 'confirmation', { confirmation_id: 'a1', state: 'pending', action: 'terminal: rm -rf build', risk: 'HIGH' }),
      ev(4, 'confirmation', { confirmation_id: 'a1', state: 'approved', reason: '', by: { id: 'u', name: 'Ada' } }),
      ev(5, 'confirmation', { confirmation_id: 'a2', state: 'pending', action: 'terminal: curl …', risk: 'high' }),
    ]);
    expect(items.map((i) => i.kind)).toEqual(['command', 'file_change', 'confirmation', 'confirmation']);
    const done = items[2];
    expect(done.kind === 'confirmation' && [done.state, done.risk, done.by, done.seqs]).toEqual(['approved', 'high', 'Ada', [3, 4]]);
    expect(pendingConfirmations(items).map((i) => i.id)).toEqual(['a2']);
    expect(items.filter((i) => matchesFilter(i, 'workspace'))).toHaveLength(4);
    expect(items.filter((i) => matchesFilter(i, 'need')).map((i) => i.seq)).toEqual([1, 5]);
  });
});

describe('follow the tail', () => {
  it('counts new items only while the user is scrolled up and clears at the bottom', () => {
    let s = tailOnAppend(TAIL_START, 2);
    expect(s).toEqual({ pinned: true, unseen: 0 });
    s = tailOnScroll(s, false);
    s = tailOnAppend(tailOnAppend(s, 1), 2);
    expect(s).toEqual({ pinned: false, unseen: 3 });
    expect(tailOnScroll(s, true)).toEqual({ pinned: true, unseen: 0 });
  });
});

describe('budget', () => {
  const f = { count: String, money: (fen: number) => `¥${(fen / 100).toFixed(2)}`, duration: (ms: number) => `${ms / 1000}s`, unknown: 'unknown' };
  it('shows an unpriced cost as unknown, never 0, and no limit as null', () => {
    const budget = {
      limits: { max_steps: 8, max_duration_s: 300, max_cost_fen: 500 },
      used: { steps: 3, tool_calls: 2, tokens_in: 1000, tokens_out: 200, cost_fen: 0, cost_unknown: true, duration_ms: 60000 },
    };
    const rows = budgetRows(budget, budget.used);
    const cost = rows.find((r) => r.key === 'cost')!;
    expect(cost.used).toBeNull();
    expect(budgetText(cost, f)).toEqual({ used: 'unknown', max: '¥5.00' });
    expect(budgetText(rows.find((r) => r.key === 'tool_calls')!, f)).toEqual({ used: '2', max: null });
    const priced = budgetRows(budget, { ...budget.used, cost_unknown: false, cost_fen: 42 }).find((r) => r.key === 'cost')!;
    expect(budgetText(priced, f).used).toBe('¥0.42');
  });
});
