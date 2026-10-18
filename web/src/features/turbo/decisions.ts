// Generation supplement D (design §2.3): every AI / rule assumption read as one decision — object, trigger, adopted
// value, basis, impact, sources, state and problems — so specs, model revisions, assemblies and deliveries show them the
// same way: "AI decision: adopt …; reason: …; impact: …". Pure helpers (no React), unit-tested in decisions.test.ts. The
// server's policy.decision_record is authoritative; for raw assumptions (no ``problems`` / ``state`` yet) these mirror it.

import type { MessageKey } from '../../i18n/en';
import type { Vars } from '../../i18n/core';
import { formatAdopted } from './model';

export type DecisionTrigger = 'missing' | 'ambiguous' | 'conflict';
export type DecisionState = 'adopted' | 'confirmed' | 'corrected' | 'replaced' | 'person';
export type DecisionProblem = 'no_value' | 'no_basis' | 'generic_basis' | 'no_subject';

/** Anything the lists receive: a stored ``Assumption``, a server ``AiDecision``, an ``AssumptionBrief`` or an upstream
 * listing item. Every field but ``subject`` is optional; unknown values are shown as "not stated", never guessed. */
export interface DecisionLike {
  id?: string | null;
  subject: string;
  field?: string | null;
  adopted?: unknown;
  unit?: string | null;
  basis?: string | null;
  trigger?: string | null;
  affected?: string[] | null;
  sources?: string[] | null;
  alternatives?: unknown[] | null;
  author?: string | null;
  state?: string | null;
  problems?: string[] | null;
  confirmation?: { decision?: string | null } | null;
  replaced_by?: unknown;
  owner?: { type: string; id: string; label?: string; rev_no?: number; version_no?: number } | string | null;
}

export interface DecisionView {
  key: string;
  id: string | null;
  /** What it is about, e.g. "D-03.length" (subject plus field when the subject does not already end with it). */
  subject: string;
  /** The object the decision belongs to (owner version + definition / requirement key): the group of the list. */
  object: string;
  trigger: DecisionTrigger | null;
  /** The adopted value with its unit; null when nothing was adopted. */
  value: string | null;
  basis: string;
  affected: string[];
  sources: string[];
  alternatives: string[];
  author: 'ai_turbo' | 'ai_design' | 'rule' | 'person';
  state: DecisionState;
  problems: DecisionProblem[];
}

const TRIGGERS: readonly string[] = ['missing', 'ambiguous', 'conflict'];
const STATES: readonly string[] = ['adopted', 'confirmed', 'corrected', 'replaced', 'person'];
const PROBLEMS: readonly string[] = ['no_value', 'no_basis', 'generic_basis', 'no_subject'];

// Mirrors server policy._GENERIC_BASIS / _PLACEHOLDER_VALUES: a basis made only of these phrases names no reason.
const GENERIC_BASIS =
  /按常规|常规处理|由建成几何决定|由几何决定|AI ?已?自动补全|已自动补全|自动处理|as usual|by convention|decided by the (built )?geometry|auto[- ]?(completed|filled)|handled automatically/gi;
const PLACEHOLDERS = new Set(['tbd', 'tba', 'n/a', 'unknown', '待定', '未定', '未知', '待确认']);

function emptyValue(v: unknown): boolean {
  if (v === null || v === undefined || v === '') return true;
  if (Array.isArray(v)) return v.length === 0;
  if (typeof v === 'object') return Object.keys(v as object).length === 0;
  if (typeof v === 'string') return PLACEHOLDERS.has(v.trim().toLowerCase());
  return false;
}

/** The problems of a decision: the server's list when it sent one, else the same rules computed here. */
export function decisionProblems(d: DecisionLike): DecisionProblem[] {
  if (Array.isArray(d.problems)) return d.problems.filter((p): p is DecisionProblem => PROBLEMS.includes(p));
  const out: DecisionProblem[] = [];
  if (!String(d.subject ?? '').trim()) out.push('no_subject');
  if (emptyValue(d.adopted)) out.push('no_value');
  const basis = String(d.basis ?? '').trim();
  if (!basis) out.push('no_basis');
  else if (basis.search(GENERIC_BASIS) >= 0 && basis.replace(GENERIC_BASIS, '').replace(/[\s，。,.;；:：-]/g, '').length < 12) out.push('generic_basis');
  return out;
}

/** adopted (in use, waiting for a person) / confirmed / corrected (a person decided) / replaced / person (a person's own
 * answer). The server's ``state`` wins; an upstream listing's ``unconfirmed`` reads adopted. */
export function decisionState(d: DecisionLike): DecisionState {
  if (d.author === 'person') return 'person';
  if (d.state && STATES.includes(d.state)) return d.state as DecisionState;
  if (d.state === 'unconfirmed') return 'adopted';
  if (d.replaced_by) return 'replaced';
  const c = d.confirmation?.decision;
  if (c === 'confirmed' || c === 'corrected') return c;
  return 'adopted';
}

function ownerTag(owner: DecisionLike['owner']): string {
  if (!owner || typeof owner !== 'object') return '';
  if (owner.label) return owner.label;
  if (typeof owner.version_no === 'number') return `v${owner.version_no}`;
  if (typeof owner.rev_no === 'number') return `${owner.type === 'assembly_revision' ? 'A' : 'R'}${owner.rev_no}`;
  return '';
}

function text(v: unknown): string {
  if (typeof v === 'string') return v;
  return formatAdopted({ adopted: v, unit: null }) ?? String(v);
}

export function decisionView(d: DecisionLike, index = 0): DecisionView {
  const subject = String(d.subject ?? '');
  const field = d.field ? String(d.field) : '';
  const full = field && !subject.endsWith(field) ? `${subject} · ${field}` : subject;
  const root = subject.split('.')[0] || subject;
  const tag = ownerTag(d.owner);
  const author = d.author === 'rule' || d.author === 'person' || d.author === 'ai_design' ? d.author : 'ai_turbo';
  const value = emptyValue(d.adopted) ? null : formatAdopted({ adopted: d.adopted, unit: d.unit ?? null });
  return {
    key: `${tag}#${d.id ?? ''}#${subject}#${index}`,
    id: d.id ?? null,
    subject: full,
    object: tag ? `${tag} · ${root}` : root,
    trigger: d.trigger && TRIGGERS.includes(d.trigger) ? (d.trigger as DecisionTrigger) : null,
    value,
    basis: String(d.basis ?? '').trim(),
    affected: (d.affected ?? []).map(String).filter(Boolean),
    sources: (d.sources ?? []).map(String).filter(Boolean),
    alternatives: (d.alternatives ?? []).map(text).filter(Boolean),
    author,
    state: decisionState(d),
    problems: decisionProblems(d),
  };
}

type T = (key: MessageKey, vars?: Vars) => string;

/** The one sentence of a decision: "AI decision: adopt 30 mm; reason: …; impact: D-03, I-10" (a person's own value reads
 * "Human-provided: …"); a missing value, reason or impact says so instead of staying silent. */
export function decisionLine(v: Pick<DecisionView, 'value' | 'basis' | 'affected' | 'author'>, t: T): string {
  const none = t('tb.decision.unstated');
  return t(v.author === 'person' ? 'tb.decision.linePerson' : 'tb.decision.line', {
    value: v.value ?? t('tb.decision.noValue'),
    basis: v.basis || none,
    impact: v.affected.length ? v.affected.join(t('tb.decision.listSep')) : none,
  });
}

export interface DecisionGroup {
  object: string;
  items: DecisionView[];
  problems: number;
}

/** Decisions grouped by the object they belong to, in first-seen order. */
export function groupDecisions(views: DecisionView[]): DecisionGroup[] {
  const groups = new Map<string, DecisionGroup>();
  for (const v of views) {
    const g = groups.get(v.object) ?? { object: v.object, items: [], problems: 0 };
    g.items.push(v);
    if (v.problems.length) g.problems += 1;
    groups.set(v.object, g);
  }
  return [...groups.values()];
}

/** A decision the server did not keep (spec merge / finish record, generation supplement D-03). */
export interface DroppedLike {
  subject: string;
  adopted: unknown;
  unit?: string | null;
  reason: string;
  assumption?: string;
  stated?: unknown;
  stated_unit?: string | null;
}

/** The dropped decisions worth showing (a stated value kept, a person's answer that replaced a decision), once per
 * subject; ``not_open`` adoptions (nothing to decide) are left out. */
export function droppedDecisions(...lists: (DroppedLike[] | null | undefined)[]): DroppedLike[] {
  const seen = new Set<string>();
  const out: DroppedLike[] = [];
  for (const d of lists.flatMap((l) => l ?? [])) {
    if (d.reason !== 'stated_value' && d.reason !== 'answered') continue;
    const key = `${d.reason}#${d.subject}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(d);
  }
  return out;
}

/** "AI decision not adopted: D-01.outer_diameter is explicitly 30 mm in the source without conflicts; retain the source value (AI proposed 32 mm)" / "AI decision A-01 was superseded by a human answer: …". */
export function droppedLine(d: DroppedLike, t: T): string {
  const stated = formatAdopted({ adopted: d.stated, unit: d.stated_unit ?? null }) ?? t('tb.decision.unstated');
  const adopted = formatAdopted({ adopted: d.adopted, unit: d.unit ?? null }) ?? t('tb.decision.noValue');
  return d.reason === 'answered'
    ? t('tb.decision.dropped.answered', { id: d.assumption ?? '', subject: d.subject, stated })
    : t('tb.decision.dropped.stated_value', { subject: d.subject, stated, adopted });
}

/** Counts for the list header: per trigger ("unstated" for older records), waiting for a person, with problems. */
export function decisionCounts(views: DecisionView[]): { byTrigger: Record<DecisionTrigger | 'unstated', number>; adopted: number; withProblems: number } {
  const byTrigger = { missing: 0, ambiguous: 0, conflict: 0, unstated: 0 };
  for (const v of views) byTrigger[v.trigger ?? 'unstated'] += 1;
  return {
    byTrigger,
    adopted: views.filter((v) => v.state === 'adopted').length,
    withProblems: views.filter((v) => v.problems.length > 0).length,
  };
}
