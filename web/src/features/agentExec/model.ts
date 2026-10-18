// Pure rules of the agent execution pages (plan §5.5): merging the event stream by seq, turning events into
// timeline items (tool_call + tool_result paired by call_index, model_call only feeding badges and the budget),
// the budget rows against the frozen limits (unknown cost stays unknown), and the "N new events" follow state.

import {
  asKnown,
  type AgentExecution,
  type ExecutionBudget,
  type ExecutionEvent,
  type ExecutionUsed,
  type ModelCallPayload,
  type ToolCallPayload,
  type ToolResultPayload,
} from '../../api/agentExecutions';
import type { EvidenceRead } from '../../api/collaboration';

// ─── stream merge ──────────────────────────────────────────────────────────

/** Add events to the list: one entry per seq (a resumed stream may resend), ascending. */
export function mergeEvents(prev: readonly ExecutionEvent[], incoming: readonly ExecutionEvent[]): ExecutionEvent[] {
  if (!incoming.length) return prev as ExecutionEvent[];
  const last = prev.length ? prev[prev.length - 1].seq : -Infinity;
  // Fast path: strictly newer, in order (the live stream).
  let inOrder = true;
  let p = last;
  for (const e of incoming) {
    if (!(e.seq > p)) {
      inOrder = false;
      break;
    }
    p = e.seq;
  }
  if (inOrder) return [...prev, ...incoming];
  const bySeq = new Map<number, ExecutionEvent>();
  for (const e of prev) bySeq.set(e.seq, e);
  for (const e of incoming) if (!bySeq.has(e.seq)) bySeq.set(e.seq, e);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

export const lastSeqOf = (events: readonly ExecutionEvent[]) => (events.length ? events[events.length - 1].seq : 0);

// ─── timeline items ────────────────────────────────────────────────────────

interface ItemBase {
  /** Anchor: the first event of the item (outputs link to any of `seqs`). */
  seq: number;
  seqs: number[];
  at: string | null;
  simulated: boolean;
}

export type TimelineItem =
  | (ItemBase & { kind: 'system'; code: string; message: string; detail?: unknown })
  | (ItemBase & { kind: 'message'; text: string; call: ModelCallPayload | null })
  | (ItemBase & { kind: 'tool'; index: number; name: string; args: Record<string, unknown> | null; result: ToolResultPayload | null; resultAt: string | null })
  | (ItemBase & { kind: 'question'; questions: string[] })
  | (ItemBase & { kind: 'deliver'; deliverables: { asset_version_id?: string; slot_key?: string }[]; note: string })
  | (ItemBase & { kind: 'blocked'; reason: string; missing: string[] })
  // P4: a tool_agent review's criteria carry what it read (evidence).
  | (ItemBase & { kind: 'verdict'; criteria: { criterion_id: string; result: string; reason: string; evidence?: EvidenceRead[] }[]; summary: string })
  // P3 workspace agent: a terminal command, a file change, an action waiting for / decided by a person
  | (ItemBase & { kind: 'command'; command: string; exitCode: number | null; output: string; durationMs: number | null; timeout: boolean })
  | (ItemBase & { kind: 'file_change'; path: string; added: number; removed: number; diff: string })
  | (ItemBase & {
      kind: 'confirmation';
      id: string;
      state: string;
      action: string;
      risk: string;
      actions: string[];
      reason: string;
      by: string | null;
      decidedAt: string | null;
    })
  | (ItemBase & { kind: 'other'; type: string; payload: Record<string, unknown> });

export type TimelineKind = TimelineItem['kind'];

const sim = (p: unknown) => !!(p && typeof p === 'object' && (p as { simulated?: unknown }).simulated === true);

function questionText(q: unknown): string {
  if (typeof q === 'string') return q;
  if (q && typeof q === 'object') {
    const o = q as { question?: unknown; text?: unknown };
    if (typeof o.question === 'string') return o.question;
    if (typeof o.text === 'string') return o.text;
  }
  return String(q ?? '');
}

/**
 * Events → timeline items in order. tool_call and tool_result with the same call_index make one item (a result
 * without its call still shows); model_call is never an item — it becomes the badge of the message with the same
 * call_id; status only drives the header.
 */
export function buildTimeline(events: readonly ExecutionEvent[]): TimelineItem[] {
  const calls = new Map<string, ModelCallPayload>();
  for (const e of events) {
    const k = asKnown(e);
    if (k?.type === 'model_call' && k.payload.call_id) calls.set(k.payload.call_id, k.payload);
  }
  const items: TimelineItem[] = [];
  const tools = new Map<number, Extract<TimelineItem, { kind: 'tool' }>>();
  const confirmations = new Map<string, Extract<TimelineItem, { kind: 'confirmation' }>>();
  for (const e of events) {
    const base = { seq: e.seq, seqs: [e.seq], at: e.at, simulated: sim(e.payload) };
    const k = asKnown(e);
    if (!k) {
      items.push({ ...base, kind: 'other', type: e.type, payload: e.payload ?? {} });
      continue;
    }
    switch (k.type) {
      case 'model_call':
      case 'status':
        break;
      case 'system':
        items.push({ ...base, kind: 'system', code: k.payload.code, message: k.payload.message, detail: k.payload.detail });
        break;
      case 'message':
        items.push({ ...base, kind: 'message', text: k.payload.text ?? '', call: (k.payload.call_id && calls.get(k.payload.call_id)) || null });
        break;
      case 'tool_call': {
        const p: ToolCallPayload = k.payload;
        const item = { ...base, kind: 'tool' as const, index: p.call_index, name: p.name, args: p.args ?? null, result: null, resultAt: null };
        tools.set(p.call_index, item);
        items.push(item);
        break;
      }
      case 'tool_result': {
        const p = k.payload;
        const open = tools.get(p.call_index);
        if (open && !open.result) {
          open.result = p;
          open.resultAt = e.at;
          open.seqs.push(e.seq);
          open.simulated = open.simulated || base.simulated;
        } else {
          items.push({ ...base, kind: 'tool', index: p.call_index, name: p.name, args: null, result: p, resultAt: e.at });
        }
        break;
      }
      case 'question':
        items.push({ ...base, kind: 'question', questions: (Array.isArray(k.payload.questions) ? k.payload.questions : []).map(questionText).filter(Boolean) });
        break;
      case 'deliver':
        items.push({ ...base, kind: 'deliver', deliverables: Array.isArray(k.payload.deliverables) ? k.payload.deliverables : [], note: k.payload.note ?? '' });
        break;
      case 'blocked':
        items.push({ ...base, kind: 'blocked', reason: k.payload.reason ?? '', missing: Array.isArray(k.payload.missing) ? k.payload.missing.map(String) : [] });
        break;
      case 'verdict':
        items.push({ ...base, kind: 'verdict', criteria: Array.isArray(k.payload.criteria) ? k.payload.criteria : [], summary: k.payload.summary ?? '' });
        break;
      case 'command': {
        const p = k.payload;
        items.push({ ...base, kind: 'command', command: p.command ?? '', exitCode: p.exit_code ?? null, output: p.output_head ?? '', durationMs: p.duration_ms ?? null, timeout: !!p.timeout });
        break;
      }
      case 'file_change': {
        const p = k.payload;
        items.push({ ...base, kind: 'file_change', path: p.path ?? '', added: Number(p.added) || 0, removed: Number(p.removed) || 0, diff: p.diff_head ?? '' });
        break;
      }
      case 'confirmation': {
        // The pending event and the decision share confirmation_id: one item whose state follows the latest event.
        const p = k.payload;
        const open = confirmations.get(p.confirmation_id);
        if (open) {
          open.state = p.state;
          open.seqs.push(e.seq);
          if (p.state !== 'pending') {
            open.reason = p.reason ?? '';
            open.by = p.by?.name ?? null;
            open.decidedAt = e.at;
          }
          break;
        }
        const item = {
          ...base,
          kind: 'confirmation' as const,
          id: p.confirmation_id,
          state: p.state,
          action: p.action ?? '',
          risk: (p.risk ?? 'unknown').toLowerCase(),
          actions: Array.isArray(p.detail?.actions) ? p.detail.actions.map(String) : [],
          reason: p.state === 'pending' ? '' : p.reason ?? '',
          by: p.state === 'pending' ? null : p.by?.name ?? null,
          decidedAt: p.state === 'pending' ? null : e.at,
        };
        confirmations.set(p.confirmation_id, item);
        items.push(item);
        break;
      }
    }
  }
  return items;
}

/** Pending confirmations of a timeline (newest last). */
export const pendingConfirmations = (items: readonly TimelineItem[]) =>
  items.filter((i): i is Extract<TimelineItem, { kind: 'confirmation' }> => i.kind === 'confirmation' && i.state === 'pending');

/** Timeline filters (after the run ends). workspace: commands, file changes and confirmations (P3). */
export const TIMELINE_FILTERS = ['all', 'message', 'tool', 'workspace', 'need'] as const;
export type TimelineFilter = (typeof TIMELINE_FILTERS)[number];

export function matchesFilter(item: TimelineItem, f: TimelineFilter): boolean {
  switch (f) {
    case 'all':
      return true;
    case 'message':
      return item.kind === 'message';
    case 'tool':
      return item.kind === 'tool';
    case 'workspace':
      return item.kind === 'command' || item.kind === 'file_change' || item.kind === 'confirmation';
    case 'need':
      return (
        item.kind === 'question' ||
        item.kind === 'blocked' ||
        (item.kind === 'tool' && !!item.result && !item.result.ok) ||
        (item.kind === 'system' && NEEDS_ATTENTION.has(item.code)) ||
        (item.kind === 'command' && (item.timeout || (item.exitCode !== null && item.exitCode !== 0))) ||
        (item.kind === 'confirmation' && item.state !== 'approved')
      );
  }
}

const NEEDS_ATTENTION = new Set(['lease_lost', 'budget_exceeded', 'timeout', 'failed', 'stopped']);

/** Item anchor for an event seq (outputs and produced versions jump to the step that made them). */
export function itemAnchorFor(items: readonly TimelineItem[], seq: number | null | undefined): number | null {
  if (seq === null || seq === undefined) return null;
  const hit = items.find((i) => i.seqs.includes(seq));
  return hit ? hit.seq : null;
}

// ─── budget ────────────────────────────────────────────────────────────────

/**
 * Usage including events that arrived after the detail was loaded (seq > sinceSeq): every model_call is one step
 * plus its tokens, every tool_call one tool call. Cost and duration stay the server's (cost needs pricing).
 */
export function liveUsed(used: ExecutionUsed, events: readonly ExecutionEvent[], sinceSeq: number): ExecutionUsed {
  let steps = 0;
  let toolCalls = 0;
  let tin = 0;
  let tout = 0;
  for (const e of events) {
    if (e.seq <= sinceSeq) continue;
    const k = asKnown(e);
    if (k?.type === 'model_call') {
      steps += 1;
      tin += k.payload.tokens_in ?? 0;
      tout += k.payload.tokens_out ?? 0;
    } else if (k?.type === 'tool_call') toolCalls += 1;
  }
  if (!steps && !toolCalls) return used;
  return { ...used, steps: used.steps + steps, tool_calls: used.tool_calls + toolCalls, tokens_in: used.tokens_in + tin, tokens_out: used.tokens_out + tout };
}

export type BudgetKey = 'steps' | 'tool_calls' | 'tokens' | 'cost' | 'duration';

export interface BudgetRow {
  key: BudgetKey;
  /** null = unknown (cost without pricing): shown as "unknown", never 0. */
  used: number | null;
  /** null = no limit. */
  max: number | null;
  /** 0–100, null without a limit or a known value. */
  pct: number | null;
}

const pctOf = (used: number | null, max: number | null) => (used === null || max === null || max <= 0 ? null : Math.min(100, Math.round((used / max) * 100)));

/** Budget rows against the frozen limits. `durationMs` overrides the server's duration while running. */
export function budgetRows(budget: ExecutionBudget | null | undefined, used: ExecutionUsed | null | undefined, durationMs?: number | null): BudgetRow[] {
  const l = budget?.limits;
  const u = used ?? budget?.used;
  const num = (v: number | null | undefined) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const steps = num(u?.steps) ?? 0;
  const tools = num(u?.tool_calls) ?? 0;
  const tokens = (num(u?.tokens_in) ?? 0) + (num(u?.tokens_out) ?? 0);
  const cost = !u || u.cost_unknown || num(u.cost_fen) === null ? null : (u.cost_fen as number);
  const dur = num(durationMs) ?? num(u?.duration_ms) ?? 0;
  const maxSteps = num(l?.max_steps);
  const maxTools = num(l?.max_tool_calls);
  const maxCost = num(l?.max_cost_fen);
  const maxDur = num(l?.max_duration_s) === null ? null : (l!.max_duration_s as number) * 1000;
  return [
    { key: 'steps', used: steps, max: maxSteps, pct: pctOf(steps, maxSteps) },
    { key: 'tool_calls', used: tools, max: maxTools, pct: pctOf(tools, maxTools) },
    { key: 'tokens', used: tokens, max: null, pct: null },
    { key: 'cost', used: cost, max: maxCost, pct: pctOf(cost, maxCost) },
    { key: 'duration', used: dur, max: maxDur, pct: pctOf(dur, maxDur) },
  ];
}

export interface BudgetFormat {
  count: (n: number) => string;
  money: (fen: number) => string;
  duration: (ms: number) => string;
  /** Text for an unknown value (cost without pricing) — never "0". */
  unknown: string;
}

/** Display text of one budget row: `used`, and `max` (null = no limit). */
export function budgetText(row: BudgetRow, f: BudgetFormat): { used: string; max: string | null } {
  const one = (v: number) => (row.key === 'cost' ? f.money(v) : row.key === 'duration' ? f.duration(v) : f.count(v));
  return { used: row.used === null ? f.unknown : one(row.used), max: row.max === null ? null : one(row.max) };
}

// ─── status ────────────────────────────────────────────────────────────────

const LIVE_STATUSES = new Set(['running', 'queued', 'claimed', 'pending']);
export const isExecLive = (status: string | null | undefined) => !!status && LIVE_STATUSES.has(status);

/** The header state: the server status, refined by stop / pause / budget signals. */
export type ExecDisplay = 'running' | 'paused' | 'stopping' | 'question' | 'budget' | 'stopped' | string;

export function execDisplay(x: Pick<AgentExecution, 'status' | 'allowed_actions' | 'error'> & { stop_requested?: boolean }, events: readonly ExecutionEvent[] = []): ExecDisplay {
  const code = x.error?.code ?? '';
  if (isExecLive(x.status)) {
    if (x.allowed_actions?.includes('resume')) return 'paused';
    if (x.stop_requested) return 'stopping';
    return 'running';
  }
  if (code === 'budget_exceeded') return 'budget';
  if (code === 'agent_stopped') return 'stopped';
  if (x.status === 'blocked' && events.some((e) => e.type === 'question')) return 'question';
  return x.status;
}

/** Last visible item, as "what it is doing now". */
export function currentItem(items: readonly TimelineItem[]): TimelineItem | null {
  return items.length ? items[items.length - 1] : null;
}

// ─── follow the tail ("N new events") ──────────────────────────────────────

export interface TailState {
  /** The user is at the bottom: new items scroll into view. */
  pinned: boolean;
  /** Items added while the user looked further up. */
  unseen: number;
}

export const TAIL_START: TailState = { pinned: true, unseen: 0 };

/** New items arrived: count them only when the user scrolled away from the bottom. */
export function tailOnAppend(s: TailState, added: number): TailState {
  if (added <= 0 || s.pinned) return s;
  return { ...s, unseen: s.unseen + added };
}

/** The user scrolled: reaching the bottom pins again and clears the count. */
export function tailOnScroll(s: TailState, atBottom: boolean): TailState {
  if (atBottom) return s.pinned && !s.unseen ? s : { pinned: true, unseen: 0 };
  return s.pinned ? { ...s, pinned: false } : s;
}

export function isAtBottom(el: { scrollTop: number; scrollHeight: number; clientHeight: number }, slack = 48): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= slack;
}
