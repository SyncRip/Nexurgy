// Generation reliability N (.trellis/tasks/archive/2026-10/10-13-generation-reliability/design.md §2.8, brief §4): the object-state
// vocabulary — what a generated object (a delivery, each object of a layered delivery) is — and its tone, shown apart
// from the check status (CHECK_STATUSES), never in its place; the Turbo boundary report of a delivery (which checks the
// policy closed, how often one really ran); and the execution context every entry shows (policy, where it came from,
// input versions, target, budget). Pure helpers (server: app/policy.py object_state, services/verification.py
// checks_report); unit-tested in status.test.ts. The badges are in StateBadge.tsx.

import type { ExecutionPolicy } from '../../api/engineering';
import type { ChecksReport, DeliveryLevels, SpecValue } from '../../api/orchestration';
import type { PolicySource, SourceVersion } from '../../api/turbo';
import type { Tone } from '../../components/ui/Badges';

export const OBJECT_STATES = ['verified', 'generated', 'generated_ai', 'partial', 'blocked', 'display_only', 'simulated'] as const;
export type ObjectState = (typeof OBJECT_STATES)[number];

/** verified ok · generated neutral · generated_ai information (never the failure style) · partial / display_only warning ·
 * blocked failure · simulated its own mark. */
export const STATE_TONE: Record<ObjectState, Tone> = {
  verified: 'ok',
  generated: 'pending',
  generated_ai: 'measure',
  partial: 'warn',
  blocked: 'err',
  display_only: 'warn',
  simulated: 'stale',
};

/** A state the server sent, or null for anything else (an unknown value is never read as success). */
export function asObjectState(s: unknown): ObjectState | null {
  return typeof s === 'string' && (OBJECT_STATES as readonly string[]).includes(s) ? (s as ObjectState) : null;
}

/** The tone of a state; an unknown one reads as a warning (never as success). */
export function stateTone(s: unknown): Tone {
  const st = asObjectState(s);
  return st ? STATE_TONE[st] : 'warn';
}

/** The state of the delivery a run made (its run summary): the ``delivery`` value among its node outputs; null when the
 * run made none (the run status says the rest). */
export function runDeliveryState(run: { nodes?: readonly { outputs?: Record<string, unknown> | null }[] } | null | undefined): ObjectState | null {
  for (const n of run?.nodes ?? []) {
    for (const v of Object.values(n.outputs ?? {})) {
      if (v && typeof v === 'object' && (v as { type?: unknown }).type === 'delivery') return asObjectState((v as { state?: unknown }).state);
    }
  }
  return null;
}

/** The objects of a layered delivery counted per state, in vocabulary order (only states that occur). */
export function levelStateCounts(levels: Pick<DeliveryLevels, 'assembly' | 'subassembly' | 'part'> | null | undefined): { state: ObjectState; n: number }[] {
  const n = new Map<ObjectState, number>();
  for (const o of [...(levels?.assembly ?? []), ...(levels?.subassembly ?? []), ...(levels?.part ?? [])]) {
    const st = asObjectState((o as { state?: unknown }).state);
    if (st) n.set(st, (n.get(st) ?? 0) + 1);
  }
  return OBJECT_STATES.filter((s) => n.has(s)).map((s) => ({ state: s, n: n.get(s)! }));
}

// ─── Turbo boundary report (manifest.checks) ────────────────────────────────

export type { ChecksReport };

export interface ChecksLine {
  /** turbo_clean: Turbo, no closed check ran; turbo_invoked: Turbo and a closed check ran (a defect to report);
   * standard: the checks ran as usual. */
  kind: 'turbo_clean' | 'turbo_invoked' | 'standard';
  closed: number;
  invokedClosed: number;
  /** Checks really run (all names). */
  invoked: number;
  /** Closed checks reached and not called. */
  skipped: number;
  /** Some steps of the run kept no ledger: the numbers do not cover them. */
  incomplete: boolean;
}

const sum = (r: Record<string, number> | undefined) => Object.values(r ?? {}).reduce((a, b) => a + (Number(b) || 0), 0);

export function checksLine(r: ChecksReport | null | undefined): ChecksLine | null {
  if (!r || !Array.isArray(r.closed)) return null;
  const invokedClosed = Number(r.invoked_closed) || 0;
  return {
    kind: r.policy === 'turbo' ? (invokedClosed > 0 ? 'turbo_invoked' : 'turbo_clean') : 'standard',
    closed: r.closed.length,
    invokedClosed,
    invoked: sum(r.invoked),
    skipped: sum(r.skipped),
    incomplete: (r.nodes_unrecorded?.length ?? 0) > 0 || (r.tool_calls_unrecorded ?? 0) > 0,
  };
}

// ─── execution context (run card, operation card, Agent tool results) ───────

/** The run's frozen budget (package J: ``budget`` on the run snapshot). */
export interface RunBudget {
  max_calls?: number | null;
  max_seconds?: number | null;
}

export interface ExecutionContext {
  policy: ExecutionPolicy;
  /** null: not recorded (requests from before S5). */
  source: PolicySource | null;
  /** The version the policy was inherited from (inherited) or the request continues from. */
  sourceLabel: string | null;
  /** The exact input versions, as labels. */
  inputs: string[];
  target: string | null;
  budget: RunBudget | null;
}

export function asPolicy(p: unknown): ExecutionPolicy {
  return p === 'turbo' ? 'turbo' : 'standard';
}

function asSource(s: unknown): PolicySource | null {
  return s === 'explicit' || s === 'inherited' || s === 'project_default' || s === 'default' ? s : null;
}

export function asBudget(b: unknown): RunBudget | null {
  if (!b || typeof b !== 'object') return null;
  const { max_calls, max_seconds } = b as RunBudget;
  const calls = typeof max_calls === 'number' && Number.isFinite(max_calls) ? max_calls : null;
  const seconds = typeof max_seconds === 'number' && Number.isFinite(max_seconds) ? max_seconds : null;
  return calls === null && seconds === null ? null : { max_calls: calls, max_seconds: seconds };
}

/** "12 min" / "45 s" parts of a budget (null when not set). */
export function budgetParts(b: RunBudget | null | undefined): { calls: number | null; minutes: number | null; seconds: number | null } {
  const s = b?.max_seconds ?? null;
  return { calls: b?.max_calls ?? null, minutes: s !== null && s >= 60 ? Math.round(s / 60) : null, seconds: s !== null && s < 60 ? s : null };
}

const short = (id: string) => `#${id.slice(0, 8)}`;

interface RunLike {
  execution_policy?: ExecutionPolicy | string | null;
  policy_source?: PolicySource | string | null;
  policy_source_version?: SourceVersion | null;
  part_id?: string | null;
  budget?: unknown;
  graph?: { nodes?: readonly { id: string; type: string; params?: Record<string, unknown> | null }[] } | null;
  nodes?: readonly { node_id: string; outputs?: Record<string, unknown> | null }[];
  input_bindings?: readonly { port_key: string; asset_version_id: string; version?: { asset_name?: string; label?: string; version_no?: number } | null }[];
}

/** The input versions a run was started with: bound asset versions, the spec version its spec step used and the files
 * that version read, and the file / spec / revision versions its graph pins that are not listed already. */
export function runInputs(run: RunLike): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const add = (key: string, label: string) => {
    if (!seen.has(key)) {
      seen.add(key);
      out.push(label);
    }
  };
  for (const b of run.input_bindings ?? []) {
    const v = b.version;
    add(b.asset_version_id, [v?.asset_name || b.port_key, v?.label || (v?.version_no != null ? `v${v.version_no}` : '')].filter(Boolean).join(' '));
  }
  for (const n of run.nodes ?? []) {
    const spec = Object.values(n.outputs ?? {}).find((v): v is SpecValue => !!v && typeof v === 'object' && (v as { type?: unknown }).type === 'engineering_spec');
    if (!spec) continue;
    add(spec.spec_version_id, `${spec.code || spec.title || short(spec.spec_version_id)} v${spec.version_no}`);
    for (const s of spec.sources ?? []) {
      if (s.file_version_id) add(s.file_version_id, `${s.label || s.name || short(s.file_version_id)}${s.version != null && !(s.label || '').includes(`v${s.version}`) ? ` v${s.version}` : ''}`);
    }
  }
  for (const n of run.graph?.nodes ?? []) {
    const p = n.params ?? {};
    const ids = [p.file_version_id, ...(Array.isArray(p.file_version_ids) ? p.file_version_ids : []), p.spec_version_id, p.revision_id, p.fixed_asset_version_id];
    for (const id of ids) if (typeof id === 'string' && id) add(id, short(id));
  }
  return out;
}

/** The execution context of a workflow run (run card, run page). ``target``: the version the run continues from, else
 * null (the caller names the part / project). */
export function runExecutionContext(run: RunLike): ExecutionContext {
  const sv = run.policy_source_version ?? null;
  return {
    policy: asPolicy(run.execution_policy),
    source: asSource(run.policy_source),
    sourceLabel: sv?.label || null,
    inputs: runInputs(run),
    target: sv?.label || null,
    budget: asBudget(run.budget),
  };
}

interface RefLike {
  title?: string | null;
  version_label?: string | null;
  object_id?: string;
  kind?: string;
}

interface OperationLike {
  execution_policy?: ExecutionPolicy | string | null;
  policy_source?: PolicySource | string | null;
  request?: { target?: RefLike | null; references?: readonly RefLike[]; policy_source_version?: SourceVersion | null } | null;
  part?: { name?: string | null; number?: string | null } | null;
  /** The operation's run brief with the run's frozen ``budget`` caps (package J). */
  run?: object | null;
}

const refLabel = (r: RefLike) => [r.title || (r.object_id ? short(r.object_id) : r.kind || '?'), r.version_label].filter(Boolean).join(' ');

/** The execution context of an AI workspace operation (its frozen request snapshot). */
export function operationExecutionContext(op: OperationLike): ExecutionContext {
  const req = op.request ?? {};
  const target = req.target ? refLabel(req.target) : op.part ? [op.part.number, op.part.name].filter(Boolean).join(' ') : null;
  return {
    policy: asPolicy(op.execution_policy),
    source: asSource(op.policy_source),
    sourceLabel: req.policy_source_version?.label || null,
    inputs: (req.references ?? []).filter((r) => r.version_label).map(refLabel),
    target: target || null,
    budget: asBudget(op.run && 'budget' in op.run ? (op.run as { budget?: unknown }).budget : null),
  };
}

/** An Agent tool result's ``data.execution`` (server services/agent_tools.execution_brief). */
export function toolExecutionContext(execution: unknown): ExecutionContext | null {
  if (!execution || typeof execution !== 'object') return null;
  const e = execution as {
    execution_policy?: unknown;
    policy_source?: unknown;
    source_version?: SourceVersion | null;
    inputs?: { kind?: string; id?: string; slot_key?: string | null }[];
    target?: { type?: string; id?: string } | null;
    budget?: unknown;
  };
  return {
    policy: asPolicy(e.execution_policy),
    source: asSource(e.policy_source),
    sourceLabel: e.source_version?.label || null,
    inputs: (e.inputs ?? []).filter((i) => i.id).map((i) => `${i.slot_key || i.kind || ''} ${short(i.id!)}`.trim()),
    target: e.target?.id ? `${e.target.type ?? ''} ${short(e.target.id)}`.trim() : null,
    budget: asBudget(e.budget),
  };
}
